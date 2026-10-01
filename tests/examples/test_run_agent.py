"""Launcher failure paths with a tiny fake Claude CLI: no model, no network, no real server."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

CASE = Path(__file__).resolve().parents[2] / "examples" / "agent-case-study"
sys.path.insert(0, str(CASE))

import run_agent  # noqa: E402

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

FAKE = """#!{python}
import json, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve()
if "--version" in sys.argv:
    print("0.0.0 (fake)"); sys.exit(0)
mode = here.with_suffix(".mode").read_text().strip()
sys.stdin.read()
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    here.with_suffix(".child").write_text(str(child.pid))
    time.sleep(600)
def emit(obj): print(json.dumps(obj), flush=True)
emit({{"type": "system", "subtype": "init", "model": "fake", "tools": ["mcp__genomics__get_signal"]}})
text = {{"prose": "All done, trust me.", "leak": "contact someone@example.org"}}[mode]
emit({{"type": "assistant", "message": {{"model": "fake", "content": [{{"type": "text", "text": text}}]}}}})
emit({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": text}})
"""


def _fake(tmp_path: Path, mode: str) -> Path:
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE.format(python=sys.executable))
    cli.chmod(0o755)
    cli.with_suffix(".mode").write_text(mode)
    return cli


def _run(monkeypatch, tmp_path, *args: str) -> int:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("synthetic test prompt")
    argv = ["run_agent.py", "--server-command", "/bin/echo", "--prompt-file", str(prompt), *args]
    monkeypatch.setattr(sys, "argv", argv)
    return run_agent.main()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_refuses_nonempty_output_directory_before_writing(monkeypatch, tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "prompt.txt").write_text("SENTINEL")
    assert _run(monkeypatch, tmp_path, "--out", str(out), "--dry-run") == 2
    assert (out / "prompt.txt").read_text() == "SENTINEL"
    assert sorted(p.name for p in out.iterdir()) == ["prompt.txt"]


def test_dry_run_keeps_session_id_out_of_published_files(monkeypatch, tmp_path):
    out = tmp_path / "run"
    assert (
        _run(
            monkeypatch,
            tmp_path,
            "--out",
            str(out),
            "--dry-run",
            "--claude",
            str(_fake(tmp_path, "prose")),
        )
        == 0
    )
    assert "session_id" in json.loads((out / "raw" / "launch.json").read_text())
    assert not (out / "launch.json").exists()


def test_timeout_stops_the_whole_process_group(monkeypatch, tmp_path):
    cli = _fake(tmp_path, "hang")
    rc = _run(
        monkeypatch,
        tmp_path,
        "--out",
        str(tmp_path / "run"),
        "--claude",
        str(cli),
        "--timeout-min",
        "0.05",
    )
    assert rc == 124
    child = int(cli.with_suffix(".child").read_text())
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child), "grandchild survived the launcher timeout"
    record = json.loads((tmp_path / "run" / "raw" / "launch.json").read_text())
    assert record["timed_out"] is True


def test_finished_model_without_measurements_is_not_validated(monkeypatch, tmp_path, capsys):
    out = tmp_path / "run"
    assert (
        _run(monkeypatch, tmp_path, "--out", str(out), "--claude", str(_fake(tmp_path, "prose")))
        == 3
    )
    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["model_process_exit"] == 0 and status["extraction_exit"] == 0
    assert status["coverage_exit"] == 1
    assert json.loads((out / "run.json").read_text())["completion"].startswith("incomplete")
    assert "session_id" not in (out / "launch.json").read_text()


def test_extraction_failure_propagates(monkeypatch, tmp_path, capsys):
    rc = _run(
        monkeypatch,
        tmp_path,
        "--out",
        str(tmp_path / "run"),
        "--claude",
        str(_fake(tmp_path, "leak")),
    )
    assert rc == 3
    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["extraction_exit"] != 0 and status["coverage_exit"] is None


@pytest.mark.parametrize("run", ["2026-09-28-claude-opus-5", "2026-09-28-interrupted"])
def test_published_run_metadata_has_no_session_id(run):
    for name in ("run.json", "launch.json", "NOTE.md"):
        text = (CASE / "runs" / run / name).read_text()
        assert "session_id" not in text and "--session-id" not in text, name
        assert not UUID.search(text), name
