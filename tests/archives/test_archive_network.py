"""Live E6 demos through GenomicsService with the real providers (GENOMICS_MCP_NETWORK_TESTS=1).

EGA uses its documented public test account, enabled explicitly per test via
GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1 in the settings env; the pinned pyega3 configuration
is fetched and hash-checked in memory. No other credentials are used."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import pysam
import pytest

from genomics_mcp.archives.ega.integration import TRANSFER_BACKEND_COMPONENT
from genomics_mcp.config import load_settings
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import GenomicsError
from genomics_mcp.models import FileRef, Interval
from genomics_mcp.public import Deadline
from genomics_mcp.registry import Operation
from genomics_mcp.result import EffectiveLimits
from genomics_mcp.service import GenomicsService

pytestmark = pytest.mark.network
EGAF = "EGAF00007243773"  # pyega3 functional-test BAM slice in EGAD00001003338


def svc(tmp_path: Path, public_test: bool = False) -> GenomicsService:
    env = {"GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT": "1"} if public_test else {}
    return GenomicsService(
        load_settings(env=env, overrides={"paths": {"work_dir": str(tmp_path / "work")}})
    )


def ctx(s: GenomicsService, op: Operation, timeout: float = 60) -> OperationContext:
    return OperationContext(
        operation=op,
        settings=s.settings,
        limits=EffectiveLimits.build(s.settings.limits),
        deadline=Deadline(timeout),
        registry=s.registry,
        http=s.http,
        request_id="live",
    )


async def test_ega_discovery_via_service(tmp_path):
    s = svc(tmp_path)
    d = await s.call("describe_dataset", {"source": "ega", "accession": "EGAD00001003338"})
    assert d.status == "ok" and d.data["dataset"]["access_status"] == "controlled"
    assert d.data["dataset"]["policy_accession"] == "EGAP00001000598" and d.data["studies"]
    files = await s.call(
        "list_files", {"source": "ega", "accession": "EGAD00001003338", "max_records": 5}
    )
    assert len(files.data["records"]) == 5 and files.truncation.reason == "source_page"
    assert all(f["visibility"] == "private" for f in files.data["records"])


async def test_ega_region_via_resolver_public_test_account(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    s = svc(tmp_path, public_test=True)
    iv = Interval(contig="chr10", start=10000, end=10050, assembly="GRCh38")
    r = await ctx(s, Operation.GET_READS).resolve_file(
        FileRef(uri=f"ega://{EGAF}", format="bam"), interval=iv
    )
    data = r.local_path.read_bytes()
    meta = r.file.native["region_artifact"]
    assert len(data) == 121225 and r.region == iv
    assert (
        hashlib.sha256(data).hexdigest()
        == "3305e420ba77b722771cd279a31b1f7766eb444ae4d7689369dc27897c686211"
    )
    assert (meta["records_received"], meta["records_overlapping"]) == (91, 42)
    assert meta["header"]["n_references"] == 3366 and meta["header"]["sq_assembly_tags"] == [
        "GRCh38"
    ]
    assert "access_token" not in caplog.text and "Bearer ey" not in caplog.text
    with pytest.raises(GenomicsError) as ei:  # GRCh37 request against a GRCh38 header is refused
        await ctx(s, Operation.GET_READS).resolve_file(
            FileRef(uri=f"ega://{EGAF}", format="bam"),
            interval=iv.model_copy(update={"assembly": "GRCh37"}),
        )
    assert ei.value.error_code == "invalid_input"


async def test_ega_whole_file_via_transfer_backend(tmp_path):
    import pysam

    s = svc(tmp_path, public_test=True)
    backend = s.registry.component(TRANSFER_BACKEND_COMPONENT)
    f = FileRef(uri=f"ega://{EGAF}", format="bam")
    desc = await backend.describe(f, ctx(s, Operation.FETCH_FILE))
    assert (
        desc.size_bytes == 194821 and desc.checksums[0].value == "ed365c71461eac21a64d2c29e7216e50"
    )
    out = tmp_path / "whole.bam"
    with out.open("wb") as fh:
        for start, end in (
            (0, 100000),
            (100000, desc.size_bytes),
        ):  # second request resumes at 100000
            async with backend.open(f, ctx(s, Operation.FETCH_FILE), start=start, end=end) as body:
                async for chunk in body:
                    fh.write(chunk)
    assert hashlib.md5(out.read_bytes()).hexdigest() == desc.checksums[0].value
    with pysam.AlignmentFile(str(out), "rb") as bam:
        assert sum(1 for _ in bam.fetch(until_eof=True)) == 1097


async def test_ega_permission_denied_is_unauthorized(tmp_path):
    s = svc(tmp_path, public_test=True)
    with pytest.raises(GenomicsError) as ei:
        await ctx(s, Operation.GET_READS).resolve_file(
            FileRef(uri="ega://EGAF00000077618", format="bam"),
            interval=Interval(contig="chr1", start=0, end=10, assembly="GRCh37"),
        )
    assert ei.value.error_code == "unauthorized" and ei.value.info.details["http_status"] == 403


async def test_ena_sequence_record_list_files_and_native_fetch(tmp_path):
    from genomics_mcp.archives._common.http import make_client
    from genomics_mcp.archives.ena import EnaClient

    s = svc(tmp_path)
    res = await s.call("list_files", {"source": "ena", "accession": "DQ285577"})
    (f,) = res.data["records"]
    assert f["accession"] == "DQ285577.1" and f["uri"].endswith("/fasta/DQ285577.1")
    assert f["native"]["base_count"] == "614" and f["readiness"]["state"] == "download_required"
    # E3 retrieval through the service: bounded transfer, then an explicit .fai on the copy.
    started = json.loads(
        (
            await s.call("fetch_file", {"file": f, "budget_bytes": 8192, "prepare": True})
        ).model_dump_json()
    )
    assert started["status"] == "ok", started["error"]
    tid = started["data"]["transfer"]["transfer_id"]
    for _ in range(300):
        done = json.loads(
            (await s.call("get_transfer_status", {"transfer_id": tid})).model_dump_json()
        )
        if done["data"]["transfer"]["state"] in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.1)
    t = done["data"]["transfer"]
    assert t["state"] == "completed", t["error"]
    assert t["bytes_done"] == t["bytes_total"] == 756 <= t["budget_bytes"] == 8192
    af = done["data"]["artifact_file"]
    assert af["format"] == "fasta" and af["readiness"]["state"] == "ready"
    fai = Path(af["index_uri"]).read_text().split("\t")  # noqa: ASYNC240 - small local test file
    assert fai[:2] == ["ENA|DQ285577|DQ285577.1", "614"]
    seq = await s.call(
        "get_sequence",
        {
            "file": af,
            "interval": {
                "contig": "ENA|DQ285577|DQ285577.1",
                "start": 0,
                "end": 614,
                "assembly": "DQ285577.1",
            },
        },
    )
    assert seq.status == "ok", seq.error
    service_seq = seq.data["records"][0]["sequence"]
    assert len(service_seq) == 614
    async with make_client() as c:  # the provider's explicit sequence-record path agrees
        art = await EnaClient(c).fetch_sequence_fasta(
            "DQ285577", workspace=tmp_path / "ena", budget_bytes=8192
        )
    assert art.size_bytes == 756 and art.checksum_verified and art.origin.accession == "DQ285577.1"
    assert Path(art.index_path).read_text().split("\t")[:2] == ["ENA|DQ285577|DQ285577.1", "614"]  # noqa: ASYNC240 - small local test file
    provider_md5 = hashlib.md5(Path(art.path).read_bytes()).hexdigest()  # noqa: ASYNC240
    assert done["data"]["source_verification"]["checksums"]["md5"] == provider_md5
    with pysam.FastaFile(art.path) as fa:
        assert fa.fetch("ENA|DQ285577|DQ285577.1") == service_seq


async def test_ena_study_samples_and_sra_resolution(tmp_path):
    s = svc(tmp_path)
    d = await s.call("describe_dataset", {"source": "ena", "accession": "SRP000001"})
    assert d.data["dataset"]["accession"] == "PRJNA33627" and d.warnings
    files = await s.call("list_files", {"source": "ena", "accession": "ERR10043599"})
    bam = next(x for x in files.data["records"] if x["format"] == "bam")
    assert bam["index_uri"].endswith(".bai") and bam["size_bytes"] == 10287 and bam["checksums"]


async def test_live_stdio_mcp_round_trip(tmp_path):
    """Real server subprocess over stdio; dummy ambient AWS values must never appear."""
    import os
    import sys

    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    dummy = "dummy-ambient-secret-never-used"
    (tmp_path / "aws").write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GENOMICS_MCP_WORK_DIR": str(tmp_path / "work"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": str(tmp_path / "aws"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "aws"),
        "AWS_ACCESS_KEY_ID": "AKIADUMMYDUMMYDUMMY0",
        "AWS_SECRET_ACCESS_KEY": dummy,
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "genomics_mcp"], env=env)
    with (tmp_path / "stderr.log").open("w") as errlog:
        async with Client(stdio_client(params, errlog=errlog), mode="legacy") as client:
            enc = await client.call_tool(
                "list_files", {"source": "encode", "accession": "ENCFF792QDS"}
            )
            ega = await client.call_tool(
                "describe_dataset", {"source": "ega", "accession": "EGAD00001003338"}
            )
            ena = await client.call_tool("list_files", {"source": "ena", "accession": "DQ285577"})
            srcs = await client.call_tool("list_sources", {"kind": "archive"})
    assert enc.structured_content["data"]["records"][0]["assembly"] == "GRCh38"
    assert ega.structured_content["data"]["dataset"]["access_status"] == "controlled"
    assert ena.structured_content["data"]["records"][0]["accession"] == "DQ285577.1"
    assert {r["name"]: r["state"] for r in srcs.structured_content["data"]["records"]} == {
        "ega": "ok",
        "ena": "ok",
    }
    text = (tmp_path / "stderr.log").read_text() + str(enc) + str(ega) + str(ena)
    assert dummy not in text and "AKIADUMMY" not in text
