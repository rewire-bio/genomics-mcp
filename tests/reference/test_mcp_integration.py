"""E8 through the core: GenomicsService dispatch, typed envelopes, list_sources, MCP round-trip,
local FASTA boundaries and the composition facade's egress consent."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pysam
import pytest
from mcp.client import Client

from genomics_mcp.config import Settings, SourceSettings
from genomics_mcp.context import OperationContext
from genomics_mcp.evidence import ReferenceRuntime, register
from genomics_mcp.models import FileRef, Visibility
from genomics_mcp.public import Deadline, EgressContext
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import EffectiveLimits, ResultStatus
from genomics_mcp.server import build_server
from genomics_mcp.service import GenomicsService

from .conftest import Router, html_500, load_bytes, load_json, ncbi_fasta

EUTILS = r"eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils"
BRAF38 = "N" * 64 + "A" + "N" * 64
REFERENCE_OPS = [
    "resolve_identifier",
    "normalize_variant",
    "lookup_variant",
    "lookup_gene",
    "lookup_protein",
]
V600E = {"assembly": "GRCh38", "contig": "7", "pos": 140753336, "ref": "A", "alt": "T"}


async def _no_sleep(seconds: float) -> None:
    """Retry backoff without real waiting; deadlines still use the real clock."""


def make_service(settings: Settings, router: Router) -> GenomicsService:
    reg = Registry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(router.handle))
    register(reg, runtime=ReferenceRuntime(client=client, service_kwargs={"sleep": _no_sleep}))
    return GenomicsService(settings, reg, load_providers=False)


def braf_routes(router: Router) -> None:
    router.add("GET", r"rest\.ensembl\.org/sequence", html_500())
    router.add(
        "GET",
        EUTILS + r"/efetch\.fcgi",
        lambda request: (
            ncbi_fasta("NC_000007.14", BRAF38)
            if request.url.params.get("db") == "nuccore"
            else httpx.Response(200, content=load_bytes("clinvar_vcv_13961_trimmed.xml"))
        ),
    )
    router.json("GET", EUTILS + r"/esearch", load_json("clinvar_esearch_braf_grch38.json"))
    router.json("GET", EUTILS + r"/esummary", load_json("clinvar_esummary_braf.json"))
    router.json("GET", EUTILS + r"/einfo", load_json("clinvar_einfo.json"))
    router.json("POST", r"gnomad", load_json("gnomad_variant_7-140753336-A-T.json"))


def fasta(root: Path, name: str = "ref.fa", *, index: bool = True) -> Path:
    # 0-based 0..12 on contig "chr7": the CA repeat used in the normalization tests.
    path = root / name
    path.write_text(">chr7\nGGCTCACACAGTT\n")
    if index:
        pysam.faidx(str(path))
    return path


async def test_all_reference_tools_are_registered_and_sources_listed(settings, router):
    svc = make_service(settings, router)
    caps = svc.capabilities()["operations"]
    for op in REFERENCE_OPS:
        assert caps[op]["available"] is True and caps[op]["keys"] == ["default"], op
    res = await svc.call(Operation.LIST_SOURCES, {"kind": "reference"})
    by_name = {s["name"]: s for s in res.data["records"]}
    for name in (
        "hgnc",
        "ensembl",
        "clinvar",
        "gnomad",
        "uniprot",
        "open_targets",
        "ncbi_variation",
    ):
        assert by_name[name]["state"] == "ok", name
    assert "lookup_variant" in by_name["clinvar"]["operations"]
    assert by_name["open_targets"]["operations"] == ["lookup_gene"]
    assert by_name["alphagenome_atlas"]["state"] == "not_configured"
    assert "cosmic" not in by_name
    assert router.calls == []


async def test_lookup_variant_envelope_keeps_assertions_and_denominators(settings, router):
    braf_routes(router)
    svc = make_service(settings, router)
    res = await svc.call(
        Operation.LOOKUP_VARIANT, {"variant": V600E, "sources": ["clinvar", "gnomad"]}
    )
    assert res.status is ResultStatus.PARTIAL  # Ensembl sequence HTTP 500 is reported, not hidden
    data = res.data
    assert data["canonical_variant"]["reference_check"]["source"] == "ncbi_nuccore"
    records = data["records"]
    by_type = {}
    for r in records:
        by_type.setdefault(r["evidence_type"], []).append(r)
    vcv = by_type["clinical_variant_record"][0]
    assert vcv["source_record_id"] == "VCV000013961" and vcv["source_record_version"] == "143"
    agg = vcv["data"]["aggregate_classifications"]
    assert agg["germline"]["conflict_reported_by_clinvar"] is True
    assert set(agg) == {"germline", "somatic_clinical_impact", "oncogenicity"}
    scvs = by_type["clinical_assertion"]
    assert len(scvs) == 11 and all(isinstance(s["data"]["submitter"], str) for s in scvs)
    assert records[-1]["evidence_type"] == "clinical_assertion"  # SCVs are trimmed last-first
    gnomad = by_type["population_frequency"][0]
    assert gnomad["observed"] is True
    assert (gnomad["data"]["exome"]["ac"], gnomad["data"]["exome"]["an"]) == (2, 1460618)
    assert gnomad["provenance"]["source_version"] == "dataset gnomad_r4"
    states = {s.source: s.state.value for s in res.source_status}
    assert states["clinvar"] == "ok" and states["gnomad"] == "ok" and states["ncbi_nuccore"] == "ok"
    assert states["ensembl"] == "unavailable"
    assert {p.source for p in res.provenance} >= {"clinvar", "gnomad"}
    err = next(e for e in res.errors if e.source == "ensembl")
    assert err.code == "upstream_error" and err.details["http_status"] == 500
    assert "?" not in err.details["url"]


async def test_record_and_byte_caps_are_reported(settings, router):
    braf_routes(router)
    svc = make_service(settings, router)
    res = await svc.call(Operation.LOOKUP_VARIANT, {"variant": V600E, "sources": ["clinvar"]})
    total = len(res.data["records"])
    settings.limits.max_records = 3
    capped = await make_service(settings, router).call(
        Operation.LOOKUP_VARIANT, {"variant": V600E, "sources": ["clinvar"]}
    )
    assert len(capped.data["records"]) == 3
    assert capped.truncation.reason == "max_records" and capped.truncation.available == total
    settings.limits.max_records = 10_000
    small = await make_service(settings, router).call(
        Operation.LOOKUP_VARIANT, {"variant": V600E, "sources": ["clinvar"]}
    )
    assert small.json_size() <= 1 << 20
    from genomics_mcp.result import fit_to_response_budget

    fitted = fit_to_response_budget(small, 16_384)
    assert fitted.json_size() <= 16_384
    assert fitted.truncation.reason == "max_response_bytes"
    assert 0 < fitted.truncation.returned < total


async def test_unknown_and_disabled_sources_are_explicit(settings, router):
    braf_routes(router)
    settings.sources["gnomad"] = SourceSettings(enabled=False)
    svc = make_service(settings, router)
    res = await svc.call(
        Operation.LOOKUP_VARIANT, {"variant": V600E, "sources": ["clinvar", "gnomad", "cosmic"]}
    )
    states = {s.source: s.state.value for s in res.source_status}
    assert states["cosmic"] == "not_implemented"
    assert states["gnomad"] == "disabled"
    assert not router.called(r"gnomad")
    only_unknown = await svc.call(
        Operation.LOOKUP_VARIANT, {"variant": V600E, "sources": ["cosmic"]}
    )
    assert only_unknown.status is ResultStatus.ERROR and only_unknown.error.code == "invalid_input"


async def test_local_fasta_normalizes_without_network(settings, router, data_root):
    ref = fasta(data_root)
    svc = make_service(settings, router)
    res = await svc.call(
        Operation.NORMALIZE_VARIANT,
        {
            "variant": {"assembly": "GRCh38", "contig": "7", "pos": 8, "ref": "ACA", "alt": "A"},
            "reference": {"uri": str(ref), "assembly": "GRCh38"},
        },
    )
    assert router.calls == []
    assert res.status is ResultStatus.OK
    v = res.data["canonical_variant"]
    assert v["reference_check"]["source"] == "local_fasta"
    assert (v["start"], v["ref"], v["vcf"]["pos"], v["vcf"]["ref"]) == (4, "CA", 4, "TCA")
    assert res.data["local_reference_steps"][0]["operation"] == "contig_name_in_fasta"


@pytest.mark.parametrize(
    ("ref_arg", "code"),
    [
        ({"assembly": None}, "invalid_input"),  # assembly must be explicit
        ({"assembly": "GRCh37"}, "invalid_input"),  # and match the variant: no liftover
    ],
)
async def test_local_fasta_requires_explicit_matching_assembly(
    settings, router, data_root, ref_arg, code
):
    ref = fasta(data_root)
    res = await make_service(settings, router).call(
        Operation.NORMALIZE_VARIANT,
        {
            "variant": {"assembly": "GRCh38", "contig": "7", "pos": 4, "ref": "T", "alt": "G"},
            "reference": {"uri": str(ref), **ref_arg},
        },
    )
    assert res.status is ResultStatus.ERROR and res.error.code == code
    assert router.calls == []


async def test_local_fasta_outside_allowed_roots_and_missing_index(
    settings, router, tmp_path, data_root
):
    outside = tmp_path / "outside"
    outside.mkdir()
    ref = fasta(outside)
    svc = make_service(settings, router)
    args = {
        "variant": {"assembly": "GRCh38", "contig": "7", "pos": 4, "ref": "T", "alt": "G"},
        "sources": ["local_fasta"],
    }
    res = await svc.call(
        Operation.NORMALIZE_VARIANT, {**args, "reference": {"uri": str(ref), "assembly": "GRCh38"}}
    )
    assert any(e.source == "local_fasta" and e.code == "unauthorized" for e in res.errors)
    assert res.data["canonical_variant"]["reference_check"]["status"] == "not_checked"
    no_index = fasta(data_root, "noindex.fa", index=False)
    res = await svc.call(
        Operation.NORMALIZE_VARIANT,
        {**args, "reference": {"uri": str(no_index), "assembly": "GRCh38"}},
    )
    err = next(e for e in res.errors if e.source == "local_fasta")
    assert err.code == "unsupported" and ".fai" in err.message
    assert router.calls == []


def _ctx(svc: GenomicsService, settings: Settings) -> OperationContext:
    return OperationContext(
        operation=Operation.INSPECT_LOCUS, settings=settings,
        limits=EffectiveLimits.build(settings.limits), deadline=Deadline(5.0),
        registry=svc.registry, http=svc.http, request_id="t",
    )  # fmt: skip


async def test_facade_blocks_private_egress_without_consent(settings, router, data_root):
    braf_routes(router)
    svc = make_service(settings, router)
    facade = svc.registry.component("reference_evidence")
    ctx = _ctx(svc, settings)
    private = FileRef(uri=str(data_root / "calls.vcf"), visibility=Visibility.PRIVATE)
    egress = EgressContext.for_files([private], consent=False)
    from genomics_mcp.models import VariantSpec

    out = await facade.lookup_variant(ctx, VariantSpec(**V600E), egress=egress)
    assert router.calls == []
    assert out.data is None
    assert any(e.code == "consent_required" for e in out.errors)
    # Local validation still runs with a local FASTA, and still sends nothing.
    ref = fasta(data_root)
    norm = await facade.normalize_variant(
        ctx,
        VariantSpec(assembly="GRCh38", contig="7", pos=4, ref="T", alt="G"),
        egress=egress,
        reference=FileRef(uri=str(ref), assembly="GRCh38"),
    )
    assert router.calls == []
    assert norm.data["canonical_variant"]["reference_check"]["status"] == "verified"
    consented = await facade.lookup_variant(
        ctx,
        VariantSpec(**V600E),
        egress=EgressContext.for_files([private], consent=True),
        sources=["gnomad"],
    )
    assert router.called(r"gnomad") and consented.data["records"][0]["source"] == "gnomad"


async def test_mcp_round_trip_calls_reference_tools(settings, router):
    router.json("GET", r"rest\.genenames\.org/info$", load_json("hgnc_info.json"))
    router.json("GET", r"rest\.genenames\.org/fetch/symbol/", load_json("hgnc_empty.json"))
    router.json("GET", r"rest\.genenames\.org/fetch/alias_symbol/", load_json("hgnc_empty.json"))
    router.json(
        "GET", r"rest\.genenames\.org/fetch/prev_symbol/", load_json("hgnc_prev_symbol_FANCD1.json")
    )
    router.json("GET", r"uniprotkb/P51587\.json", load_json("uniprot_P51587.json"))
    svc = make_service(settings, router)
    async with Client(build_server(svc)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for op in REFERENCE_OPS:
            assert "Not implemented in this build" not in tools[op].description
        res = await client.call_tool(
            "resolve_identifier", {"identifier": "FANCD1", "sources": ["hgnc"]}
        )
        body = res.structured_content
        assert body["status"] == "ok"
        assert body["data"]["resolved"]["gene"]["hgnc_id"] == "HGNC:1101"
        assert body["data"]["transformations"][0]["operation"] == "previous_symbol_to_approved"
        res = await client.call_tool("lookup_protein", {"protein": "P51587"})
        body = res.structured_content
        assert body["status"] == "ok"
        entry = body["data"]["records"][0]
        assert entry["data"]["reviewed"] is True and entry["provenance"]["source"] == "uniprot"
        assert "lookup_protein: ok" in res.content[0].text
        json.dumps(body)
    await svc.aclose()


async def test_atlas_without_key_is_not_configured_and_sends_nothing(settings, router):
    svc = make_service(settings, router)
    res = await svc.call(
        Operation.LOOKUP_VARIANT,
        {"variant": V600E, "sources": ["alphagenome_atlas"], "include": ["prediction"]},
    )
    assert not router.called(r"googleapis|alphagenome")
    states = {s.source: s.state.value for s in res.source_status}
    assert states["alphagenome_atlas"] == "not_configured"
    atlas_err = next(e for e in res.errors if e.source == "alphagenome_atlas")
    assert atlas_err.code == "unsupported" and "alphagenome.google/terms" in atlas_err.message
