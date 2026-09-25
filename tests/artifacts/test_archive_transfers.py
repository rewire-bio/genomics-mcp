"""fetch_file through archive transfer backends and preparers (EGA, NCBI).

Offline tests use a fake backend that behaves like EGA's plain stream: a range spanning the
whole object is refused, ranges starting inside a cipher block return wrong bytes, and a
stream can drop mid-way. Live tests (GENOMICS_MCP_NETWORK_TESTS=1) use EGA's documented
public test account, ENA and NCBI Datasets.
"""

# ruff: noqa: ASYNC240  (tests read finished artifacts from disk)
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from gm_test_support import NETWORK, envelope, make_settings

from genomics_mcp.archives.ega.integration import TransferDescription
from genomics_mcp.errors import UpstreamError
from genomics_mcp.models import Checksum, FileRef
from genomics_mcp.service import GenomicsService

DATA = hashlib.sha256(b"seed").digest() * 20_000  # 640,000 bytes


class FakeEgaBackend:
    scheme = "ega"

    def __init__(self, data: bytes = DATA) -> None:
        self.data = data
        self.calls: list[tuple[int, int]] = []
        self.drop_after: int | None = None

    async def describe(self, file, ctx):
        return TransferDescription(
            file=file,
            size_bytes=len(self.data),
            checksums=[Checksum(algorithm="md5", value=hashlib.md5(self.data).hexdigest())],
            suggested_name="EGAF00000000009.bam",
        )

    @asynccontextmanager
    async def open(self, file, ctx, *, start=0, end=None):
        self.calls.append((start, end))
        if start == 0 and end == len(self.data):
            raise UpstreamError("ega: server ignored the Range request (HTTP 200)")
        chunk = self.data[start:end] if start % 16 == 0 else b"\0" * (end - start)

        async def body():
            sent = 0
            for i in range(0, len(chunk), 8192):
                piece = chunk[i : i + 8192]
                if self.drop_after is not None and sent + len(piece) > self.drop_after:
                    self.drop_after = None
                    raise UpstreamError("connection dropped", retryable=True)
                sent += len(piece)
                yield piece

        yield body()


@pytest.fixture
async def ega_svc(tmp_path, golden):
    svc = GenomicsService(make_settings(tmp_path, [golden["root"]]))
    backend = FakeEgaBackend()
    svc.registry._components["transfer_backend:ega"] = backend
    yield svc, backend
    await svc.aclose()


async def _wait(svc, tid):
    for _ in range(1200):
        res = envelope(await svc.call("get_transfer_status", {"transfer_id": tid}))
        if res["data"]["transfer"]["state"] in ("completed", "failed", "cancelled"):
            return res
        await asyncio.sleep(0.05)
    raise AssertionError("transfer did not finish")


async def test_backend_fetch_uses_aligned_ranges_and_verifies_md5(ega_svc):
    svc, backend = ega_svc
    res = envelope(await svc.call("fetch_file", {"file": {"uri": "ega://EGAF00000000009"}}))
    done = await _wait(svc, res["data"]["transfer"]["transfer_id"])
    t = done["data"]["transfer"]
    assert t["state"] == "completed", t["error"]
    assert Path(t["artifact"]["path"]).read_bytes() == DATA
    assert Path(t["artifact"]["path"]).name == "EGAF00000000009.bam"
    assert t["artifact"]["checksum_verified"] is True
    assert all(s % 65536 == 0 for s, _ in backend.calls)
    assert (0, len(DATA)) not in backend.calls


