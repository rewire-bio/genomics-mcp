"""Live ENCODE/GEO/NCBI Datasets checks (GENOMICS_MCP_LIVE=1). Never downloads large files."""

from __future__ import annotations

from pathlib import Path

import pytest

from genomics_mcp.archives._common.http import make_client
from genomics_mcp.catalogs.encode import EncodeClient
from genomics_mcp.catalogs.geo import GeoClient
from genomics_mcp.catalogs.ncbi_datasets import NcbiDatasetsClient

pytestmark = pytest.mark.live


async def test_encode_bigbed_metadata_and_range_readiness():
    async with make_client() as c:
        enc = EncodeClient(c)
        (f,) = (await enc.list_files("ENCFF001JBR")).items
        checked = await enc.check_file(f)
    assert (f.assembly, f.size_bytes, f.format) == ("mm9", 16438476, "bigbed")
    assert f.relationships[0].accession == "ENCSR000BZH"
    assert checked.readiness.state == "ready"


async def test_encode_bigwig_annotation_ready_without_download():
    async with make_client() as c:
        enc = EncodeClient(c)
        (f,) = (await enc.list_files("ENCFF792QDS")).items
        checked = await enc.check_file(f)
    assert (f.assembly, f.size_bytes) == ("GRCh38", 1413106336)
    assert (f.relationships[0].kind, f.relationships[0].accession) == ("dataset", "ENCSR901HTN")
    assert checked.readiness.state == "ready"


async def test_encode_biosamples():
    async with make_client() as c:
        page = await EncodeClient(c).list_samples("ENCSR000BZH")
    assert page.total == 2 and page.items[0].organism == "Mus musculus"


async def test_geo_sample_and_supplementary_bigwig():
    async with make_client() as c:
        geo = GeoClient(c)
        s = await geo.get_sample_metadata("GSM9343150")
        (bw,) = (await geo.list_files("GSM9343150")).items
        checked = await geo.check_file(bw)
    assert ("antibody", "H3K4me3") in [(p.name, p.value) for p in s.phenotypes]
    assert checked.readiness.state == "ready" and checked.size_bytes


async def test_ncbi_prepare_small_genome(tmp_path):
    async with make_client() as c:
        n = NcbiDatasetsClient(c)
        d = await n.describe_dataset("GCF_000001405.40")
        art = await n.prepare_genome_fasta("GCF_000819615.1", workspace=tmp_path, budget_bytes=1024 * 1024)
    assert d.dataset.title == "GRCh38.p14" and d.related["insdc_accession"] == "GCA_000001405.29"
    assert art.checksum_verified and Path(art.index_path).read_text().startswith("NC_001422.1\t5386\t")
