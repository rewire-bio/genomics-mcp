"""E8 provider: public reference evidence and variant normalization.

`register(registry)` is called by core provider loading. It registers one planner per
reference operation (fanout dispatch, key "default"), the reference sources, a
`reference_evidence` facade for composition (E9) and a shutdown hook.

Settings are respected per call: disabled sources are never contacted, keys come only
from explicitly configured environment variable names (`sources.<name>.api_key_env`),
and configured source timeouts/rates can only tighten the documented limits. Rate
limiters persist across calls.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

import httpx

from genomics_mcp.config import Settings
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import ErrorCode, ErrorInfo, InvalidInputError
from genomics_mcp.evidence.local_fasta import SOURCE as LOCAL_FASTA
from genomics_mcp.evidence.local_fasta import LocalFastaProvider, fasta_assembly
from genomics_mcp.evidence.mapping import to_output
from genomics_mcp.models import FileRef, SourceState, SourceStatus, VariantSpec
from genomics_mcp.public import EgressContext
from genomics_mcp.references import schemas as ref
from genomics_mcp.references.assemblies import AssemblyError, normalize_assembly
from genomics_mcp.references.http import USER_AGENT
from genomics_mcp.references.models import Evidence
from genomics_mcp.references.service import ReferenceConfig, ReferenceService, selected_sources
from genomics_mcp.references.sources import SOURCES
from genomics_mcp.registry import DEFAULT_KEY, Operation, Registry, SourceInfo
from genomics_mcp.requests import (
    LookupGeneRequest,
    LookupProteinRequest,
    LookupVariantRequest,
    NormalizeVariantRequest,
    ResolveIdentifierRequest,
)
from genomics_mcp.result import OperationOutput

__all__ = ["ReferenceEvidenceFacade", "ReferenceRuntime", "register"]

REFERENCE_SOURCES = (
    "hgnc", "ensembl", "clinvar", "ncbi_variation", "ncbi_nuccore",
    "gnomad", "uniprot", "open_targets", "alphagenome_atlas",
)  # fmt: skip

# Sources each operation can use. `sources` outside these are reported as not implemented.
OP_SOURCES: dict[Operation, tuple[str, ...]] = {
    Operation.NORMALIZE_VARIANT: ("ensembl", "ncbi_nuccore", "ncbi_variation", LOCAL_FASTA),
    Operation.LOOKUP_VARIANT: ("ensembl", "clinvar", "gnomad", "alphagenome_atlas"),
    Operation.LOOKUP_GENE: ("hgnc", "ensembl", "uniprot", "open_targets", "gnomad"),
    Operation.LOOKUP_PROTEIN: ("hgnc", "ensembl", "uniprot"),
    Operation.RESOLVE_IDENTIFIER: (
        "hgnc", "ensembl", "uniprot", "clinvar", "ncbi_variation", "ncbi_nuccore",
    ),
}  # fmt: skip
DEFAULT_VARIANT_SOURCES = ("ensembl", "clinvar", "gnomad")
INCLUDE_TO_SOURCE = {
    "consequence": "ensembl",
    "gene_context": "ensembl",
    "clinical": "clinvar",
    "population": "gnomad",
    "prediction": "alphagenome_atlas",
    "functional_prediction": "alphagenome_atlas",
}
GENE_INCLUDES = {"identifiers", "transcripts", "protein", "disease", "constraint"}
TARGET_KEYS = {
    "gene": ("gene", "match_type"),
    "hgnc": ("gene", "match_type"),
    "ensembl_gene": ("gene",),
    "entrez": ("gene",),
    "protein": ("uniprot", "uniprot_reviewed"),
    "uniprot": ("uniprot", "uniprot_reviewed"),
    "transcript": ("ensembl",),
    "ensembl_transcript": ("ensembl",),
    "ensembl": ("ensembl", "gene"),
    "variant": ("variant", "alleles"),
    "clinvar": ("clinvar", "locations"),
}
SUPPORT_NOTE = (
    "Normalization support: reference bases and rsID/HGVS resolution. Consulted by "
    "lookup_variant regardless of its `sources`, which select annotation sources."
)


def atlas_sdk_installed() -> bool:
    return importlib.util.find_spec("alphagenome") is not None


def config_from_settings(settings: Settings) -> ReferenceConfig:
    """Explicit configuration only; keys are read from configured env var names, never ambient."""
    enabled = frozenset(s for s in REFERENCE_SOURCES if settings.source(s).enabled)
    ncbi = settings.source("clinvar")
    atlas_key = (
        settings.source_api_key("alphagenome_atlas")
        if settings.source("alphagenome_atlas").enabled
        else None
    )
    timeouts = {
        s: settings.source(s).timeout_s for s in REFERENCE_SOURCES if settings.source(s).timeout_s
    }
    rpm = {
        s: settings.source(s).requests_per_minute
        for s in REFERENCE_SOURCES
        if settings.source(s).requests_per_minute
    }
    return ReferenceConfig(
        ncbi_api_key=settings.source_api_key("clinvar"),
        ncbi_email=ncbi.contact_email,
        atlas_api_key=atlas_key,
        deadline_seconds=settings.limits.interactive_timeout_s,
        timeouts=dict(timeouts),
        source_deadlines=dict(timeouts),
        enabled_sources=enabled,
        requests_per_minute=dict(rpm),  # type: ignore[arg-type]
    )


class ReferenceRuntime:
    """Process-wide state: one HTTP client and one ReferenceService per effective configuration,
    so rate limiters persist across calls. Tests inject a client/transport here."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        atlas_transport: Any = None,
        service_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._atlas_transport = atlas_transport
        self._service_kwargs = service_kwargs or {}
        self._services: dict[str, ReferenceService] = {}

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # httpx logs full request URLs (query strings, including an NCBI api_key) at INFO.
            logging.getLogger("httpx").setLevel(logging.WARNING)
            self._client = httpx.AsyncClient(
                trust_env=False, follow_redirects=False, headers={"User-Agent": USER_AGENT}
            )
        return self._client

    def service(self, settings: Settings) -> ReferenceService:
        key = settings.model_dump_json(include={"sources", "limits"})
        svc = self._services.get(key)
        if svc is None:
            svc = ReferenceService(
                self.client(),
                config_from_settings(settings),
                atlas_transport=self._atlas_transport,
                **self._service_kwargs,
            )
            self._services[key] = svc
        return svc

    async def aclose(self) -> None:
        for svc in self._services.values():
            transport = getattr(svc._atlas, "transport", None)
            if transport is not None and hasattr(transport, "aclose"):
                await transport.aclose()
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        self._services.clear()


