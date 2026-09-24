"""Tool-level behavior: failure isolation, ambiguity handling and the dispatch entrypoint."""

from __future__ import annotations

import httpx
import pytest

from genomics_mcp.references import ReferenceConfig, call_tool, input_schemas, source_status
from genomics_mcp.references.schemas import LookupVariantRequest, NormalizeVariantRequest

from .conftest import Router, html_500, load_bytes, load_json, ncbi_fasta

pytestmark = pytest.mark.asyncio
EUTILS = r"eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils"
BRAF38 = "N" * 64 + "A" + "N" * 64


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("stalled", request=request)


async def test_lookup_variant_isolates_429_and_timeout(router: Router, make_service) -> None:
    router.json("GET", r"grch37\.rest\.ensembl\.org/vep/", load_json("ensembl_grch37_vep_7-140453136-T.json"))
    router.json("GET", r"grch37\.rest\.ensembl\.org/info/software", {"release": 116})
    router.add("GET", EUTILS + r"/esearch", httpx.Response(429, json={"error": "API rate limit exceeded"}))
    router.add("POST", r"gnomad", timeout)
    result = await make_service(ReferenceConfig(max_retries=1)).lookup_variant(LookupVariantRequest(
        variant="7-140453136-A-T", assembly="GRCh37", use_remote_reference=False))
    assert result.status == "partial"
    assert len(result.evidence.consequence) == 1
    assert result.gene_context[0]["symbol"] == "BRAF"
    kinds = {(e.source, e.kind) for e in result.errors}
    assert kinds == {("clinvar", "rate_limited"), ("gnomad", "timeout")}
    assert len(router.called(r"esearch")) == 2  # one retry of the 429


async def test_lookup_variant_end_to_end_with_real_fixtures(router: Router, make_service) -> None:
    router.add("GET", r"rest\.ensembl\.org/sequence", html_500())
    router.add("GET", EUTILS + r"/efetch\.fcgi", lambda request: (
        ncbi_fasta("NC_000007.14", BRAF38) if request.url.params.get("db") == "nuccore"
        else httpx.Response(200, content=load_bytes("clinvar_vcv_13961_trimmed.xml"))))
    router.add("GET", r"rest\.ensembl\.org/vep/", html_500())
    router.json("GET", EUTILS + r"/esearch", load_json("clinvar_esearch_braf_grch38.json"))
    router.json("GET", EUTILS + r"/esummary", load_json("clinvar_esummary_braf.json"))
    router.json("GET", EUTILS + r"/einfo", load_json("clinvar_einfo.json"))
    router.json("POST", r"gnomad", load_json("gnomad_variant_7-140753336-A-T.json"))
    out = await call_tool(make_service(ReferenceConfig(max_retries=0)), "lookup_variant",
                          {"variant": "chr7:140753336A>T", "assembly": "GRCh38"})
    assert out["status"] == "partial"
    assert out["canonical_variant"]["reference_check"] == {
        "status": "verified", "source": "ncbi_nuccore", "expected_ref": "A", "observed_ref": "A"}
    clinical = out["evidence"]["clinical"]
    assert clinical[0]["data"]["matched_variation_ids"] == ["13961"]
    assert clinical[1]["source_record_id"] == "VCV000013961"
    assert out["evidence"]["population"][0]["data"]["exome"]["an"] == 1460618
    # Ensembl outages are reported, not hidden, in both normalization and lookup.
    assert ("ensembl", "vep/region", 500) in {(e["source"], e["operation"], e.get("status_code")) for e in out["errors"]}
    assert out["normalization"]["errors"][0]["operation"] == "sequence/region"


async def test_multiallelic_rsid_returns_candidates_and_no_lookups(router: Router, make_service) -> None:
    router.json("GET", r"api\.ncbi\.nlm\.nih\.gov/variation/v0/refsnp/113488022", load_json("ncbi_refsnp_113488022_trimmed.json"))
    result = await make_service().lookup_variant(LookupVariantRequest(variant="rs113488022", assembly="GRCh38"))
    assert result.status == "ambiguous"
    assert result.canonical_variant is None
    spdis = {c.spdi for c in result.normalization.candidates}
    assert spdis == {"NC_000007.14:140753335:A:C", "NC_000007.14:140753335:A:G", "NC_000007.14:140753335:A:T"}
    assert [r.url.host for r in router.calls] == ["api.ncbi.nlm.nih.gov"]