async def test_backend_resume_restarts_from_aligned_offset(ega_svc):
    svc, backend = ega_svc
    backend.drop_after = 100_003  # interrupted at an unaligned byte
    first = envelope(await svc.call("fetch_file", {"file": {"uri": "ega://EGAF00000000009"}}))
    failed = await _wait(svc, first["data"]["transfer"]["transfer_id"])
    assert failed["data"]["transfer"]["state"] == "failed"
    assert failed["data"]["transfer"]["resumable"] is True
    second = envelope(await svc.call("fetch_file", {"file": {"uri": "ega://EGAF00000000009"}}))
    done = await _wait(svc, second["data"]["transfer"]["transfer_id"])
    t = done["data"]["transfer"]
    assert t["transfer_id"] == failed["data"]["transfer"]["transfer_id"]
    assert t["state"] == "completed" and t["artifact"]["checksum_verified"] is True
    assert Path(t["artifact"]["path"]).read_bytes() == DATA
    resumed = backend.calls[1][0]
    assert resumed % 65536 == 0 and resumed > 0


async def test_backend_budget_is_enforced_before_streaming(tmp_path, golden):
    svc = GenomicsService(
        make_settings(tmp_path, [golden["root"]], limits={"max_transfer_bytes": 1000})
    )
    backend = FakeEgaBackend()
    svc.registry._components["transfer_backend:ega"] = backend
    try:
        res = envelope(await svc.call("fetch_file", {"file": {"uri": "ega://EGAF00000000009"}}))
        assert res["error"]["code"] == "budget_exceeded"
        assert backend.calls == []
    finally:
        await svc.aclose()


# --------------------------------------------------------------------------- live

live = pytest.mark.skipif(not NETWORK, reason="set GENOMICS_MCP_NETWORK_TESTS=1 for live sources")
EGAF = "EGAF00007243773"  # pyega3 functional-test BAM in EGAD00001003338 (public test account)
EGA_IV = {"contig": "chr10", "start": 10000, "end": 10050, "assembly": "GRCh38"}


@pytest.fixture
async def live_svc(tmp_path):
    svc = GenomicsService(
        make_settings(tmp_path, [], env={"GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT": "1"})
    )
    yield svc
    await svc.aclose()


@live
async def test_live_ega_region_reads_are_post_filtered(live_svc):
    res = envelope(
        await live_svc.call(
            "get_reads", {"file": {"uri": f"ega://{EGAF}", "format": "bam"}, "interval": EGA_IV}
        )
    )
    assert res["status"] == "ok", res.get("error")
    provider = res["data"]["region_slice"]["provider"]
    assert provider["records_received"] == 91 and provider["records_overlapping"] == 42
    assert len(res["data"]["records"]) == 42
    assert all(r["start"] < 10050 and r["end"] > 10000 for r in res["data"]["records"])
    pile = envelope(
        await live_svc.call(
            "get_pileup", {"file": {"uri": f"ega://{EGAF}", "format": "bam"}, "interval": EGA_IV}
        )
    )
    assert pile["status"] == "ok", pile.get("error")


@live
async def test_live_ega_fetch_file_through_backend(live_svc):
    res = envelope(
        await live_svc.call(
            "fetch_file", {"file": {"uri": f"ega://{EGAF}", "format": "bam"}, "prepare": True}
        )
    )
    done = await _wait(live_svc, res["data"]["transfer"]["transfer_id"])
    t = done["data"]["transfer"]
    assert t["state"] == "completed", t["error"]
    assert t["bytes_total"] == 194_821
    assert done["data"]["source_verification"]["verified"] is True
    assert (
        done["data"]["source_verification"]["checksums"]["md5"]
        == "ed365c71461eac21a64d2c29e7216e50"
    )
    import pysam

    with pysam.AlignmentFile(t["artifact"]["path"]) as f:
        assert sum(1 for _ in f.fetch(until_eof=True)) == 1097
    reads = envelope(
        await live_svc.call(
            "get_reads", {"file": done["data"]["artifact_file"], "interval": EGA_IV}
        )
    )
    assert len(reads["data"]["records"]) == 42


