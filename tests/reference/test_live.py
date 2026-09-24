"""Live checks against public services. Opt in with GENOMICS_MCP_LIVE=1.

These make a handful of anonymous requests within documented rate limits. Live
data changes; assertions check identity and structure, not current counts.
"""

from __future__ import annotations

import os

import httpx
import pytest

from genomics_mcp.references import ReferenceService
from genomics_mcp.references.schemas import (
    LookupGeneRequest,
    LookupProteinRequest,
    LookupVariantRequest,
    ResolveIdentifierRequest,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("GENOMICS_MCP_LIVE") != "1", reason="set GENOMICS_MCP_LIVE=1"
    ),
]


async def service() -> ReferenceService:
    return ReferenceService(httpx.AsyncClient())


async def test_live_hgnc_previous_symbol() -> None:
    result = await (await service()).resolve_identifier(
        ResolveIdentifierRequest(identifier="FANCD1")
    )
    assert result.resolved["gene"]["hgnc_id"] == "HGNC:1101"
    assert result.resolved["match_type"] == "previous_symbol"
    assert result.evidence[0].source_release.startswith("HGNC index lastModified")


async def test_live_uniprot_entry() -> None:
    result = await (await service()).lookup_protein(LookupProteinRequest(protein="P51587"))
    assert result.status == "ok"
    ev = result.evidence[0]
    assert ev.data["reviewed"] is True and ev.data["sequence"]["length"] == 3418
    assert ev.source_release and ev.source_release.startswith("UniProt ")


async def test_live_clinvar_vcv_assertions() -> None:
    result = await (await service()).lookup_variant(
        LookupVariantRequest(variant="7-140753336-A-T", assembly="GRCh38", sources=["clinvar"])
    )
    clinical = result.evidence.clinical
    assert clinical[0].data["matched_variation_ids"] == ["13961"]
    record = clinical[1]
    assert record.source_record_id == "VCV000013961" and record.source_record_version
    assert {"germline", "somatic_clinical_impact", "oncogenicity"} <= set(
        record.data["aggregate_classifications"]
    )
    assert sum(e.evidence_type == "clinical_assertion" for e in clinical) >= 40
    # The reference check may come from Ensembl or, if Ensembl is failing, NCBI; failures stay visible.
    assert result.canonical_variant.reference_check.status == "verified"


async def test_live_gnomad_open_targets_and_ensembl_outcomes_are_explicit() -> None:
    svc = await service()
    variant = await svc.lookup_variant(
        LookupVariantRequest(
            variant="7-140753336-A-T", assembly="GRCh38", sources=["gnomad", "ensembl"]
        )
    )
    if variant.evidence.population:
        assert variant.evidence.population[0].data["exome"]["an"] > 0
    if not variant.evidence.consequence:
        assert any(e.source == "ensembl" for e in variant.errors)
    gene = await svc.lookup_gene(
        LookupGeneRequest(gene="BRCA2", include=["identifiers", "disease"])
    )
    assert any(e.source == "open_targets" for e in gene.evidence) or any(
        e.source == "open_targets" for e in gene.errors
    )


async def test_live_through_mcp_server(tmp_path) -> None:
    """Full path: MCP client -> server -> GenomicsService -> evidence provider -> public APIs."""
    from mcp.client import Client

    from genomics_mcp.config import load_settings
    from genomics_mcp.server import build_server
    from genomics_mcp.service import GenomicsService

    settings = load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path / "work")}})
    svc = GenomicsService(settings)
    try:
        async with Client(build_server(svc)) as client:
            res = await client.call_tool(
                "lookup_variant",
                {
                    "variant": {"assembly": "GRCh38", "contig": "7", "pos": 140753336,
                                "ref": "A", "alt": "T"},
                    "sources": ["clinvar", "gnomad", "ensembl"],
                },
            )  # fmt: skip
            body = res.structured_content
            assert body["status"] in ("ok", "partial")
            types = [r["evidence_type"] for r in body["data"]["records"]]
            assert "clinical_variant_record" in types
            assert types.count("clinical_assertion") >= 40
            states = {s["source"]: s["state"] for s in body["source_status"]}
            # Ensembl GRCh38 may be down; it must then be reported, never replaced by GRCh37.
            assert "ensembl" in states
            assert all(
                "grch37" not in (r["provenance"].get("url") or "") for r in body["data"]["records"]
            )
            gene = await client.call_tool(
                "lookup_gene", {"gene": "BRCA2", "include": ["identifiers", "disease"]}
            )
            assert gene.structured_content["data"]["gene"]["hgnc_id"] == "HGNC:1101"
    finally:
        await svc.aclose()
