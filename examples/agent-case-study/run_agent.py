#!/usr/bin/env python3
"""Run the case-study agent: Claude Code (your own subscription) + the local Genomics MCP over stdio.

The agent gets only the `genomics` MCP tools (no shell, files or web). The prompt is
agent/prompt.md + PROTOCOL.md + the panel from manifest.json. Claude Code's stream-json output
is kept under <out>/raw/ (git-ignored) and a redacted copy plus extracted tool evidence is
written next to it by extract_run.py.

Credential isolation: the Claude and server processes get an allowlisted environment. Cloud
provider, AWS/boto/HTSlib and Anthropic API-key variables are never passed on, EC2 metadata is
disabled and AWS/boto config files point at an empty file. ~/.aws is never read.

    python3 examples/agent-case-study/run_agent.py --dry-run      # write prompt + config only
    python3 examples/agent-case-study/run_agent.py                # run (uses your Claude login)

Exit status: 0 only when the model process finished, extraction succeeded, the run is complete
and every (file, window) cell maps to a successful MCP result equal to the agent's value
(`replay.py --coverage-only`). 2: --out exists and is not empty. 3: the model finished but the
case is not validated. 124: timeout. Independent live verification is the separate command
`replay.py RUN_DIR`. On timeout, interruption or error the launcher stops its own process group
(Claude Code, the MCP server and its readers) before removing scratch files.

The default model is claude-opus-5. With the `opus` alias (Opus 5.5 at the time), the first
attempt was flagged by Opus 5.5's biology safeguards and Claude Code continued on Opus 5
automatically, mixing two models in one transcript. Pinning one model keeps a run to one model;
extract_run.py still records every model that answered.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# Passed through to Claude Code and the server. Everything else is dropped.
KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR")


def isolated_env(empty: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in KEEP if k in os.environ}
    env.update(
        AWS_EC2_METADATA_DISABLED="true",
        AWS_CONFIG_FILE=str(empty),
        AWS_SHARED_CREDENTIALS_FILE=str(empty),
        AWS_SDK_LOAD_CONFIG="0",
        BOTO_CONFIG=str(empty),
    )
    return env


def panel_markdown(manifest: dict) -> str:
    rows = [
        "| group | label | experiment | biological replicate | file | donor | ENCODE audit flags | url |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for p in manifest["panel"]:
        for f in p["files"]:
            rows.append(
                f"| {p['group']} | {p['label']} | {p['experiment']} | {f['biological_replicate']} "
                f"| {f['file']} | {f['donor']} | {', '.join(p['audit_flags']) or '-'} | {f['url']} |"
            )
    return (
        f"Curated by the protocol author with discovery/select_files.py on "
        f"{manifest['generated_utc']}: {manifest['experiments']} experiments, "
        f"{manifest['files']} files. Assay {manifest['assay']}, output "
        f"'{manifest['output_type']}', assembly {manifest['assembly']}.\n\n" + "\n".join(rows)
    )


def build_prompt() -> str:
    manifest = json.loads((HERE / "manifest.json").read_text())
    return (
        (HERE / "agent" / "prompt.md").read_text()
        + "\n# Protocol\n\n"
        + (HERE / "PROTOCOL.md").read_text()
        + "\n# Panel\n\n"
        + panel_markdown(manifest)
        + "\n"
    )


def git(*args: str) -> str | None:
    try:
        return subprocess.run(  # noqa: S603
            [shutil.which("git") or "git", "-C", str(REPO), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def provenance(server_command: list[str], env: dict[str, str], claude: str) -> dict:
    """Which example and runtime ran. Paths are reduced to names; nothing secret is recorded."""
    version = None
    try:
        version = subprocess.run(  # noqa: S603
            [*server_command, "--version"], capture_output=True, text=True, env=env, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        claude = subprocess.run(  # noqa: S603
            [claude, "--version"],
            capture_output=True, text=True, env=env, timeout=60,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        claude = None
    src = git("rev-parse", "HEAD:src")
    return {
        "example_commit": git("rev-parse", "HEAD"),
        "example_files_dirty": bool(
            # The run's own output directory under runs/ does not count.
            git("status", "--porcelain", "--", str(HERE.relative_to(REPO)), ":!*/runs/*")
        ),
        "runtime_src_tree": src,
        "runtime_src_equals_v0_1_0": src is not None and src == git("rev-parse", "v0.1.0^{}:src"),
        "runtime_dirty": bool(
            git("status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock")
        ),
        "server_version": version,
        "claude_code_version": claude,
    }


def stop_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """Terminate the launched process group (Claude Code, the MCP server and its readers).

    Only the group this launcher created is signalled; nothing is killed by name. macOS reports
    EPERM for a group whose remaining members are zombies, which counts as stopped.
    """

    def alive() -> bool:
        proc.poll()  # reap the leader so it does not keep the group alive as a zombie
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not alive():
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + grace
        while alive() and time.monotonic() < deadline:
            time.sleep(0.1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument(
        "--server-command",
        nargs="+",
        default=[str(REPO / ".venv" / "bin" / "genomics-mcp")],
        help="stdio server command (default: this checkout's .venv/bin/genomics-mcp)",
    )
    ap.add_argument("--model", default="claude-opus-5", help="Claude Code model alias or ID")
    ap.add_argument("--out", type=Path, help="new run directory (default: runs/<UTC time>)")
    ap.add_argument("--timeout-min", type=float, default=45)
    ap.add_argument("--dry-run", action="store_true", help="write prompt and configs, do not run")
    ap.add_argument("--prompt-file", type=Path, help="use this prompt instead (smoke tests)")
    ap.add_argument("--claude", default=shutil.which("claude") or "claude", help=argparse.SUPPRESS)
    a = ap.parse_args()

    started = datetime.now(UTC)
    out = a.out or HERE / "runs" / started.strftime("%Y%m%dT%H%M%SZ")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(
            f"refusing to write into existing non-empty {out}; choose a new --out", file=sys.stderr
        )
        return 2
    (out / "raw").mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="gmcp-agent-"))
    empty = scratch / "empty"
    empty.write_text("")
    work = scratch / "work"
    work.mkdir()
    cwd = scratch / "cwd"  # empty working directory: no project settings or CLAUDE.md
    cwd.mkdir()

    server_cfg = scratch / "genomics-mcp.toml"
    server_cfg.write_text(
        (HERE / "agent" / "genomics-mcp.toml").read_text().replace("WORK_DIR", str(work))
    )
    env = isolated_env(empty)
    mcp = {
        "mcpServers": {
            "genomics": {
                "type": "stdio",
                "command": a.server_command[0],
                "args": a.server_command[1:],
                "env": {
                    "GENOMICS_MCP_CONFIG": str(server_cfg),
                    **{k: v for k, v in env.items() if k.startswith(("AWS_", "BOTO_"))},
                },
            }
        }
    }
    mcp_path = scratch / "mcp.json"
    mcp_path.write_text(json.dumps(mcp, indent=2))
    prompt = a.prompt_file.read_text() if a.prompt_file else build_prompt()
    (out / "prompt.txt").write_text(prompt)

    session = str(uuid.uuid4())
    cmd = [
        a.claude,
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--session-id", session,
        "--model", a.model,
        "--strict-mcp-config",
        "--mcp-config", str(mcp_path),
        "--tools", "",
        "--allowedTools", "mcp__genomics",
        "--no-session-persistence",
        # Only project settings, from the empty cwd: no user hooks, env blocks or permissions.
        "--setting-sources", "project",
    ]  # fmt: skip
    # Internal record, kept locally under raw/ (git-ignored). extract_run.py publishes a copy
    # without the session ID.
    launch = {
        "session_id": session,
        "started_utc": started.isoformat(timespec="seconds"),
        "model_requested": a.model,
        "claude_args": [c for c in cmd[1:] if c not in (str(mcp_path),)],
        "server_command": [Path(c).name if os.sep in c else c for c in a.server_command],
        "server_config": (HERE / "agent" / "genomics-mcp.toml").name,
        "env_passed": sorted(env),
        "provenance": provenance(a.server_command, env, a.claude),
    }
    record = out / "raw" / "launch.json"
    record.write_text(json.dumps(launch, indent=2) + "\n")
    print(json.dumps({"out": str(out)}), flush=True)
    if a.dry_run:
        shutil.rmtree(scratch, ignore_errors=True)
        return 0

    raw = out / "raw" / "transcript.jsonl"
    t0 = time.monotonic()
    timed_out = False
    with raw.open("w") as fh:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd, stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.STDOUT, text=True,
            cwd=cwd, env=env, start_new_session=True,
        )  # fmt: skip
        try:
            proc.communicate(prompt, timeout=a.timeout_min * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # On timeout, Ctrl-C or any error, and also after a normal exit: stop whatever
            # is left in the group (for example the MCP server) before removing scratch.
            stop_group(proc)
            proc.wait()
    launch.update(
        exit_code=proc.returncode,
        timed_out=timed_out,
        elapsed_seconds=round(time.monotonic() - t0, 1),
        work_dir_bytes_after=sum(p.stat().st_size for p in work.rglob("*") if p.is_file()),
        finished_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    record.write_text(json.dumps(launch, indent=2) + "\n")
    shutil.rmtree(scratch, ignore_errors=True)

    # A finished model process is not a validated case: extraction must succeed, the run must
    # be complete, and every (file, window) cell must map to its own successful MCP result.
    # Independent live verification is the separate `replay.py RUN_DIR` command.
    extracted = subprocess.run(  # noqa: S603
        [sys.executable, str(HERE / "extract_run.py"), str(out)], check=False
    ).returncode
    coverage = (
        subprocess.run(  # noqa: S603
            [sys.executable, str(HERE / "replay.py"), str(out), "--coverage-only"], check=False
        ).returncode
        if extracted == 0
        else None
    )
    status = {
        "model_process_exit": proc.returncode,
        "timed_out": timed_out,
        "extraction_exit": extracted,
        "coverage_exit": coverage,
        "next": "run replay.py on this directory for independent live verification",
    }
    print(json.dumps(status))
    if timed_out:
        return 124
    if proc.returncode:
        return proc.returncode if proc.returncode > 0 else 1
    return 0 if extracted == 0 and coverage == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
