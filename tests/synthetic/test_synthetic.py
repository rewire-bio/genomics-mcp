"""Synthetic-data protocol catalogue and truth sets (#69): catalogue integrity, filters,
build pairing and the MCP round trip."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mcp.client import Client

from genomics_mcp import synthetic
from genomics_mcp.config import load_settings
from genomics_mcp.models import AccessStatus, FileFormat, Visibility
from genomics_mcp.registry import Registry
from genomics_mcp.server import build_server
from genomics_mcp.service import GenomicsService
from genomics_mcp.synthetic.protocols import PROTOCOLS
from genomics_mcp.synthetic.truth_sets import CONTIG_LENGTHS, TRUTH_SETS

DOI = re.compile(r"^10\.\d{4,9}/\S+$")
HEX = {"md5": 32, "sha256": 64}


def make_service(tmp_path: Path) -> GenomicsService:
    reg = Registry()
    synthetic.register(reg)
    settings = load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path / "work")}})
    return GenomicsService(settings, reg, load_providers=False)


@pytest.fixture
async def svc(tmp_path: Path):
    s = make_service(tmp_path)
    yield s
    await s.aclose()


# --------------------------------------------------------------------------- catalogue


@pytest.mark.parametrize("pid", sorted(PROTOCOLS))
def test_protocol_is_cited_pinned_and_linked(pid: str) -> None:
    p = PROTOCOLS[pid]
    keys = {c.key for c in p.citations}
    assert p.citations and all(c.url.startswith("https://") for c in p.citations)
    assert all(DOI.match(c.doi) for c in p.citations if c.doi)
    assert p.simulator.version and p.simulator.source_url.startswith("https://")
    for param in p.parameters:
        assert param.source in keys or param.source.startswith("https://"), param.name
    for t in p.truth_sets:
        assert t in TRUTH_SETS, t
    assert p.recipe.status == "planned" and p.recipe.id == f"rewirebench:synthetic/{p.id}"


@pytest.mark.parametrize("tid", sorted(TRUTH_SETS))
def test_truth_bundle_is_one_build_with_digests(tid: str) -> None:
    ts = TRUTH_SETS[tid]
    roles = [f.role for f in ts.files]
    assert len(roles) == len(set(roles))
    for tf in ts.files:
        f = tf.file
        assert f.assembly == ts.file_assembly, f.uri
        assert f.uri.startswith("https://")
        assert f.source == ts.provider and f.accession
        assert f.visibility == Visibility.PUBLIC and f.access_status == AccessStatus.OPEN
        assert f.checksums, f.uri
        for c in f.checksums:
            if c.algorithm in HEX:
                assert re.fullmatch(rf"[0-9a-f]{{{HEX[c.algorithm]}}}", c.value), f.uri
    if "benchmark_vcf" in roles:
        vcf = next(t.file for t in ts.files if t.role == "benchmark_vcf")
        assert vcf.format == FileFormat.VCF and vcf.index_uri == vcf.uri + ".tbi"
        assert "benchmark_regions" in roles and "reference_fasta" in roles
        ref = next(t.file for t in ts.files if t.role == "reference_fasta")
        assert vcf.reference_uri == ref.uri
    if ts.provider == "hprc":
        assert ts.assembly not in CONTIG_LENGTHS, "a haplotype assembly is not GRCh37/38"


def test_contig_naming_matches_each_build() -> None:
    assert CONTIG_LENGTHS["GRCh38"]["chr20"] == 64_444_167
    assert CONTIG_LENGTHS["GRCh37"]["20"] == 63_025_520
    for ts in TRUTH_SETS.values():
        if ts.assembly == "GRCh38":
            assert ts.contig_style == "chr"
        elif ts.assembly == "GRCh37":
            assert ts.contig_style == "plain"


# --------------------------------------------------------------------------- tools


async def test_filters_find_somatic_spike_in(svc: GenomicsService) -> None:
    res = await svc.call(
        "list_synthetic_protocols", {"variant_type": "indel", "method": "spike_in"}
    )
    assert res.status == "ok"
    assert [r["id"] for r in res.data["records"]] == ["bamsurgeon-snv-indel-spike-in"]
    res = await svc.call("list_synthetic_protocols", {"variant_type": "sv"})
    assert [r["id"] for r in res.data["records"]] == ["survivor-sv-simulation"]
    res = await svc.call("list_synthetic_protocols", {"query": "DREAM spike-in"})
    assert [r["id"] for r in res.data["records"]] == ["bamsurgeon-snv-indel-spike-in"]
    res = await svc.call("list_synthetic_protocols", {"query": "no such protocol"})
    assert res.status == "ok" and res.data["records"] == []


async def test_unknown_protocol_is_not_found(svc: GenomicsService) -> None:
    res = await svc.call("describe_synthetic_protocol", {"protocol_id": "magic"})
    assert res.status == "error" and res.error.code == "not_found"
    assert "bamsurgeon-snv-indel-spike-in" in res.error.hint


async def test_mismatched_build_is_rejected(svc: GenomicsService) -> None:
    res = await svc.call(
        "get_truth_set", {"truth_set_id": "giab-hg002-v4.2.1-grch38", "assembly": "GRCh37"}
    )
    assert res.status == "error" and res.error.code == "invalid_input"
    assert "no liftover" in res.error.message


@pytest.mark.parametrize(
    ("assembly", "wrong", "right"), [("GRCh38", "20", "chr20"), ("GRCh37", "chr20", "20")]
)
async def test_contig_names_are_not_translated(
    svc: GenomicsService, assembly: str, wrong: str, right: str
) -> None:
    res = await svc.call(
        "get_truth_set",
        {"sample": "HG002", "assembly": assembly, "variant_type": "snv", "contig": wrong},
    )
    assert res.status == "error" and res.error.code == "invalid_input"
    assert repr(right) in res.error.hint


async def test_haplotype_assembly_has_no_grch_contig_lookup(svc: GenomicsService) -> None:
    res = await svc.call("get_truth_set", {"provider": "hprc", "contig": "chr20"})
    assert res.status == "error" and res.error.code == "invalid_input"
    assert "own assembly" in res.error.message


async def test_no_match_is_empty_with_hint(svc: GenomicsService) -> None:
    res = await svc.call("get_truth_set", {"sample": "HG999"})
    assert res.status == "ok" and res.data["records"] == []
    assert "known truth sets" in res.warnings[0]


async def test_done_when_spike_in_on_hg002_chr20_over_mcp(tmp_path: Path) -> None:
    """An agent asking for an SNV and indel spike-in on HG002 chr20 gets the protocol, the
    truth files and the recipe ID."""
    svc = make_service(tmp_path)
    async with Client(build_server(svc), mode="legacy") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("list_synthetic_protocols", "describe_synthetic_protocol", "get_truth_set"):
            assert "not implemented in this build" not in tools[name].description.lower()
            assert tools[name].annotations.read_only_hint is True

        res = await client.call_tool(
            "list_synthetic_protocols", {"variant_type": "snv", "method": "spike_in"}
        )
        (protocol,) = res.structured_content["data"]["records"]
        assert "records: 1" in res.content[0].text

        res = await client.call_tool("describe_synthetic_protocol", {"protocol_id": protocol["id"]})
        body = res.structured_content
        assert body["status"] == "ok"
        assert body["data"]["protocol"]["recipe"] == {
            "id": "rewirebench:synthetic/bamsurgeon-snv-indel-spike-in",
            "status": "planned",
        }
        assert body["data"]["protocol"]["citations"][0]["doi"] == "10.1038/nmeth.3407"
        assert any("reserved" in w for w in body["warnings"])
        linked = {t["id"] for t in body["data"]["truth_sets"]}

        res = await client.call_tool(
            "get_truth_set",
            {"sample": "HG002", "assembly": "GRCh38", "variant_type": "snv", "contig": "chr20"},
        )
        body = res.structured_content
        assert body["status"] == "ok"
        (bundle,) = body["data"]["records"]
        assert bundle["id"] in linked
        assert bundle["region"] == {
            "contig": "chr20",
            "start": 0,
            "end": 64_444_167,
            "assembly": "human_GRCh38_no_alt_analysis_set.fasta",
        }
        files = {f["role"]: f["file"] for f in bundle["files"]}
        assert set(files) == {"benchmark_vcf", "benchmark_regions", "reference_fasta"}
        assert files["benchmark_vcf"]["uri"].startswith(
            "https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/"
        )
        assert {c["algorithm"] for c in files["benchmark_vcf"]["checksums"]} >= {"md5"}
        assert body["provenance"][0]["source"] == "giab"
        assert body["provenance"][0]["source_version"] == "NISTv4.2.1"
    await svc.aclose()


def test_capabilities_report_the_provider(tmp_path: Path) -> None:
    svc = GenomicsService(
        load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path / "work")}})
    )
    caps = svc.capabilities()
    assert caps["providers"]["genomics_mcp.synthetic"] == "loaded"
    for op in ("list_synthetic_protocols", "describe_synthetic_protocol", "get_truth_set"):
        assert caps["operations"][op]["available"]
        assert caps["operations"][op]["category"] == "synthetic"
