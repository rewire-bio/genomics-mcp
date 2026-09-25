"""HGNC symbol, alias and previous-symbol resolution against real response fixtures."""

from __future__ import annotations

import pytest

from genomics_mcp.references.schemas import LookupGeneRequest, ResolveIdentifierRequest

from .conftest import Router, load_json

pytestmark = pytest.mark.asyncio
HGNC = r"rest\.genenames\.org"
EMPTY = load_json("hgnc_empty.json")


def hgnc_routes(
    router: Router, symbol: dict | None = None, alias: dict | None = None, prev: dict | None = None
) -> None:
    router.json("GET", HGNC + r"/info$", load_json("hgnc_info.json"))
    router.json("GET", HGNC + r"/fetch/symbol/", symbol or EMPTY)
    router.json("GET", HGNC + r"/fetch/alias_symbol/", alias or EMPTY)
    router.json("GET", HGNC + r"/fetch/prev_symbol/", prev or EMPTY)


async def test_previous_symbol_resolves_with_explicit_transformation(
    router: Router, make_service
) -> None:
    hgnc_routes(router, prev=load_json("hgnc_prev_symbol_FANCD1.json"))
    service = make_service()
    result = await service.resolve_identifier(ResolveIdentifierRequest(identifier="FANCD1"))
    assert result.status == "ok"
    gene = result.resolved["gene"]
    assert (gene["symbol"], gene["hgnc_id"], gene["ensembl_gene_id"]) == (
        "BRCA2",
        "HGNC:1101",
        "ENSG00000139618",
    )
    assert result.resolved["match_type"] == "previous_symbol"
    assert [t.operation for t in result.transformations] == ["previous_symbol_to_approved"]
    ev = result.evidence[0]
    assert ev.source == "hgnc" and ev.source_record_id == "HGNC:1101"
    assert ev.source_release == "HGNC index lastModified 2026-09-18T12:19:06.043Z"
    assert ev.source_updated_at == "2023-01-20T00:00:00Z"
    assert "FANCD1" in ev.data["prev_symbol"]


async def test_approved_symbol_reports_alias_collisions(router: Router, make_service) -> None:
    hgnc_routes(
        router,
        symbol=load_json("hgnc_symbol_CAP2.json"),
        alias=load_json("hgnc_alias_symbol_CAP2.json"),
    )
    result = await make_service().resolve_identifier(ResolveIdentifierRequest(identifier="CAP2"))
    assert result.status == "ok"
    assert result.resolved["gene"]["symbol"] == "CAP2"
    assert result.resolved["match_type"] == "approved_symbol"
    assert {c.label for c in result.candidates} == {"SERPINB8", "TMPRSS4"}
    assert sum("also a alias symbol" in w for w in result.warnings) == 2


async def test_alias_shared_by_two_genes_is_ambiguous(router: Router, make_service) -> None:
    hgnc_routes(router, alias=load_json("hgnc_alias_symbol_p16.json"))
    result = await make_service().lookup_gene(LookupGeneRequest(gene="p16"))
    assert result.status == "ambiguous"
    assert result.gene is None
    assert {c.label for c in result.candidates} == {"H3P10", "CDKN2A"}
    assert all(c.match_type == "alias_symbol" for c in result.candidates)
    # Nothing downstream was queried for an ambiguous gene.
    assert not router.called(r"ensembl|uniprot|opentargets|gnomad")


async def test_unknown_symbol_is_unresolved(router: Router, make_service) -> None:
    hgnc_routes(router)
    result = await make_service().resolve_identifier(
        ResolveIdentifierRequest(identifier="NOTAGENE1")
    )
    assert result.status == "unresolved"
    assert "HGNC has no record for 'NOTAGENE1'" in result.warnings


async def test_versioned_ensembl_gene_id_drops_version_only_for_hgnc(
    router: Router, make_service
) -> None:
    router.json("GET", HGNC + r"/info$", load_json("hgnc_info.json"))
    router.json(
        "GET",
        HGNC + r"/fetch/ensembl_gene_id/ENSG00000139618$",
        load_json("hgnc_symbol_BRCA2.json"),
    )
    result = await make_service().resolve_identifier(
        ResolveIdentifierRequest(identifier="ENSG00000139618.17")
    )
    assert result.status == "ok"
    assert result.transformations[0].operation == "drop_version_for_lookup"
    assert result.resolved["gene"]["symbol"] == "BRCA2"


async def test_hgnc_release_failure_does_not_fail_resolution(router: Router, make_service) -> None:
    router.add("GET", HGNC + r"/info$", __import__("httpx").Response(503, json={"error": "down"}))
    router.json("GET", HGNC + r"/fetch/symbol/", load_json("hgnc_symbol_BRCA2.json"))
    router.json("GET", HGNC + r"/fetch/alias_symbol/", EMPTY)
    router.json("GET", HGNC + r"/fetch/prev_symbol/", EMPTY)
    result = await make_service().resolve_identifier(ResolveIdentifierRequest(identifier="BRCA2"))
    assert result.status == "partial"
    assert result.resolved["gene"]["hgnc_id"] == "HGNC:1101"
    assert result.evidence[0].source_release is None
    assert [(e.source, e.operation) for e in result.errors] == [("hgnc", "info")]
