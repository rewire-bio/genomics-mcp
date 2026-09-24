"""Live EGA/ENA retrieval (GENOMICS_MCP_LIVE=1).

EGA tests also need GENOMICS_MCP_EGA_PYEGA3_CONFIG: a directory holding pyega3's public
`default_server_file.json` and `default_credential_file.json` (the documented public test
account). Credentials are loaded into memory only; nothing else is read.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from genomics_mcp.archives._common.errors import UnauthorizedError
from genomics_mcp.archives._common.http import make_client
from genomics_mcp.archives._common.models import Interval
from genomics_mcp.archives.ega import EgaAuth, EgaClient, EgaPasswordGrant
from genomics_mcp.archives.ena import EnaClient

pytestmark = pytest.mark.live
EGA_FILE = "EGAF00007243773"  # HG00096 slice BAM in EGAD00001003338 (pyega3 functional test)


def ega_auth() -> EgaAuth:
    cfg = os.environ.get("GENOMICS_MCP_EGA_PYEGA3_CONFIG")
    if not cfg:
        pytest.skip("GENOMICS_MCP_EGA_PYEGA3_CONFIG not set")
    d = Path(cfg)
    return EgaAuth(grant=EgaPasswordGrant.from_pyega3_files(d / "default_server_file.json",
                                                            d / "default_credential_file.json"))


async def test_ega_public_metadata():
    async with make_client() as c:
        d = await EgaClient(c).describe_dataset("EGAD00001003338")
    assert d.dataset.access_status == "controlled" and d.dataset.policy_accession == "EGAP00001000598"
    assert d.dataset.file_count and d.studies


async def test_ega_htsget_region(tmp_path):
    async with make_client() as c:
        res = await EgaClient(c, auth=ega_auth()).get_region(
            EGA_FILE, Interval(contig="chr10", start=10000, end=10050, assembly="GRCh38"),
            workspace=tmp_path, budget_bytes=5 * 1024 * 1024)
    data = Path(res.artifact.path).read_bytes()
    assert len(data) == 121225
    assert hashlib.sha256(data).hexdigest() == "3305e420ba77b722771cd279a31b1f7766eb444ae4d7689369dc27897c686211"
    assert (res.records_in_blocks, res.records_overlapping) == (91, 42)
    assert res.header.n_references == 3366 and res.header.sq_assembly_tags == ["GRCh38"]
    assert all(r["start"] < 10050 and r["end"] > 10000 for r in res.records)


async def test_ega_whole_small_file(tmp_path):
    import pysam

    async with make_client() as c:
        art = await EgaClient(c, auth=ega_auth()).fetch_file(EGA_FILE, workspace=tmp_path, budget_bytes=5 * 1024 * 1024)
    assert art.size_bytes == 194821 and art.checksum_verified
    assert art.checksums[0].value == "ed365c71461eac21a64d2c29e7216e50"
    with pysam.AlignmentFile(art.path, "rb") as fh:
        assert sum(1 for _ in fh.fetch(until_eof=True)) == 1097


async def test_ega_check_file_links_index():
    async with make_client() as c:
        ref = await EgaClient(c, auth=ega_auth()).check_file(EGA_FILE)
    assert ref.readiness.state == "ready" and ref.index_uri == "ega://EGAF00007243782"


async def test_ega_permission_denied_is_unauthorized_not_missing(tmp_path):
    async with make_client() as c:
        with pytest.raises(UnauthorizedError) as ei:
            await EgaClient(c, auth=ega_auth()).get_region(
                "EGAF00000077618", Interval(contig="chr1", start=0, end=10, assembly="GRCh37"),
                workspace=tmp_path, budget_bytes=1024)
    assert ei.value.details["http_status"] == 403 and list(tmp_path.iterdir()) == []


async def test_ena_submitted_bam_and_index(tmp_path):
    import pysam

    async with make_client() as c:
        ena = EnaClient(c)
        files = (await ena.list_files("ERR10043599")).items
        bam = next(f for f in files if f.format == "bam")
        assert bam.index_uri and bam.size_bytes == 10287
        checked = await ena.check_file(bam)
        art = await ena.fetch_file(bam, workspace=tmp_path, budget_bytes=1024 * 1024)
    assert checked.readiness.state == "ready" and art.checksum_verified
    with pysam.AlignmentFile(art.path, index_filename=art.index_path) as fh:
        assert fh.count("MT") == 198
    fastq = next(f for f in files if f.format == "fastq")
    assert fastq.readiness.state == "not_locus_ready"


async def test_ena_sequence_fasta_version(tmp_path):
    async with make_client() as c:
        art = await EnaClient(c).fetch_sequence_fasta("DQ285577", workspace=tmp_path, budget_bytes=64 * 1024)
    assert art.origin.accession == "DQ285577.1"
    fai = Path(art.index_path).read_text().split("\t")
    assert fai[:2] == ["ENA|DQ285577|DQ285577.1", "614"]


async def test_ena_sra_accession_resolves_through_ena():
    async with make_client() as c:
        d = await EnaClient(c).describe_dataset("SRP000001")
    assert d.dataset.accession == "PRJNA33627" and d.warnings