# --------------------------------------------------------------------------- helpers


def _runtime(ctx: OperationContext) -> ReferenceRuntime:
    return ctx.require_component("reference_runtime")


def _budget(ctx: OperationContext) -> float:
    """Leave a small reserve inside the call deadline for mapping and serialization."""
    left = ctx.deadline.remaining()
    return max(0.0, left - min(0.5, 0.1 * left))


def _select(
    op: Operation, requested: list[str] | None
) -> tuple[list[str] | None, list[ErrorInfo], list[SourceStatus]]:
    """Validate `sources`. Unknown names are reported per source, never silently dropped."""
    if requested is None:
        return None, [], []
    allowed = OP_SOURCES[op]
    chosen: list[str] = []
    errors: list[ErrorInfo] = []
    statuses: list[SourceStatus] = []
    for raw in dict.fromkeys(s.strip().lower() for s in requested):
        if raw in allowed:
            chosen.append(raw)
            continue
        msg = f"{op.value} has no {raw!r} source in this build; available: {', '.join(allowed)}"
        errors.append(ErrorInfo(code=ErrorCode.UNSUPPORTED, message=msg, source=raw))
        statuses.append(SourceStatus(source=raw, state=SourceState.NOT_IMPLEMENTED, message=msg))
    if not chosen:
        raise InvalidInputError(
            f"none of the requested sources are available for {op.value}",
            details={"requested": requested, "available": list(allowed)},
        )
    return chosen, errors, statuses


