"""Tool registry for core MCP wiring.

``call_tool(service, name, arguments)`` validates arguments, runs the tool and
returns a JSON-compatible dict. Invalid arguments produce a ``status="error"``
result with an ``invalid_input`` error rather than an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from .models import SourceError
from .schemas import (
    LookupGeneRequest,
    LookupGeneResult,
    LookupProteinRequest,
    LookupProteinResult,
    LookupVariantRequest,
    LookupVariantResult,
    NormalizeVariantRequest,
    NormalizeVariantResult,
    ResolveIdentifierRequest,
    ResolveIdentifierResult,
    ToolResult,
)
from .service import ReferenceService
from .sources import SOURCES


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    request_model: type[BaseModel]
    result_model: type[BaseModel]
    method: str


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "resolve_identifier",
            "Resolve a gene symbol/alias/previous symbol, HGNC/Ensembl/Entrez/UniProt/RefSeq ID, ClinVar VCV or a "
            "variant identifier to linked identifiers. Ambiguous inputs return candidates, not a guess.",
            ResolveIdentifierRequest,
            ResolveIdentifierResult,
            "resolve_identifier",
        ),
        ToolSpec(
            "normalize_variant",
            "Normalize a variant (structured allele, VCF string, SPDI, versioned HGVS g./c./n./p. or rsID) to one "
            "0-based half-open allele on an explicit assembly. REF is verified and indels are left-aligned only with "
            "actual reference sequence; no liftover.",
            NormalizeVariantRequest,
            NormalizeVariantResult,
            "normalize_variant",
        ),
        ToolSpec(
            "lookup_variant",
            "Normalize one allele, then fetch Ensembl VEP consequences, ClinVar VCV/SCV assertions (germline, somatic "
            "clinical impact and oncogenicity kept separate), gnomAD counts with denominators and, only if requested "
            "and configured, AlphaGenome Atlas precomputed predictions. Sources fail independently.",
            LookupVariantRequest,
            LookupVariantResult,
            "lookup_variant",
        ),
        ToolSpec(
            "lookup_gene",
            "HGNC nomenclature (aliases/previous symbols with ambiguity), versioned Ensembl gene/transcripts, "
            "UniProt entries, Open Targets associations and gnomAD constraint for one gene.",
            LookupGeneRequest,
            LookupGeneResult,
            "lookup_gene",
        ),
        ToolSpec(
            "lookup_protein",
            "UniProt entry identity, review status, function, locations and features for a UniProt accession, "
            "Ensembl/RefSeq protein or transcript, or gene.",
            LookupProteinRequest,
            LookupProteinResult,
            "lookup_protein",
        ),
    )
}


def input_schemas() -> dict[str, dict[str, Any]]:
    return {name: spec.request_model.model_json_schema() for name, spec in TOOLS.items()}


def output_schemas() -> dict[str, dict[str, Any]]:
    return {name: spec.result_model.model_json_schema() for name, spec in TOOLS.items()}


def source_status(service: ReferenceService) -> list[dict[str, Any]]:
    """Non-secret capability view for an MCP resource."""
    out = []
    for name, info in SOURCES.items():
        entry = info.model_dump(mode="json")
        if name == "alphagenome_atlas":
            configured = (
                service._atlas_transport is not None or service.config.atlas_api_key is not None
            )
            entry["status"] = "configured" if configured else "disabled (no API key configured)"
        else:
            entry["status"] = "enabled" if service._enabled(name) else "disabled"
        out.append(entry)
    return out


async def call_tool(
    service: ReferenceService, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if spec is None:
        raise KeyError(f"unknown reference tool {name!r}")
    try:
        request = spec.request_model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
            for e in exc.errors()[:10]
        )
        result = ToolResult(
            tool=name,
            status="error",
            query={"argument_keys": sorted(arguments)},
            errors=[
                SourceError(
                    source="local", operation="validate", kind="invalid_input", message=problems
                )
            ],
        )
        return result.model_dump(mode="json")
    result = await getattr(service, spec.method)(request)
    return result.model_dump(mode="json", exclude_none=True)