@live
async def test_live_ena_fasta_fetch_and_sequence(live_svc):
    files = envelope(await live_svc.call("list_files", {"source": "ena", "accession": "DQ285577"}))
    fasta = next(f for f in files["data"]["records"] if f.get("format") == "fasta")
    res = envelope(await live_svc.call("fetch_file", {"file": fasta, "prepare": True}))
    done = await _wait(live_svc, res["data"]["transfer"]["transfer_id"])
    assert done["data"]["transfer"]["state"] == "completed", done["data"]["transfer"]["error"]
    af = done["data"]["artifact_file"]
    contig = Path(af["index_uri"]).read_text().split("\t")[0]
    assert contig == "ENA|DQ285577|DQ285577.1"  # source-native name, not renamed
    seq = envelope(
        await live_svc.call(
            "get_sequence",
            {
                "file": af,
                "interval": {"contig": contig, "start": 0, "end": 30, "assembly": "DQ285577.1"},
            },
        )
    )
    assert seq["status"] == "ok" and len(seq["data"]["records"][0]["sequence"]) == 30


@live
async def test_live_ncbi_genome_preparation(live_svc):
    files = envelope(
        await live_svc.call(
            "list_files", {"source": "ncbi_datasets", "accession": "GCF_000819615.1"}
        )
    )
    pkg = next(
        f for f in files["data"]["records"] if f["native"].get("annotation_type") == "GENOME_FASTA"
    )
    res = envelope(await live_svc.call("fetch_file", {"file": pkg, "prepare": True}))
    assert res["status"] == "ok", res.get("error")
    af = res["data"]["artifact_file"]
    assert af["readiness"]["state"] == "ready"
    seq = envelope(
        await live_svc.call(
            "get_sequence",
            {
                "file": af,
                "interval": {
                    "contig": "NC_001422.1",
                    "start": 0,
                    "end": 20,
                    "assembly": "GCF_000819615.1",
                },
            },
        )
    )
    assert seq["data"]["records"][0]["sequence"] == "GAGTTTTATCGCTTCCATGA"


@live
async def test_live_ega_over_mcp_stdio(tmp_path):
    import os

    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    (tmp_path / "aws").write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GENOMICS_MCP_WORK_DIR": str(tmp_path / "work"),
        "GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT": "1",
        "AWS_CONFIG_FILE": str(tmp_path / "aws"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "aws"),
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "genomics_mcp"], env=env)
    with (tmp_path / "err.log").open("w") as err:
        async with Client(stdio_client(params, errlog=err), mode="legacy") as client:
            res = await client.call_tool(
                "get_reads", {"file": {"uri": f"ega://{EGAF}", "format": "bam"}, "interval": EGA_IV}
            )
            body = res.structured_content
            assert body["status"] == "ok", body.get("error")
            assert len(body["data"]["records"]) == 42
            assert "Bearer" not in json.dumps(body)


def test_fake_backend_matches_contract():
    from genomics_mcp.archives.ega.integration import TransferBackend

    assert isinstance(FakeEgaBackend(), TransferBackend)
    assert FileRef(uri="ega://EGAF00000000009").scheme == "ega"


@pytest.mark.parametrize("chunk", [7, 8 * 1024 * 1024])
def test_ncbi_fai_bound_is_never_below_faidx_output(tmp_path, monkeypatch, chunk):
    import random

    import pysam

    import genomics_mcp.catalogs.ncbi_datasets as ncbi

    monkeypatch.setattr(ncbi, "_SCAN_CHUNK", chunk)
    rng = random.Random(3)
    cases = [
        ">a desc\nACGT\n>bb\nAC\n",
        ">x\nA",
        "".join(f">contig_{i}\nA\n" for i in range(5000)),
        "".join(f">c{i} x\n" + "ACGT" * rng.randint(1, 9) + "\n" for i in range(300)),
    ]
    for text in cases:
        fa = tmp_path / "t.fa"
        fa.write_text(text)
        bound = ncbi._fai_upper_bound(fa)
        pysam.faidx(str(fa))
        assert (tmp_path / "t.fa.fai").stat().st_size <= bound
        (tmp_path / "t.fa.fai").unlink()
