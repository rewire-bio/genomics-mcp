# ruff: noqa: ASYNC240  (tests read finished artifacts from disk)
"""fetch_file / get_transfer_status / cancel_transfer: budgets, quota, resume, checksums, cancel."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path

import pytest
from gm_test_support import FixtureServer, envelope, iv, make_settings

from genomics_mcp.artifacts import fetch as fetch_mod
from genomics_mcp.artifacts.transfers import safe_name
from genomics_mcp.service import GenomicsService


@pytest.fixture
def srv_root(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    return root


@pytest.fixture
def srv(srv_root):
    s = FixtureServer(srv_root)
    yield s
    s.close()


def blob(path: Path, n: int, seed: int = 1) -> bytes:
    data = hashlib.sha256(str(seed).encode()).digest() * (n // 32 + 1)
    path.write_bytes(data[:n])
    return data[:n]


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


async def fetch(svc, **args):
    return envelope(await svc.call("fetch_file", args))


async def status(svc, tid):
    return envelope(await svc.call("get_transfer_status", {"transfer_id": tid}))


async def wait_done(svc, tid, seconds=20.0):
    for _ in range(int(seconds / 0.05)):
        res = await status(svc, tid)
        if res["data"]["transfer"]["state"] in ("completed", "failed", "cancelled"):
            return res
        await asyncio.sleep(0.05)
    raise AssertionError("transfer did not finish")


@pytest.fixture
async def svc(tmp_path, golden):
    s = GenomicsService(make_settings(tmp_path, [golden["root"]]))
    yield s
    await s.aclose()


def parts_dir(svc) -> Path:
    return svc.settings.paths.work_dir / "transfers"


async def test_remote_fetch_with_index_and_checksums(svc, fixture_server, golden):
    data = golden["bam"].read_bytes()
    res = await fetch(
        svc,
        file={
            "uri": fixture_server.url("golden.bam"),
            "checksums": [{"algorithm": "sha256", "value": sha256(data)}],
        },
    )
    t = res["data"]["transfer"]
    assert t["state"] == "completed", t
    art = t["artifact"]
    assert Path(art["path"]).read_bytes() == data
    assert Path(art["index_path"]).name == "golden.bam.bai"
    assert art["checksum_verified"] is True
    assert t["bytes_done"] == len(data) + (golden["root"] / "golden.bam.bai").stat().st_size
    assert res["data"]["artifact_file"]["readiness"]["state"] == "ready"
    assert not list(parts_dir(svc).rglob("*.part"))
    # The artifact is directly usable by the readers.
    reads = envelope(
        await svc.call(
            "get_reads", {"file": {"uri": art["path"]}, "interval": iv("chrG", 100, 120)}
        )
    )
    assert reads["status"] == "ok" and reads["data"]["records"]
    assert "binary" not in str(res) and len(str(res)) < 10_000


async def test_default_budget_and_ceiling(tmp_path, golden, fixture_server):
    size = golden["bam"].stat().st_size
    svc = GenomicsService(
        make_settings(
            tmp_path,
            [golden["root"]],
            limits={"max_transfer_bytes": 100, "transfer_budget_ceiling_bytes": size + 10_000},
        )
    )
    try:
        refused = await fetch(svc, file={"uri": fixture_server.url("golden.bam")})
        assert refused["error"]["code"] == "budget_exceeded"
        assert "budget_bytes" in refused["error"]["hint"]
        over = await fetch(svc, file={"uri": fixture_server.url("golden.bam")}, budget_bytes=10**9)
        assert over["error"]["code"] == "budget_exceeded" and "ceiling" in over["error"]["message"]
        ok = await fetch(
            svc, file={"uri": fixture_server.url("golden.bam")}, budget_bytes=size + 10_000
        )
        assert ok["data"]["transfer"]["state"] == "completed"
        # Index bytes share the budget: data fits, data + index does not.
        tight = await fetch(
            svc, file={"uri": fixture_server.url("redirect/golden.bam")}, budget_bytes=size + 5
        )
        done = await wait_done(svc, tight["data"]["transfer"]["transfer_id"])
        assert done["data"]["transfer"]["state"] == "failed"
        assert done["data"]["transfer"]["error"]["code"] == "budget_exceeded"
    finally:
        await svc.aclose()


async def test_workspace_quota(tmp_path, golden, fixture_server):
    svc = GenomicsService(
        make_settings(tmp_path, [golden["root"]], limits={"workspace_max_bytes": 1000})
    )
    try:
        res = await fetch(svc, file={"uri": fixture_server.url("golden.bam")})
        assert res["error"]["code"] == "budget_exceeded" and "quota" in res["error"]["message"]
    finally:
        await svc.aclose()


async def test_resume_after_interruption_uses_if_range(svc, srv, srv_root):
    data = blob(srv_root / "big.bin", 3_000_000)
    srv.drop_once["/big.bin"] = 1_000_000
    first = await fetch(svc, file={"uri": srv.url("big.bin"), "format": "other"})
    tid = first["data"]["transfer"]["transfer_id"]
    failed = await wait_done(svc, tid)
    t = failed["data"]["transfer"]
    assert t["state"] == "failed" and t["resumable"] is True and t["artifact"] is None
    assert 0 < t["bytes_done"] < len(data)
    second = await fetch(
        svc,
        file={
            "uri": srv.url("big.bin"),
            "format": "other",
            "checksums": [{"algorithm": "sha256", "value": sha256(data)}],
        },
    )
    done = await wait_done(svc, second["data"]["transfer"]["transfer_id"])
    t2 = done["data"]["transfer"]
    assert t2["transfer_id"] == tid and t2["state"] == "completed"
    assert Path(t2["artifact"]["path"]).read_bytes() == data
    resume = [
        e
        for e in srv.log
        if e["headers"].get("Range", "").endswith("-") and e["headers"]["Range"] != "bytes=0-0"
    ]
    assert resume and resume[-1]["headers"]["Range"] == f"bytes={t['bytes_done']}-"
    assert resume[-1]["headers"]["If-Range"].startswith('"')
    assert "resuming an interrupted transfer" in done["data"]["notes"]


async def test_resume_answered_with_200_restarts_from_zero(svc, srv, srv_root):
    data = blob(srv_root / "big.bin", 2_000_000, seed=2)
    srv.drop_once["/big.bin"] = 500_000
    first = await fetch(svc, file={"uri": srv.url("big.bin"), "format": "other"})
    await wait_done(svc, first["data"]["transfer"]["transfer_id"])
    srv.ignore_range_once.add("/big.bin")
    second = await fetch(svc, file={"uri": srv.url("big.bin"), "format": "other"})
    done = await wait_done(svc, second["data"]["transfer"]["transfer_id"])
    assert done["data"]["transfer"]["state"] == "completed"
    assert Path(done["data"]["transfer"]["artifact"]["path"]).read_bytes() == data
    assert any("whole object" in n for n in done["data"]["notes"])


async def test_changed_object_is_not_spliced(svc, srv, srv_root):
    blob(srv_root / "obj.bin", 2_000_000, seed=3)
    srv.drop_once["/obj.bin"] = 700_000
    first = await fetch(svc, file={"uri": srv.url("obj.bin"), "format": "other"})
    await wait_done(svc, first["data"]["transfer"]["transfer_id"])
    new = blob(srv_root / "obj.bin", 2_000_000, seed=4)  # same size, new content and ETag
    second = await fetch(svc, file={"uri": srv.url("obj.bin"), "format": "other"})
    done = await wait_done(svc, second["data"]["transfer"]["transfer_id"])
    assert done["data"]["transfer"]["state"] == "completed"
    assert Path(done["data"]["transfer"]["artifact"]["path"]).read_bytes() == new
    assert done["data"]["transfer"]["transfer_id"] != first["data"]["transfer"]["transfer_id"]


async def test_checksum_mismatch_fails_and_removes_bytes(svc, fixture_server):
    res = await fetch(
        svc,
        file={
            "uri": fixture_server.url("features.bed"),
            "checksums": [{"algorithm": "md5", "value": "0" * 32}],
        },
    )
    done = await wait_done(svc, res["data"]["transfer"]["transfer_id"])
    t = done["data"]["transfer"]
    assert t["state"] == "failed" and t["error"]["code"] == "upstream_error"
    assert t["artifact"] is None and t["resumable"] is False
    art_dir = svc.settings.paths.work_dir / "artifacts" / t["transfer_id"]
    assert not art_dir.exists() or not any(art_dir.iterdir())


async def test_cancel_stops_background_work(svc, srv, srv_root, monkeypatch):
    monkeypatch.setattr(fetch_mod, "WAIT_FOR_COMPLETION_S", 0.2)
    blob(srv_root / "slow.bin", 4_000_000, seed=5)
    res = await fetch(svc, file={"uri": srv.url("slow/slow.bin"), "format": "other"})
    tid = res["data"]["transfer"]["transfer_id"]
    assert res["data"]["transfer"]["state"] in ("queued", "running")
    await asyncio.sleep(0.3)
    cancelled = envelope(await svc.call("cancel_transfer", {"transfer_id": tid}))
    assert cancelled["data"]["transfer"]["state"] == "cancelled"
    await asyncio.sleep(0.5)
    later = await status(svc, tid)
    assert later["data"]["transfer"]["state"] == "cancelled"
    assert later["data"]["transfer"]["artifact"] is None
    assert not list((parts_dir(svc) / tid).glob("*.part"))
    sent = srv.bytes_sent.get("/slow/slow.bin", 0)
    await asyncio.sleep(0.3)
    assert srv.bytes_sent.get("/slow/slow.bin", 0) == sent  # nothing more was pulled


async def test_restart_resumes_from_persisted_state(tmp_path, golden, srv, srv_root, monkeypatch):
    monkeypatch.setattr(fetch_mod, "WAIT_FOR_COMPLETION_S", 0.2)
    data = blob(srv_root / "slow.bin", 600_000, seed=6)
    settings = make_settings(tmp_path, [golden["root"]])
    a = GenomicsService(settings)
    res = await fetch(a, file={"uri": srv.url("slow/slow.bin"), "format": "other"})
    tid = res["data"]["transfer"]["transfer_id"]
    await asyncio.sleep(0.6)
    await a.aclose()  # server stops mid-transfer
    record = (settings.paths.work_dir / "transfers" / tid / "job.json").read_text()
    assert "slow.bin" in record and "X-Amz" not in record
    b = GenomicsService(make_settings(tmp_path, [golden["root"]]))
    try:
        st = await status(b, tid)
        assert st["data"]["transfer"]["state"] == "failed" and st["data"]["transfer"]["resumable"]
        done_before = st["data"]["transfer"]["bytes_done"]
        assert done_before > 0
        again = await fetch(b, file={"uri": srv.url("slow/slow.bin"), "format": "other"})
        done = await wait_done(b, again["data"]["transfer"]["transfer_id"], seconds=30)
        assert done["data"]["transfer"]["transfer_id"] == tid
        assert Path(done["data"]["transfer"]["artifact"]["path"]).read_bytes() == data
    finally:
        await b.aclose()


async def test_prepare_local_fasta_never_touches_source(svc, golden):
    src = golden["fasta_unindexed"]
    before = (src.stat().st_mtime_ns, src.read_bytes())
    res = await fetch(svc, file={"uri": str(src)}, prepare=True)
    done = await wait_done(svc, res["data"]["transfer"]["transfer_id"])
    t = done["data"]["transfer"]
    assert t["state"] == "completed", t
    art = t["artifact"]
    assert Path(art["path"]).parent.parent == svc.settings.paths.work_dir / "artifacts"
    assert art["index_path"].endswith(".fai")
    assert "built .fai with samtools faidx" in done["data"]["preparation"]["steps"]
    assert (src.stat().st_mtime_ns, src.read_bytes()) == before
    assert not Path(str(src) + ".fai").exists()
    seq = envelope(
        await svc.call(
            "get_sequence", {"file": {"uri": art["path"]}, "interval": iv("m1", 0, 8, "x")}
        )
    )
    assert seq["data"]["records"][0]["sequence"] == "ACGTacgt"


async def test_prepare_ordinary_gzip_fasta_and_bed(svc, fixture_server, golden):
    res = await fetch(svc, file={"uri": fixture_server.url("ordinary.fa.gz")}, prepare=True)
    done = await wait_done(svc, res["data"]["transfer"]["transfer_id"])
    steps = done["data"]["preparation"]["steps"]
    assert steps[0] == "recompressed ordinary gzip to BGZF" and "built .gzi" in steps
    path = done["data"]["transfer"]["artifact"]["path"]
    seq = envelope(
        await svc.call("get_sequence", {"file": {"uri": path}, "interval": iv("m1", 4, 8, "x")})
    )
    assert seq["data"]["records"][0]["sequence"] == "acgt"
    bed = await fetch(svc, file={"uri": str(golden["bed_gzip"])}, prepare=True)
    bdone = await wait_done(svc, bed["data"]["transfer"]["transfer_id"])
    feats = envelope(
        await svc.call(
            "get_features",
            {
                "file": {"uri": bdone["data"]["transfer"]["artifact"]["path"]},
                "interval": iv("chrG", 0, 30),
            },
        )
    )
    assert [r["name"] for r in feats["data"]["records"]] == ["featA", "featB"]


async def test_local_file_is_not_copied(svc, golden):
    res = await fetch(svc, file={"uri": str(golden["bam"])})
    done = await wait_done(svc, res["data"]["transfer"]["transfer_id"])
    art = done["data"]["transfer"]["artifact"]
    assert art["path"] == str(golden["bam"]) and art["index_path"] == str(golden["bam"]) + ".bai"
    assert "the file is already local; it was not copied" in done["data"]["notes"]


async def test_unknown_transfer_ids(svc):
    for tid in ("0" * 32, "../../etc/passwd"):
        res = envelope(await svc.call("get_transfer_status", {"transfer_id": tid}))
        assert res["error"]["code"] == "not_found"


def test_safe_names():
    for bad in ("../x", "..", "/abs/path", "a/../../b", "\\..\\win", ".hidden"):
        out = safe_name(bad)
        assert "/" not in out and "\\" not in out and not out.startswith(".")
    assert safe_name("") == "file"


async def test_url_path_traversal_stays_in_artifact_dir(svc, srv, srv_root):
    (srv_root / "sub").mkdir()
    blob(srv_root / "sub" / "x.bin", 1000)
    os.symlink(srv_root / "sub" / "x.bin", srv_root / "..evil.bin")
    res = await fetch(svc, file={"uri": srv.url("..evil.bin"), "format": "other"})
    done = await wait_done(svc, res["data"]["transfer"]["transfer_id"])
    path = Path(done["data"]["transfer"]["artifact"]["path"])
    assert (
        path.parent
        == svc.settings.paths.work_dir / "artifacts" / done["data"]["transfer"]["transfer_id"]
    )
    assert not path.name.startswith(".")
    shutil.rmtree(srv_root / "sub")


# --------------------------------------------------------------------------- review regressions


async def test_reuse_honours_requested_verification(svc, golden, tmp_path):
    src = golden["root"] / "reuse.fa"
    src.write_text(">r\nACGT\n")
    wrong = {"uri": str(src), "checksums": [{"algorithm": "sha256", "value": "0" * 64}]}
    first = await fetch(svc, file=wrong, verify_checksum=False, include_index=False, prepare=True)
    done = await wait_done(svc, first["data"]["transfer"]["transfer_id"])
    assert done["data"]["transfer"]["state"] == "completed"
    second = await fetch(svc, file=wrong, verify_checksum=True, include_index=False, prepare=True)
    t2 = (await wait_done(svc, second["data"]["transfer"]["transfer_id"]))["data"]["transfer"]
    assert t2["transfer_id"] != done["data"]["transfer"]["transfer_id"]
    assert t2["state"] == "failed" and "checksum mismatch" in t2["error"]["message"]
    good = {
        "uri": str(src),
        "checksums": [{"algorithm": "sha256", "value": sha256(src.read_bytes())}],
    }
    third = await fetch(svc, file=good, verify_checksum=True, include_index=False, prepare=True)
    t3 = await wait_done(svc, third["data"]["transfer"]["transfer_id"])
    assert t3["data"]["transfer"]["state"] == "completed"
    assert t3["data"]["source_verification"]["verified"] is True


async def test_same_size_local_mutation_is_not_reused(svc, golden):
    src = golden["root"] / "mutate.fa"
    src.write_text(">m\nAAAA\n")
    a = await fetch(svc, file={"uri": str(src)}, prepare=True)
    da = await wait_done(svc, a["data"]["transfer"]["transfer_id"])
    await asyncio.sleep(0.01)
    src.write_text(">m\nCCCC\n")  # same size, new content
    b = await fetch(svc, file={"uri": str(src)}, prepare=True)
    db = await wait_done(svc, b["data"]["transfer"]["transfer_id"])
    assert db["data"]["transfer"]["transfer_id"] != da["data"]["transfer"]["transfer_id"]
    assert Path(db["data"]["transfer"]["artifact"]["path"]).read_text() == ">m\nCCCC\n"
    c = await fetch(svc, file={"uri": str(src)}, prepare=True)
    assert c["data"]["transfer"]["transfer_id"] == db["data"]["transfer"]["transfer_id"]
    assert "identity unchanged" in c["data"]["notes"][-1]


async def test_preparation_respects_workspace_quota(tmp_path, golden):
    import gzip as gz

    src = golden["root"] / "many_contigs.fa.gz"
    src.write_bytes(gz.compress("".join(f">c{i}\nA\n" for i in range(5000)).encode()))
    quota = 65_536
    svc = GenomicsService(
        make_settings(tmp_path, [golden["root"]], limits={"workspace_max_bytes": quota})
    )
    try:
        res = await fetch(svc, file={"uri": str(src)}, prepare=True)
        done = await wait_done(svc, res["data"]["transfer"]["transfer_id"])
        t = done["data"]["transfer"]
        assert t["state"] == "failed" and t["error"]["code"] == "budget_exceeded", t["error"]
        from genomics_mcp.artifacts.transfers import dir_usage

        assert dir_usage(svc.settings.paths.work_dir) <= quota
        assert not (svc.settings.paths.work_dir / "artifacts" / t["transfer_id"]).exists()
    finally:
        await svc.aclose()


async def test_cancel_during_preparation_removes_staged_bytes(svc, golden, monkeypatch):
    monkeypatch.setattr(fetch_mod, "WAIT_FOR_COMPLETION_S", 0.2)
    monkeypatch.setattr(fetch_mod, "PREPARE_TASK", "genomics_mcp.storage._selftest:sleep")
    src = golden["root"] / "cancelprep.fa"
    src.write_text(">c\nACGT\n")
    before = src.read_bytes()
    res = await fetch(svc, file={"uri": str(src)}, prepare=True)
    tid = res["data"]["transfer"]["transfer_id"]
    art_dir = svc.settings.paths.work_dir / "artifacts" / tid
    for _ in range(100):
        if art_dir.exists() and any(art_dir.iterdir()):
            break
        await asyncio.sleep(0.05)
    assert any(art_dir.iterdir())  # staged copy finalized, preparation running
    out = envelope(await svc.call("cancel_transfer", {"transfer_id": tid}))
    assert out["data"]["transfer"]["state"] == "cancelled"
    assert not art_dir.exists()
    assert src.read_bytes() == before


async def test_recompressed_artifact_reports_its_own_checksums(svc, fixture_server, golden):
    raw = golden["fasta_gzip"].read_bytes()
    res = await fetch(
        svc,
        file={
            "uri": fixture_server.url("ordinary.fa.gz"),
            "checksums": [{"algorithm": "md5", "value": hashlib.md5(raw).hexdigest()}],
        },
        prepare=True,
    )
    done = await wait_done(svc, res["data"]["transfer"]["transfer_id"])
    art = done["data"]["transfer"]["artifact"]
    data = Path(art["path"]).read_bytes()
    sums = {c["algorithm"]: c["value"] for c in art["checksums"]}
    assert sums["md5"] == hashlib.md5(data).hexdigest() != hashlib.md5(raw).hexdigest()
    assert sums["sha256"] == sha256(data)
    assert art["checksum_verified"] is False
    assert done["data"]["source_verification"]["verified"] is True
    assert done["data"]["source_verification"]["checksums"]["md5"] == hashlib.md5(raw).hexdigest()


async def test_running_job_is_not_shared_with_stricter_verification(
    svc, srv, srv_root, monkeypatch
):
    monkeypatch.setattr(fetch_mod, "WAIT_FOR_COMPLETION_S", 0.1)
    data = blob(srv_root / "held.bin", 300_000, seed=9)
    wrong = [{"algorithm": "sha256", "value": "0" * 64}]
    f = {"uri": srv.url("slow/held.bin"), "format": "other", "checksums": wrong}
    first = await fetch(svc, file=f, verify_checksum=False)
    second = await fetch(svc, file=f, verify_checksum=True)
    assert second["data"]["transfer"]["transfer_id"] != first["data"]["transfer"]["transfer_id"]
    a = await wait_done(svc, first["data"]["transfer"]["transfer_id"], seconds=30)
    b = await wait_done(svc, second["data"]["transfer"]["transfer_id"], seconds=30)
    assert a["data"]["transfer"]["state"] == "completed"
    assert Path(a["data"]["transfer"]["artifact"]["path"]).read_bytes() == data  # untouched
    assert b["data"]["transfer"]["state"] == "failed"
    assert "checksum mismatch" in b["data"]["transfer"]["error"]["message"]
    good = {**f, "checksums": [{"algorithm": "sha256", "value": sha256(data)}]}
    c = await fetch(svc, file=good, verify_checksum=True)
    c = await wait_done(svc, c["data"]["transfer"]["transfer_id"], seconds=30)
    assert c["data"]["source_verification"]["verified"] is True


async def test_local_no_copy_reserves_nothing(tmp_path, golden):
    svc = GenomicsService(
        make_settings(tmp_path, [golden["root"]], limits={"workspace_max_bytes": 4 * 1024 * 1024})
    )
    try:
        res = await fetch(svc, file={"uri": str(golden["ref"])})
        done = await wait_done(svc, res["data"]["transfer"]["transfer_id"])
        assert done["data"]["transfer"]["state"] == "completed"
    finally:
        await svc.aclose()


async def test_bgzf_recompression_at_quota_is_budget_exceeded(tmp_path, golden):
    import gzip as gz
    import random

    rng = random.Random(5)
    text = "".join(
        f">c{i}\n{''.join(rng.choice('ACGT') for _ in range(200))}\n" for i in range(5000)
    )
    src = golden["root"] / "random_contigs.fa.gz"
    src.write_bytes(gz.compress(text.encode()))
    quota = src.stat().st_size + 85_000
    svc = GenomicsService(
        make_settings(tmp_path, [golden["root"]], limits={"workspace_max_bytes": quota})
    )
    try:
        res = await fetch(svc, file={"uri": str(src)}, prepare=True)
        done = await wait_done(svc, res["data"]["transfer"]["transfer_id"], seconds=60)
        t = done["data"]["transfer"]
        assert t["state"] == "failed" and t["error"]["code"] == "budget_exceeded", t["error"]
        from genomics_mcp.artifacts.transfers import dir_usage

        assert dir_usage(svc.settings.paths.work_dir) <= quota
        assert src.read_bytes() == gz.compress(text.encode()) or src.exists()
    finally:
        await svc.aclose()


async def test_remote_index_staging_respects_quota(tmp_path, srv, srv_root):
    import pysam

    fa = srv_root / "many.fa"
    fa.write_text("".join(f">contig_{i}\nA\n" for i in range(5000)))
    pysam.faidx(str(fa))
    assert (srv_root / "many.fa.fai").stat().st_size > 65_536
    small = srv_root / "small.fa"
    small.write_text(">s\nACGT\n")
    pysam.faidx(str(small))
    svc = GenomicsService(make_settings(tmp_path, [], limits={"workspace_max_bytes": 65_536}))
    try:
        big = envelope(
            await svc.call(
                "get_sequence",
                {"file": {"uri": srv.url("many.fa")}, "interval": iv("contig_1", 0, 1, "x")},
            )
        )
        assert big["error"]["code"] == "budget_exceeded", big["error"]
        staged = svc.settings.paths.work_dir / ".isolation" / "staged"
        assert not staged.exists() or not any(staged.iterdir())
        ok = envelope(
            await svc.call(
                "get_sequence",
                {"file": {"uri": srv.url("small.fa")}, "interval": iv("s", 0, 4, "x")},
            )
        )
        assert ok["data"]["records"][0]["sequence"] == "ACGT"
    finally:
        await svc.aclose()


class SlowPreparer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = asyncio.Event()

    async def prepare(self, accession, ctx, *, budget_bytes=None):
        self.started.set()
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")


async def test_ncbi_preparation_is_a_managed_cancellable_job(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "WAIT_FOR_COMPLETION_S", 0.05)
    prep = SlowPreparer(tmp_path / "x.fa")
    svc.registry._components["preparer:ncbi_genome_fasta"] = prep
    pkg = {
        "uri": "https://api.ncbi.nlm.nih.gov/datasets/v2/genome/accession/GCF_000819615.1/download"
        "?include_annotation_type=GENOME_FASTA",
        "source": "ncbi_datasets",
        "accession": "GCF_000819615.1",
        "format": "other",
        "native": {"annotation_type": "GENOME_FASTA"},
    }
    res = await fetch(svc, file=pkg, prepare=True)
    tid = res["data"]["transfer"]["transfer_id"]
    assert res["data"]["transfer"]["state"] in ("queued", "running")
    await asyncio.wait_for(prep.started.wait(), 5)
    tm = svc.registry.component("transfers").manager
    assert tm.reserved_bytes() >= svc.settings.limits.max_transfer_bytes
    out = envelope(await svc.call("cancel_transfer", {"transfer_id": tid}))
    assert out["data"]["transfer"]["state"] == "cancelled"
    await asyncio.sleep(0.2)
    assert (await status(svc, tid))["data"]["transfer"]["state"] == "cancelled"