def _assembly(value: str | None, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    try:
        return normalize_assembly(value, [])
    except AssemblyError as exc:
        raise InvalidInputError(str(exc)) from None


def _variant_value(
    variant: VariantSpec | str | None, hgvs: str | None, rsid: str | None
) -> ref.GenomicAlleleInput | str:
    if isinstance(variant, VariantSpec):
        return ref.GenomicAlleleInput(
            assembly=variant.assembly, contig=variant.contig, position=variant.pos,
            ref=variant.ref, alt=variant.alt,
        )  # fmt: skip
    text = variant if isinstance(variant, str) else (hgvs or rsid)
    if not text:
        raise InvalidInputError("give exactly one of variant, hgvs, rsid")
    return text


def _origin(egress: EgressContext) -> dict[str, Any]:
    if egress.derived_from_private:
        return {"query_origin": "private_file", "allow_external_queries": egress.consent}
    return {"query_origin": "user_supplied", "allow_external_queries": False}


def _dump(value: Any) -> Any:
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _variant_evidence(ev: ref.VariantEvidence) -> list[Evidence]:
    """Record order for budget trimming: summaries first, individual ClinVar SCVs last."""
    head = [*ev.consequence, *(e for e in ev.clinical if e.evidence_type != "clinical_assertion"),
            *ev.population, *ev.functional_prediction]  # fmt: skip
    return [*head, *(e for e in ev.clinical if e.evidence_type == "clinical_assertion")]


# --------------------------------------------------------------------------- operations


async def normalize(
    ctx: OperationContext,
    variant: VariantSpec | str | None,
    *,
    hgvs: str | None = None,
    rsid: str | None = None,
    assembly: str | None = None,
    reference: FileRef | None = None,
    sources: list[str] | None = None,
    egress: EgressContext,
) -> OperationOutput:
    selection, errors, statuses = _select(Operation.NORMALIZE_VARIANT, sources)
    value = _variant_value(variant, hgvs, rsid)
    asm = _assembly(
        assembly or (value.assembly if isinstance(value, ref.GenomicAlleleInput) else None)
    )
    provider = None
    if reference is not None:
        if selection is not None and LOCAL_FASTA not in selection:
            raise InvalidInputError("reference was given but local_fasta is not among `sources`")
        provider = LocalFastaProvider(ctx, reference, fasta_assembly(reference, asm))
    request = ref.NormalizeVariantRequest(
        variant=value, assembly=asm, use_remote_reference=True, **_origin(egress)
    )
    svc = _runtime(ctx).service(ctx.settings)
    with selected_sources(
        None if selection is None else [s for s in selection if s != LOCAL_FASTA]
    ):
        result = await svc.normalize_variant(
            request, deadline_s=_budget(ctx), reference_provider=provider
        )
    data = {
        "input_kind": result.input_kind,
        "canonical_variant": _dump(result.canonical_variant),
        "alleles": _dump(result.alleles),
        "candidates": _dump(result.candidates),
    }
    if provider is not None and provider.transformations:
        data["local_reference_steps"] = _dump(provider.transformations)
    return to_output(result, result.evidence, data, max_records=ctx.limits.max_records,
                     extra_errors=errors, extra_statuses=statuses)  # fmt: skip


async def lookup_variant(
    ctx: OperationContext,
    variant: VariantSpec | str | None,
    *,
    hgvs: str | None = None,
    rsid: str | None = None,
    assembly: str | None = None,
    sources: list[str] | None = None,
    include: list[str] | None = None,
    egress: EgressContext,
    reference: FileRef | None = None,
) -> OperationOutput:
    selection, errors, statuses = _select(Operation.LOOKUP_VARIANT, sources)
    chosen = list(selection) if selection is not None else list(DEFAULT_VARIANT_SOURCES)
    if include is not None:
        unknown = [i for i in include if i not in INCLUDE_TO_SOURCE]
        if unknown:
            raise InvalidInputError(
                f"unknown include value(s) {unknown}; use {sorted(INCLUDE_TO_SOURCE)}"
            )
        wanted = {INCLUDE_TO_SOURCE[i] for i in include}
        if selection is None:
            chosen = [s for s in OP_SOURCES[Operation.LOOKUP_VARIANT] if s in wanted]
        else:
            chosen = [s for s in chosen if s in wanted]
    value = _variant_value(variant, hgvs, rsid)
    asm = _assembly(
        assembly or (value.assembly if isinstance(value, ref.GenomicAlleleInput) else None)
    )
    provider = (
        LocalFastaProvider(ctx, reference, fasta_assembly(reference, asm)) if reference else None
    )
    request = ref.LookupVariantRequest(
        variant=value, assembly=asm, sources=chosen, use_remote_reference=True, **_origin(egress)
    )
    svc = _runtime(ctx).service(ctx.settings)
    result = await svc.lookup_variant(request, deadline_s=_budget(ctx), reference_provider=provider)
    norm = result.normalization
    data = {
        "canonical_variant": _dump(result.canonical_variant),
        "sources_requested": chosen,
        "normalization": {
            "result_status": norm.status,
            "input_kind": norm.input_kind,
            "alleles": _dump(norm.alleles),
            "candidates": _dump(norm.candidates),
            "transformations": _dump(norm.transformations),
            "limitations": norm.limitations,
            "warnings": norm.warnings,
        },
        "gene_context": result.gene_context,
    }
    return to_output(result, _variant_evidence(result.evidence), data,
                     max_records=ctx.limits.max_records, extra_errors=errors, extra_statuses=statuses)  # fmt: skip


async def _gene(req: LookupGeneRequest, ctx: OperationContext) -> OperationOutput:
    selection, errors, statuses = _select(Operation.LOOKUP_GENE, req.sources)
    include = req.include if req.include is not None else sorted(GENE_INCLUDES)
    bad = [i for i in include if i not in GENE_INCLUDES]
    if bad:
        raise InvalidInputError(f"unknown include value(s) {bad}; use {sorted(GENE_INCLUDES)}")
    asm = _assembly(req.assembly, default="GRCh38")
    request = ref.LookupGeneRequest(gene=req.gene, assembly=asm, include=include)  # type: ignore[arg-type]
    with selected_sources(selection):
        result = (
            await _runtime(ctx).service(ctx.settings).lookup_gene(request, deadline_s=_budget(ctx))
        )
    if req.assembly is None:
        result.warnings.append(
            "assembly not given; GRCh38 used for gene coordinates and constraint"
        )
    data = {"gene": result.gene, "candidates": _dump(result.candidates), "assembly": asm}
    return to_output(result, result.evidence, data, max_records=ctx.limits.max_records,
                     extra_errors=errors, extra_statuses=statuses)  # fmt: skip


async def _protein(req: LookupProteinRequest, ctx: OperationContext) -> OperationOutput:
    selection, errors, statuses = _select(Operation.LOOKUP_PROTEIN, req.sources)
    request = ref.LookupProteinRequest(protein=req.protein)
    with selected_sources(selection):
        result = (
            await _runtime(ctx)
            .service(ctx.settings)
            .lookup_protein(request, deadline_s=_budget(ctx))
        )
    data = {"protein": result.protein, "candidates": _dump(result.candidates)}
    return to_output(result, result.evidence, data, max_records=ctx.limits.max_records,
                     extra_errors=errors, extra_statuses=statuses)  # fmt: skip


async def _resolve(req: ResolveIdentifierRequest, ctx: OperationContext) -> OperationOutput:
    selection, errors, statuses = _select(Operation.RESOLVE_IDENTIFIER, req.sources)
    request = ref.ResolveIdentifierRequest(identifier=req.identifier, assembly=req.assembly)
    with selected_sources(selection):
        result = (
            await _runtime(ctx)
            .service(ctx.settings)
            .resolve_identifier(request, deadline_s=_budget(ctx))
        )
    resolved = result.resolved
    if req.target_types:
        unknown = [t for t in req.target_types if t not in TARGET_KEYS]
        if unknown:
            result.warnings.append(f"unknown target_types ignored: {unknown}")
        keep = {k for t in req.target_types for k in TARGET_KEYS.get(t, ())}
        resolved = {k: v for k, v in resolved.items() if k in keep}
    data = {
        "identifier_type": result.identifier_type,
        "resolved": resolved,
        "candidates": _dump(result.candidates),
        "variant_candidates": _dump(result.variant_candidates),
    }
    return to_output(result, result.evidence, data, max_records=ctx.limits.max_records,
                     extra_errors=errors, extra_statuses=statuses)  # fmt: skip


async def _normalize_handler(
    req: NormalizeVariantRequest, ctx: OperationContext
) -> OperationOutput:
    return await normalize(ctx, req.variant, hgvs=req.hgvs, rsid=req.rsid, assembly=req.assembly,
                           reference=req.reference, sources=req.sources, egress=EgressContext.public())  # fmt: skip


async def _lookup_variant_handler(
    req: LookupVariantRequest, ctx: OperationContext
) -> OperationOutput:
    return await lookup_variant(ctx, req.variant, hgvs=req.hgvs, rsid=req.rsid, assembly=req.assembly,
                                sources=req.sources, include=req.include, egress=EgressContext.public())  # fmt: skip


class ReferenceEvidenceFacade:
    """Entry point for composition (E9), available as `ctx.component("reference_evidence")`.

    Pass `egress=ctx.egress_for(files)` when the variant came from private files: without
    per-call consent nothing is sent to any external service (local FASTA checks still run)
    and external steps return `consent_required`.
    """

    async def normalize_variant(
        self,
        ctx: OperationContext,
        variant: VariantSpec | str,
        *,
        egress: EgressContext,
        assembly: str | None = None,
        reference: FileRef | None = None,
        sources: list[str] | None = None,
    ) -> OperationOutput:
        return await normalize(ctx, variant, assembly=assembly, reference=reference,
                               sources=sources, egress=egress)  # fmt: skip

    async def lookup_variant(
        self,
        ctx: OperationContext,
        variant: VariantSpec | str,
        *,
        egress: EgressContext,
        assembly: str | None = None,
        sources: list[str] | None = None,
        include: list[str] | None = None,
        reference: FileRef | None = None,
    ) -> OperationOutput:
        return await lookup_variant(ctx, variant, assembly=assembly, sources=sources, include=include,
                                    egress=egress, reference=reference)  # fmt: skip


HANDLERS = {
    Operation.RESOLVE_IDENTIFIER: _resolve,
    Operation.NORMALIZE_VARIANT: _normalize_handler,
    Operation.LOOKUP_VARIANT: _lookup_variant_handler,
    Operation.LOOKUP_GENE: _gene,
    Operation.LOOKUP_PROTEIN: _protein,
}

_HOMEPAGES = {
    "hgnc": "https://www.genenames.org/",
    "ensembl": "https://rest.ensembl.org/",
    "clinvar": "https://www.ncbi.nlm.nih.gov/clinvar/",
    "ncbi_variation": "https://api.ncbi.nlm.nih.gov/variation/v0/",
    "ncbi_nuccore": "https://www.ncbi.nlm.nih.gov/nuccore/",
    "gnomad": "https://gnomad.broadinstitute.org/",
    "uniprot": "https://www.uniprot.org/",
    "open_targets": "https://platform.opentargets.org/",
    "alphagenome_atlas": "https://www.alphagenomedocs.com/api/atlas.html",
}


def source_infos() -> list[SourceInfo]:
    out = []
    for name in REFERENCE_SOURCES:
        info = SOURCES[name]
        ops = [op.value for op, srcs in OP_SOURCES.items() if name in srcs]
        if name in ("ncbi_variation", "ncbi_nuccore") and "lookup_variant" not in ops:
            ops.append("lookup_variant")
        notes = f"{info.rate_limit}. " + " ".join(info.notes)
        auth: Any = "none"
        if name == "clinvar":
            auth = "optional_key"
        if name == "alphagenome_atlas":
            auth = "required_key"
            if not atlas_sdk_installed():
                notes += " The optional 'alphagenome' package is not installed (install the 'atlas' extra)."
        if name in ("ncbi_variation", "ncbi_nuccore"):
            notes += " " + SUPPORT_NOTE
        out.append(
            SourceInfo(
                name=name,
                title=info.title,
                kind="reference",
                planned_epic="E8",
                homepage=_HOMEPAGES[name],
                terms_url=info.terms_url,
                auth=auth,
                notes=notes.strip(),
                operations=sorted(ops),
            )
        )
    return out


def register(registry: Registry, *, runtime: ReferenceRuntime | None = None) -> None:
    rt = runtime or ReferenceRuntime()
    for info in source_infos():
        registry.register_source(info)
    for op, handler in HANDLERS.items():
        registry.register(op, DEFAULT_KEY, handler, provider=__name__,
                          description="E8 reference planner")  # fmt: skip
    registry.provide("reference_runtime", rt)
    registry.provide("reference_evidence", ReferenceEvidenceFacade())
    registry.on_shutdown(rt.aclose)