async def test_rsid_falls_back_to_ensembl_when_ncbi_fails(router: Router, make_service) -> None:
    router.add("GET", r"variation/v0/refsnp", httpx.Response(503, json={"error": "down"}))
    router.json("GET", r"grch37\.rest\.ensembl\.org/variant_recoder/", load_json("ensembl_grch37_variant_recoder_rs113488022.json"))
    result = await make_service(ReferenceConfig(max_retries=0)).normalize_variant(
        NormalizeVariantRequest(variant="rs113488022", assembly="GRCh37"))
    assert result.status == "ambiguous"
    assert {c.vcf.alt for c in result.candidates} == {"C", "G", "T"}
    assert result.errors[0].source == "ncbi_variation"


async def test_coding_hgvs_resolves_through_ncbi_with_exact_version(router: Router, make_service) -> None:
    router.json("GET", r"variation/v0/hgvs/.+/contextuals", load_json("ncbi_hgvs_contextuals_NM_004333.6.json"))
    router.json("GET", r"variation/v0/spdi/.+/all_equivalent_contextual", load_json("ncbi_all_equivalent_NM_004333.6.json"))
    result = await make_service().normalize_variant(NormalizeVariantRequest(
        variant="NM_004333.6(BRAF):c.1799T>A", assembly="GRCh38", use_remote_reference=False))
    assert result.status == "ok"
    v = result.canonical_variant
    assert (v.contig, v.start, v.end, v.ref, v.alt) == ("7", 140753335, 140753336, "A", "T")
    ops = [t.operation for t in result.transformations]
    assert ops[:2] == ["drop_hgvs_gene_label", "transcript_to_genome"]


async def test_transcript_version_remapped_by_ensembl_is_not_selected(router: Router, make_service) -> None:
    router.add("GET", r"variation/v0/hgvs", httpx.Response(503, json={"error": "down"}))
    router.json("GET", r"grch37\.rest\.ensembl\.org/variant_recoder/", load_json("ensembl_grch37_variant_recoder_NM_004333.4.json"))
    result = await make_service(ReferenceConfig(max_retries=0)).normalize_variant(NormalizeVariantRequest(
        variant="NM_004333.4:c.1799T>A", assembly="GRCh37"))
    assert result.status == "unresolved"
    assert result.canonical_variant is None
    assert "does not appear in the Ensembl output" in result.candidates[0].notes[0]


async def test_unversioned_transcript_is_rejected_without_requests(router: Router, make_service) -> None:
    result = await make_service().normalize_variant(NormalizeVariantRequest(variant="NM_004333:c.1799T>A", assembly="GRCh38"))
    assert result.status == "error" and "has no version" in result.errors[0].message
    assert router.calls == []


async def test_protein_hgvs_is_never_selected(router: Router, make_service) -> None:
    router.json("GET", r"grch37\.rest\.ensembl\.org/variant_recoder/", load_json("ensembl_grch37_variant_recoder_BRAF_p.json"))
    result = await make_service().lookup_variant(LookupVariantRequest(variant="BRAF:p.Val600Glu", assembly="GRCh37"))
    assert result.status == "unresolved"
    assert result.canonical_variant is None
    assert result.normalization.candidates[0].vcf.alt == "T"
    assert any("may resolve to multiple genomic locations" in w for w in result.normalization.warnings)
    assert [r.url.host for r in router.calls] == ["grch37.rest.ensembl.org"]


async def test_multiallelic_vcf_is_ambiguous_with_each_allele_normalized(make_service) -> None:
    result = await make_service().normalize_variant(NormalizeVariantRequest(
        variant="7-1004-T-G,TCA", assembly="GRCh38", use_remote_reference=False,
        reference={"assembly": "GRCh38", "contig": "7", "start": 1000, "sequence": "GGCTCACACAGTT"}))
    assert result.status == "ambiguous"
    assert [a.variant_class for a in result.alleles] == ["SNV", "insertion"]


async def test_call_tool_validation_and_schemas(make_service) -> None:
    out = await call_tool(make_service(), "normalize_variant", {"variant": {"assembly": "GRCh38", "contig": "7",
                                                                           "ref": "A", "alt": "T"}})
    assert out["status"] == "error" and out["errors"][0]["kind"] == "invalid_input"
    schemas = input_schemas()
    assert set(schemas) == {"resolve_identifier", "normalize_variant", "lookup_variant", "lookup_gene", "lookup_protein"}
    status = {s["name"]: s["status"] for s in source_status(make_service())}
    assert status["alphagenome_atlas"].startswith("disabled")
    assert "cosmic" not in status
