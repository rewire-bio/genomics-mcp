# ruff: noqa: ASYNC240  (tests read finished artifacts from disk)
"""convert_sra_run with a scripted toolkit: availability, validation, budgets, resume, cancel,
timeout and independent FASTQ checks. Real-toolkit runs are in test_sra_live.py and the demo."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from gm_test_support import envelope, make_settings

from genomics_mcp.artifacts import sra as sra_mod
from genomics_mcp.service import GenomicsService

ACC = "SRR0000001"
FAKE = Path(__file__).with_name("fake_sra_toolkit.py")


@pytest.fixture
def toolkit(tmp_path):
    bin_dir = tmp_path / "toolkit"
    bin_dir.mkdir()
    script = f"#!{sys.executable}\n" + FAKE.read_text()
    for name in ("prefetch", "fasterq-dump"):
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)
    (bin_dir / "control.json").write_text("{}")
    return bin_dir


def control(bin_dir: Path, **values) -> None:
    (bin_dir / "control.json").write_text(json.dumps(values))


def calls(bin_dir: Path) -> list[dict]:
    path = bin_dir / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
async def svc(tmp_path, toolkit):
    s = GenomicsService(make_settings(tmp_path, [], sra_toolkit={"bin_dir": str(toolkit)}))
    yield s
    await s.aclose()


async def convert(svc, **args):
    return envelope(await svc.call("convert_sra_run", {"accession": ACC, **args}))


async def wait_done(svc, tid, seconds=20.0):
    for _ in range(int(seconds / 0.05)):
        res = envelope(await svc.call("get_transfer_status", {"transfer_id": tid}))
        if res["data"]["transfer"]["state"] in ("completed", "failed", "cancelled"):
            return res
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def run_job(svc, **args):
    res = await convert(svc, **args)
    assert res["status"] == "ok", res
    return await wait_done(svc, res["data"]["transfer"]["transfer_id"])


def job_dir(svc, res) -> Path:
    tid = res["data"]["transfer"]["transfer_id"]
    return svc.settings.paths.work_dir / "transfers" / tid / "sra.part"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ------------------------------------------------------------------ availability and input


async def test_missing_toolkit_is_actionable_and_other_tools_still_work(tmp_path):
    svc = GenomicsService(make_settings(tmp_path, [], sra_toolkit={"bin_dir": str(tmp_path)}))
    try:
        res = await convert(svc)
        assert res["status"] == "error"
        assert res["error"]["code"] == "unsupported"
        assert "prefetch was not found" in res["error"]["message"]
        assert "ENA FASTQ" in res["error"]["hint"]
        status = svc.status()["sra_toolkit"]
        assert status["available"] is False and status["version"] is None
        assert envelope(await svc.call("list_sources", {}))["status"] == "ok"
    finally:
        await svc.aclose()


async def test_status_reports_version(svc, toolkit):
    status = svc.status()["sra_toolkit"]
    assert status["available"] is True
    assert status["version"] == "3.4.1"
    assert status["prefetch"] == str(toolkit / "prefetch")


async def test_old_toolkit_is_refused(tmp_path, toolkit):
    control(toolkit, version="2.11.0")
    svc = GenomicsService(make_settings(tmp_path, [], sra_toolkit={"bin_dir": str(toolkit)}))
    try:
        res = await convert(svc)
        assert res["error"]["code"] == "unsupported"
        assert "older than" in res["error"]["message"]
    finally:
        await svc.aclose()


@pytest.mark.parametrize(
    "accession", ["SRP123456", "srr1234567", "SRR12", "SRR1234567 --force", "../SRR1234567", ""]
)
async def test_invalid_accessions_never_reach_the_toolkit(svc, toolkit, accession):
    res = envelope(await svc.call("convert_sra_run", {"accession": accession}))
    assert res["error"]["code"] == "invalid_input"
    assert [c for c in calls(toolkit) if c["args"] != ["--version"]] == []


async def test_extra_arguments_are_rejected(svc):
    res = envelope(await svc.call("convert_sra_run", {"accession": ACC, "flags": ["--force"]}))
    assert res["error"]["code"] == "invalid_input"


# ------------------------------------------------------------------ successful conversions


async def test_paired_and_unpaired_reads_are_kept_and_verified(svc, toolkit):
    control(toolkit, pairs=3, unpaired=2, deps=["NC_000913.3"])
    res = await run_job(svc)
    data = res["data"]
    assert data["transfer"]["state"] == "completed", data["transfer"]["error"]
    prep = data["preparation"]
    roles = {f["role"]: f for f in prep["fastq"]}
    assert set(roles) == {"mate_1", "mate_2", "unpaired"}
    assert (roles["mate_1"]["reads"], roles["mate_2"]["reads"], roles["unpaired"]["reads"]) == (
        3,
        3,
        2,
    )
    assert roles["unpaired"]["bases"] == 6
    assert prep["verification"] == {
        "reads_written_by_fasterq_dump": 8,
        "reads_counted": 8,
        "bases_counted": 30,
        "biological_bases_in_run": 30,
        "all_biological_bases_written": True,
        "spot_numbers_strictly_increasing": True,
        "pairs": 3,
        "unpaired_reads": 2,
        "records_well_formed": True,
        "mate_read_names_match": True,
    }
    assert prep["toolkit"]["version"] == "3.4.1"
    assert prep["dependencies"] == ["NC_000913.3"]
    assert prep["sequence_table"] == "SEQUENCE"
    assert prep["readiness"] == "not_locus_ready"
    assert all(f["file"]["readiness"]["state"] == "not_locus_ready" for f in prep["fastq"])
    # Artifacts: in the work dir, with checksums of the bytes on disk and SRA provenance.
    art_root = svc.settings.paths.work_dir / "artifacts"
    for art in data["artifacts"]:
        path = Path(art["path"])
        assert path.parent.parent == art_root
        sums = {c["algorithm"]: c["value"] for c in art["checksums"]}
        assert sums["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert art["format"] == "fastq" and art["checksum_verified"] is False
        assert art["provenance"]["source"] == "sra"
        assert art["provenance"]["source_record_id"] == ACC
        assert "fasterq-dump --split-3" in art["provenance"]["method"]
    assert res["provenance"][0]["source_record_id"] == ACC
    # Reads stay on disk: no sequence data in the MCP response.
    assert "ACGT" not in json.dumps(res)
    # Download, scratch and toolkit configuration are gone; only FASTQ remains.
    assert not job_dir(svc, res).exists()
    assert data["transfer"]["bytes_done"] == sum(a["size_bytes"] for a in data["artifacts"])


async def test_single_end_run(svc, toolkit):
    control(toolkit, pairs=0, unpaired=4)
    data = (await run_job(svc))["data"]
    assert data["transfer"]["state"] == "completed"
    assert [f["role"] for f in data["preparation"]["fastq"]] == ["unpaired"]
    assert data["preparation"]["verification"]["mate_read_names_match"] is False
    assert Path(data["artifacts"][0]["path"]).name == f"{ACC}.fastq"


async def test_toolkit_commands_config_and_environment(svc, toolkit, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADUMMYDUMMYDUMMY0")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dummy-ambient-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("NCBI_SETTINGS", "/etc/somewhere/user-settings.mkfg")
    await run_job(svc)
    work = svc.settings.paths.work_dir
    used = [c for c in calls(toolkit) if c["args"] != ["--version"]]
    assert [c["tool"] for c in used] == ["prefetch", "fasterq-dump", "fasterq-dump"]
    fetch, check, conv = used
    assert fetch["args"][:10] == [
        ACC,
        "--type",
        "sra",
        "--transport",
        "http",
        "--max-size",
        str(100 * 1024),
        "--resume",
        "yes",
        "--verify",
    ]
    assert fetch["args"][11:12] == ["--output-directory"]
    assert "--size-check" in check["args"] and "--details" in check["args"]
    assert conv["args"][conv["args"].index("--disk-limit") + 1].isdigit()
    for c in used:
        env = c["env"]
        assert not any(k.startswith("AWS_") and k not in _SAFE_AWS for k in env), env
        assert "HTTPS_PROXY" not in env
        assert env["AWS_EC2_METADATA_DISABLED"] == "true"
        for var in ("HOME", "TMPDIR", "NCBI_SETTINGS"):
            assert Path(env[var]).is_relative_to(work)
        assert 'accept_aws_charges = "false"' in c["settings"]
        assert 'accept_gcp_charges = "false"' in c["settings"]
        assert 'report_instance_identity = "false"' in c["settings"]
    assert '/repository/remote/disabled = "false"' in fetch["settings"]
    for c in (check, conv):
        assert '/repository/remote/disabled = "true"' in c["settings"]


_SAFE_AWS = {"AWS_EC2_METADATA_DISABLED", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"}


async def test_completed_conversion_is_reused_only_while_unchanged(svc, toolkit):
    first = await run_job(svc)
    tid = first["data"]["transfer"]["transfer_id"]
    again = await convert(svc)
    assert again["data"]["transfer"]["transfer_id"] == tid
    assert "already converted" in " ".join(again["data"]["notes"])
    Path(first["data"]["artifacts"][0]["path"]).write_text("edited")
    redo = await wait_done(svc, (await convert(svc))["data"]["transfer"]["transfer_id"])
    assert redo["data"]["transfer"]["transfer_id"] != tid
    assert redo["data"]["transfer"]["state"] == "completed"


# ------------------------------------------------------------------ failures


async def test_unknown_accession(svc, toolkit):
    control(toolkit, prefetch="404")
    res = await run_job(svc)
    err = res["data"]["transfer"]["error"]
    assert res["data"]["transfer"]["state"] == "failed"
    assert err["code"] == "not_found" and "no data ( 404 )" in err["details"]["toolkit_output"]
    assert res["data"]["artifacts"] == []
    assert not job_dir(svc, res).exists()


async def test_controlled_access_is_refused(svc, toolkit):
    control(toolkit, prefetch="403")
    res = await run_job(svc)
    assert res["data"]["transfer"]["error"]["code"] == "unauthorized"


async def test_interrupted_download_resumes_in_the_same_job(svc, toolkit):
    control(toolkit, prefetch="interrupt", run_bytes=4000)
    failed = await run_job(svc)
    transfer = failed["data"]["transfer"]
    assert transfer["state"] == "failed" and transfer["resumable"] is True
    assert transfer["error"]["retryable"] is True
    root = job_dir(svc, failed)
    partial = root / "download" / ACC / f"{ACC}.sra.tmp"
    assert partial.stat().st_size == 2000
    lock = root / "download" / ACC / f"{ACC}.sra.lock"
    assert not lock.exists()
    prf = root / "download" / ACC / f"{ACC}.sra.prf"
    assert prf.exists()
    control(toolkit, prefetch="ok", run_bytes=4000)
    done = await run_job(svc)
    assert done["data"]["transfer"]["transfer_id"] == transfer["transfer_id"]
    assert done["data"]["transfer"]["state"] == "completed"
    assert (toolkit / "resumed.txt").read_text() == "2000"
    assert any("resuming" in n for n in done["data"]["notes"])


async def test_pre_existing_lock_is_preserved_and_not_cleaned(svc, toolkit):
    control(toolkit, prefetch="interrupt", run_bytes=4000)
    failed = await run_job(svc)
    d = job_dir(svc, failed) / "download" / ACC
    foreign_lock = d / f"{ACC}.sra.lock"
    foreign_lock.touch()
    again = await run_job(svc)
    assert again["data"]["transfer"]["state"] == "failed"
    assert "lock exists" in again["data"]["transfer"]["error"]["details"]["toolkit_output"]
    assert foreign_lock.exists()


async def test_cancel_kills_the_process_group_and_removes_files(svc, toolkit):
    control(toolkit, prefetch="hang")
    res = await convert(svc)
    tid = res["data"]["transfer"]["transfer_id"]
    for _ in range(200):
        if (toolkit / "grandchild.pid").exists():
            break
        await asyncio.sleep(0.05)
    grandchild = int((toolkit / "grandchild.pid").read_text())
    tool_pid = next(
        c["pid"] for c in calls(toolkit) if c["tool"] == "prefetch" and c["args"][0] == ACC
    )
    cancelled = envelope(await svc.call("cancel_transfer", {"transfer_id": tid}))
    assert cancelled["data"]["transfer"]["state"] == "cancelled"
    await asyncio.sleep(0.2)
    assert not pid_alive(tool_pid) and not pid_alive(grandchild)
    assert not job_dir(svc, res).exists()
    later = await wait_done(svc, tid)
    assert later["data"]["transfer"]["state"] == "cancelled"
    assert later["data"]["artifacts"] == []


async def test_timeout_stops_the_toolkit_and_keeps_the_download(svc, toolkit):
    control(toolkit, fasterq="hang")
    res = await run_job(svc, timeout_s=2)
    transfer = res["data"]["transfer"]
    assert transfer["state"] == "failed"
    assert transfer["error"]["code"] == "timeout" and transfer["error"]["retryable"] is True
    await asyncio.sleep(0.2)
    assert not pid_alive(int((toolkit / "grandchild.pid").read_text()))
    root = job_dir(svc, res)
    assert (root / "download" / ACC / f"{ACC}.sra").exists()
    assert list((root / "fastq").glob("*")) == []


async def test_timeout_cannot_exceed_the_configured_limit(tmp_path, toolkit):
    control(toolkit, fasterq="hang")
    settings = make_settings(tmp_path, [], sra_toolkit={"bin_dir": str(toolkit), "timeout_s": 1.5})
    svc = GenomicsService(settings)
    try:
        res = await run_job(svc, timeout_s=600)
        assert res["data"]["transfer"]["error"]["code"] == "timeout"
        assert "1.5s" in res["data"]["transfer"]["error"]["message"]
    finally:
        await svc.aclose()


async def test_growth_past_the_budget_is_stopped(svc, toolkit):
    control(toolkit, prefetch="grow")
    res = await run_job(svc, budget_bytes=2_000_000)
    err = res["data"]["transfer"]["error"]
    assert err["code"] == "budget_exceeded" and "toolkit was stopped" in err["message"]
    assert not job_dir(svc, res).exists()


async def test_run_file_over_budget_is_skipped_by_prefetch(svc, toolkit):
    control(toolkit, prefetch="too_large")
    res = await run_job(svc)
    assert res["data"]["transfer"]["error"]["code"] == "budget_exceeded"


async def test_estimate_over_budget_keeps_download_for_a_larger_budget(svc, toolkit):
    control(toolkit, run_bytes=4000, estimate=100_000)
    res = await run_job(svc, budget_bytes=100_000)
    err = res["data"]["transfer"]["error"]
    assert err["code"] == "budget_exceeded" and err["retryable"] is True
    assert err["details"]["output_bytes_estimate"] == 100_000
    needed = err["details"]["required_bytes"]
    assert needed == err["details"]["run_bytes_on_disk"] + 125_000 + 150_000
    assert [c["tool"] for c in calls(toolkit)].count("prefetch") == 2  # --version + download
    done = await run_job(svc, budget_bytes=needed)
    assert done["data"]["transfer"]["state"] == "completed"
    assert done["data"]["preparation"]["estimates"]["required_bytes"] == needed


async def test_missing_estimate_fails_before_conversion(svc, toolkit):
    control(toolkit, fasterq="no_estimate")
    res = await run_job(svc)
    err = res["data"]["transfer"]["error"]
    assert err["code"] == "upstream_error" and "estimate" in err["message"]
    assert [c["tool"] for c in calls(toolkit)].count("fasterq-dump") == 2  # --version + check


async def test_toolkit_disk_limit(svc, toolkit):
    control(toolkit, fasterq="disk_limit")
    res = await run_job(svc)
    assert res["data"]["transfer"]["error"]["code"] == "budget_exceeded"
    assert list((job_dir(svc, res) / "fastq").glob("*")) == []


async def test_not_enough_disk_space_is_refused_before_starting(svc, toolkit, monkeypatch):
    real = shutil.disk_usage
    monkeypatch.setattr(sra_mod.shutil, "disk_usage", lambda p: real(p)._replace(free=1024))
    res = await convert(svc)
    assert res["error"]["code"] == "budget_exceeded"
    assert res["error"]["details"]["free_bytes"] == 1024
    assert [c for c in calls(toolkit) if c["args"] != ["--version"]] == []


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("truncated", "truncated record"),
        ("mismatch", "mate files disagree"),
        ("miscount", "do not match fasterq-dump's report"),
        ("dropped_bases", "do not match fasterq-dump's report"),
        ("no_report", "did not report how many reads"),
        ("extra_file", "not the expected split-3 set"),
        ("fail", "fasterq-dump failed"),
    ],
)
async def test_incomplete_or_inconsistent_output_is_never_completed(svc, toolkit, mode, message):
    extra = {"bio_bases": 40, "fasterq": "paired"} if mode == "dropped_bases" else {}
    control(toolkit, **{"fasterq": mode, "pairs": 3, "unpaired": 1, **extra})
    res = await run_job(svc)
    transfer = res["data"]["transfer"]
    assert transfer["state"] == "failed", transfer
    assert message in transfer["error"]["message"]
    assert res["data"]["artifacts"] == []
    assert not (svc.settings.paths.work_dir / "artifacts" / transfer["transfer_id"]).exists()
    root = job_dir(svc, res)
    assert not (root / "fastq").exists() or list((root / "fastq").glob("*")) == []


def fastq_dir(tmp_path, **files: str) -> Path:
    d = tmp_path / "fq"
    d.mkdir()
    for name, text in files.items():
        (d / name.replace("R", ACC)).write_text(text)
    return d


def rec(spot, seq="ACGT", name=None):
    name = name or f"{ACC}.{spot}"
    return f"@{name} {spot} length={len(seq)}\n{seq}\n+{name} {spot} length={len(seq)}\n{'I' * len(seq)}\n"


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"R.fastq": f"@{ACC}.1 1 length=4\nACGT\n+\nIII"}, "truncated record"),
        ({"R.fastq": rec(1, name="SRR9.1")}, "not <accession>.<spot>"),
        ({"R.fastq": rec(1, name=f"{ACC}.x")}, "not <accession>.<spot>"),
        ({"R.fastq": rec(1).replace("length=4", "length=5")}, "length= does not match"),
        ({"R.fastq": rec(9)}, "spot number outside the run"),
        # The same spot twice in both mate files: counts and mate names still agree.
        ({"R_1.fastq": rec(1) * 2, "R_2.fastq": rec(1) * 2}, "repeat or are out of order"),
        ({"R_1.fastq": rec(2) + rec(1), "R_2.fastq": rec(2) + rec(1)}, "out of order"),
        ({"R_1.fastq": rec(1), "R_2.fastq": rec(1), "R.fastq": rec(1)}, "repeat"),
        ({"R_1.fastq": rec(1)}, "not the expected split-3 set"),
    ],
)
def test_verifier_rejects_malformed_or_duplicated_reads(tmp_path, files, message):
    with pytest.raises(Exception, match=message):
        sra_mod.verify_fastq(fastq_dir(tmp_path, **files), ACC, 3, lambda: False)


def test_verifier_accepts_interleaved_pairs_and_unpaired_spots(tmp_path):
    d = fastq_dir(
        tmp_path,
        **{"R_1.fastq": rec(1) + rec(3), "R_2.fastq": rec(1) + rec(3), "R.fastq": rec(2, "GG")},
    )
    got = {
        f["role"]: (f["reads"], f["bases"]) for f in sra_mod.verify_fastq(d, ACC, 3, lambda: False)
    }
    assert got == {"mate_1": (2, 8), "mate_2": (2, 8), "unpaired": (1, 2)}


def test_verifier_bounds_line_length(tmp_path, monkeypatch):
    monkeypatch.setattr(sra_mod, "MAX_LINE_BYTES", 16)
    d = fastq_dir(tmp_path, **{"R.fastq": rec(1, "A" * 40)})
    with pytest.raises(Exception, match="longer than 16 bytes"):
        sra_mod.verify_fastq(d, ACC, 3, lambda: False)


async def test_unsupported_sequence_table_is_refused_before_conversion(svc, toolkit):
    control(toolkit, table="CONSENSUS")
    res = await run_job(svc)
    assert res["data"]["transfer"]["error"]["code"] == "unsupported"
    assert "CONSENSUS" in res["data"]["transfer"]["error"]["message"]
    assert [c["tool"] for c in calls(toolkit)].count("fasterq-dump") == 2  # --version + check


async def test_descendants_that_outlive_the_toolkit_are_stopped(svc, toolkit):
    control(toolkit, prefetch="orphan")
    res = await run_job(svc)
    assert res["data"]["transfer"]["state"] == "completed"
    await asyncio.sleep(0.2)
    assert not pid_alive(int((toolkit / "grandchild.pid").read_text()))


async def test_resumed_job_reserves_its_budget_minus_what_is_on_disk(tmp_path, toolkit):
    control(toolkit, fasterq="truncated", pairs=2000, run_bytes=50_000)
    settings = make_settings(
        tmp_path,
        [],
        sra_toolkit={"bin_dir": str(toolkit)},
        limits={"workspace_max_bytes": 3_000_000},
    )
    svc = GenomicsService(settings)
    try:
        failed = await run_job(svc, budget_bytes=1_000_000)
        assert failed["data"]["transfer"]["state"] == "failed"
        tm = svc.registry.component("transfers").manager
        job = tm.get(failed["data"]["transfer"]["transfer_id"])
        root = job_dir(svc, failed)
        kept = sra_mod.dir_usage(root)
        # Cleanup of the failed conversion is reflected in the job's accounting.
        assert job.parts[0].done == kept and kept < 100_000
        # Only the download is kept, and the error is retryable: resume it with a longer run.
        control(toolkit, fasterq="hang", run_bytes=50_000)
        job.error["retryable"] = True
        job.no_resume = False
        res = await convert(svc, budget_bytes=1_000_000)
        assert res["data"]["transfer"]["transfer_id"] == job.transfer_id
        assert tm.reserved_bytes() == 1_000_000 - job.parts[0].done
        # An ordinary transfer is refused if it would not fit next to the reservation.
        used = sra_mod.dir_usage(svc.settings.paths.work_dir)
        with pytest.raises(Exception, match="quota"):
            tm.check_quota(3_000_000 - used - tm.reserved_bytes() + 1)
        tm.check_quota(3_000_000 - used - tm.reserved_bytes())
        await svc.call("cancel_transfer", {"transfer_id": job.transfer_id})
    finally:
        await svc.aclose()


async def test_timeout_during_verification_waits_for_the_reader(svc, toolkit, monkeypatch):
    seen = {}

    def slow(directory, acc, spots, stop):
        while not stop():
            time.sleep(0.01)
        time.sleep(0.3)
        seen["files_present_when_reader_stopped"] = (directory / f"{acc}_1.fastq").exists()
        return []

    monkeypatch.setattr(sra_mod, "verify_fastq", slow)
    res = await run_job(svc, timeout_s=1.5)
    assert res["data"]["transfer"]["error"]["code"] == "timeout"
    assert seen == {"files_present_when_reader_stopped": True}
    assert list((job_dir(svc, res) / "fastq").glob("*")) == []


async def test_process_group_is_killed_after_its_leader_exits(tmp_path):
    from genomics_mcp.storage.native import kill_process_group

    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n"
    )
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", code, start_new_session=True)
    await proc.wait()
    child = int(pid_file.read_text())
    assert pid_alive(child)
    kill_process_group(proc)
    for _ in range(50):
        if not pid_alive(child):
            break
        await asyncio.sleep(0.02)
    assert not pid_alive(child)


async def test_resume_passes_the_whole_budget_as_prefetch_max_size(svc, toolkit):
    control(toolkit, prefetch="interrupt", run_bytes=400_000)
    await run_job(svc, budget_bytes=1_000_000)
    control(toolkit, prefetch="ok", run_bytes=400_000)
    done = await run_job(svc, budget_bytes=1_000_000)
    assert done["data"]["transfer"]["state"] == "completed"
    sizes = [
        c["args"][c["args"].index("--max-size") + 1]
        for c in calls(toolkit)
        if "--max-size" in c["args"]
    ]
    assert sizes == [str(1_000_000 // 1024)] * 2


async def test_stopping_the_mcp_server_kills_the_toolkit_and_keeps_a_resumable_job(
    tmp_path, toolkit
):
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    control(toolkit, prefetch="hang")
    work = tmp_path / "work"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GENOMICS_MCP_WORK_DIR": str(work),
        "GENOMICS_MCP_SRA_TOOLKIT_DIR": str(toolkit),
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "genomics_mcp"], env=env)
    with (tmp_path / "stderr.log").open("w") as errlog:
        async with Client(stdio_client(params, errlog=errlog), mode="legacy") as client:
            res = await client.call_tool("convert_sra_run", {"accession": ACC})
            tid = res.structured_content["data"]["transfer"]["transfer_id"]
            for _ in range(200):
                if (toolkit / "grandchild.pid").exists():
                    break
                await asyncio.sleep(0.05)
    grandchild = int((toolkit / "grandchild.pid").read_text())
    for _ in range(100):
        if not pid_alive(grandchild):
            break
        await asyncio.sleep(0.05)
    assert not pid_alive(grandchild)
    job = json.loads((work / "transfers" / tid / "job.json").read_text())
    assert job["state"] == "failed" and job["kind"] == "sra"
    assert "convert_sra_run again" in job["error"]["message"]
    assert job["error"]["retryable"] is True
