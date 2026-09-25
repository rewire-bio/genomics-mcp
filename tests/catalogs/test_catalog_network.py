"""Live E7 demos through GenomicsService with the real providers (GENOMICS_MCP_NETWORK_TESTS=1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from genomics_mcp.catalogs.preparation import PREPARER_COMPONENT
from genomics_mcp.config import load_settings
from genomics_mcp.context import OperationContext
from genomics_mcp.public import Deadline
from genomics_mcp.registry import Operation
from genomics_mcp.result import EffectiveLimits
from genomics_mcp.service import GenomicsService

pytestmark = pytest.mark.network


def svc(tmp_path: Path) -> GenomicsService:
    return GenomicsService(
        load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path / "work")}})
    )


async def test_encode_signal_file_metadata(tmp_path):
    s = svc(tmp_path)
    res = await s.call("list_files", {"source": "encode", "accession": "ENCFF792QDS"})
    (f,) = res.data["records"]
    assert (f["assembly"], f["size_bytes"], f["format"]) == ("GRCh38", 1413106336, "bigwig")
    assert f["native"]["href"].endswith("/files/ENCFF792QDS/@@download/ENCFF792QDS.bigWig")
    assert f["relationships"][0]["accession"] == "ENCSR901HTN"
    bb = await s.call(
        "list_files", {"source": "encode", "accession": "ENCSR000BZH", "max_records": 3}
    )
    assert bb.data["total"] and bb.data["records"][0]["relationships"][0]["kind"] == "experiment"
    samples = await s.call("list_samples", {"source": "encode", "accession": "ENCSR000BZH"})
    assert samples.data["total"] == 2


async def test_geo_sample_and_files(tmp_path):
    s = svc(tmp_path)
    m = await s.call("get_sample_metadata", {"source": "geo", "accession": "GSM9343150"})
    assert {"name": "antibody", "value": "H3K4me3"}.items() <= next(
        p for p in m.data["phenotypes"] if p["name"] == "antibody"
    ).items()
    files = await s.call("list_files", {"source": "geo", "accession": "GSM9343150"})
    assert files.data["records"][0]["uri"].startswith("https://ftp.ncbi.nlm.nih.gov/geo/samples/")


async def test_ncbi_assembly_and_explicit_preparation(tmp_path):
    s = svc(tmp_path)
    d = await s.call(
        "describe_dataset", {"source": "ncbi_datasets", "accession": "GCF_000001405.40"}
    )
    assert (
        d.data["dataset"]["title"] == "GRCh38.p14"
        and d.data["related"]["insdc_accession"] == "GCA_000001405.29"
    )
    pk = await s.call("list_files", {"source": "ncbi_datasets", "accession": "GCF_000819615.1"})
    assert all(r["readiness"]["state"] == "download_required" for r in pk.data["records"])
    ctx = OperationContext(
        operation=Operation.FETCH_FILE,
        settings=s.settings,
        limits=EffectiveLimits.build(s.settings.limits),
        deadline=Deadline(60),
        registry=s.registry,
        http=s.http,
        request_id="live",
    )
    art = await s.registry.component(PREPARER_COMPONENT).prepare(
        "GCF_000819615.1", ctx, budget_bytes=1 << 20
    )
    assert art.checksum_verified and Path(art.index_path).read_text().startswith(  # noqa: ASYNC240 - small local test file
        "NC_001422.1\t5386\t"
    )
