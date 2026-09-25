"""Reference evidence for inspect_locus, only through the E8 `reference_evidence` facade.

- The caller names sources in `reference_sources`; nothing external is consulted otherwise.
- Only alleles observed in the inspected VCF/BCF files are looked up. The reference
  sources in this build answer variant queries, not bare loci; a bare locus is reported
  as unanswerable rather than turned into an invented gene or variant.
- Consent is checked before every facade call. Values derived from any private file
  (including a private reference FASTA used for REF checks) are sent only when the call
  sets `allow_external_annotation`. The facade enforces the same rule again.
- At most `MAX_ANNOTATED_ALLELES` alleles are looked up, chosen deterministically in
  genomic order (POS, REF, ALT); the rest are listed as omitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from genomics_mcp.composition.common import (
    Component,
    FileSlot,
    run_components,
    with_max_records,
)
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import ErrorCode, ErrorInfo, InvalidInputError
from genomics_mcp.models import FileRef, SourceState, SourceStatus, VariantSpec
from genomics_mcp.result import OperationOutput

FACADE = "reference_evidence"
LOCAL_FASTA = "local_fasta"
# Sources the facade can answer for an observed allele.
LOOKUP_SOURCES = ("ensembl", "clinvar", "gnomad", "alphagenome_atlas")
ALLELE_SOURCES = (*LOOKUP_SOURCES, LOCAL_FASTA)
MAX_ANNOTATED_ALLELES = 5
MAX_EVIDENCE_PER_ALLELE = 50
MAX_OMITTED_LISTED = 20
_BASES = re.compile(r"^[ACGTNacgtn]+$")

BARE_LOCUS = (
    "the reference sources in this build answer variant queries, not bare loci; no variant "
    "was observed in the inspected files, so nothing was looked up (use lookup_gene or "
    "lookup_variant with an explicit identifier)"
)


@dataclass
class Allele:
    contig: str
    pos: int
    ref: str
    alt: str
    slots: list[FileSlot] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.contig}:{self.pos}:{self.ref}:{self.alt}"


@dataclass
class Annotation:
    components: list[Component] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    statuses: list[SourceStatus] = field(default_factory=list)
    errors: list[ErrorInfo] = field(default_factory=list)


def candidate_alleles(variant_components: list[Component]) -> tuple[list[Allele], int]:
    """Observed base-level alleles, deduplicated, in (POS, REF, ALT) order; count of skipped."""
    found: dict[tuple[str, int, str, str], Allele] = {}
    skipped = 0
    for c in variant_components:
        for tagged in c.records:
            rec = tagged["record"]
            ref = rec.get("ref") or ""
            for alt in rec.get("alts") or []:
                if not (_BASES.match(ref) and isinstance(alt, str) and _BASES.match(alt)):
                    skipped += 1  # symbolic, '*' or breakend alleles are not looked up
                    continue
                key = (rec["contig"], int(rec["pos"]), ref.upper(), alt.upper())
                allele = found.setdefault(key, Allele(*key))
                if c.slot is not None and c.slot not in allele.slots:
                    allele.slots.append(c.slot)
    ordered = sorted(found.values(), key=lambda a: (a.pos, a.ref, a.alt))
    return ordered, skipped


def _select_sources(ctx: OperationContext, requested: list[str]) -> tuple[list[str], Annotation]:
    note = Annotation()
    usable: list[str] = []
    for name in dict.fromkeys(s.strip().lower() for s in requested):
        if name not in ALLELE_SOURCES:
            msg = (
                f"{name!r} cannot answer a locus or an observed allele in inspect_locus; "
                f"usable: {', '.join(ALLELE_SOURCES)}"
            )
            note.errors.append(ErrorInfo(code=ErrorCode.UNSUPPORTED, message=msg, source=name))
            note.statuses.append(
                SourceStatus(source=name, state=SourceState.NOT_IMPLEMENTED, message=msg)
            )
        elif name != LOCAL_FASTA and not ctx.settings.source(name).enabled:
            note.statuses.append(
                SourceStatus(
                    source=name, state=SourceState.DISABLED, message="disabled in configuration"
                )
            )
        else:
            usable.append(name)
    return usable, note


async def annotate(
    ctx: OperationContext,
    requested: list[str] | None,
    variant_components: list[Component],
    reference: FileRef | None,
    *,
    assembly: str,
    had_variant_files: bool,
) -> Annotation:
    if not requested:
        return Annotation(
            summary={
                "requested": False,
                "note": "reference_sources not given; no external source was consulted",
            }
        )
    usable, result = _select_sources(ctx, requested)
    external = [s for s in usable if s in LOOKUP_SOURCES]
    local = LOCAL_FASTA in usable
    result.summary = {
        "requested": list(dict.fromkeys(s.strip().lower() for s in requested)),
        "consulted_for": "alleles observed in the inspected VCF/BCF files",
        "external_sources": external,
        "local_normalization": local,
        "allow_external_annotation": ctx.allow_external_annotation,
    }
    if not usable:
        return result
    if local and reference is None:
        info = ErrorInfo(
            code=ErrorCode.INVALID_INPUT,
            message="local_fasta needs `reference` (an indexed FASTA on the interval's assembly)",
            source=LOCAL_FASTA,
        )
        result.errors.append(info)
        result.statuses.append(
            SourceStatus(source=LOCAL_FASTA, state=SourceState.ERROR, message=info.message)
        )
        local = False
        usable = [s for s in usable if s != LOCAL_FASTA]
        if not usable:
            return result

    ok_variant_files = [c for c in variant_components if c.ok]
    if not had_variant_files or not ok_variant_files:
        reason = (
            BARE_LOCUS
            if not had_variant_files
            else "every variant file failed, so no observed allele could be looked up"
        )
        result.summary["status"] = "not_answerable"
        result.summary["reason"] = reason
        result.errors.append(
            ErrorInfo(code=ErrorCode.UNSUPPORTED, message=reason, source="reference_evidence")
        )
        result.statuses.extend(
            SourceStatus(source=s, state=SourceState.SKIPPED, message=reason) for s in usable
        )
        return result

    alleles, skipped = candidate_alleles(ok_variant_files)
    chosen = alleles[:MAX_ANNOTATED_ALLELES]
    omitted = alleles[MAX_ANNOTATED_ALLELES:]
    result.summary["selection"] = {
        "rule": f"first {MAX_ANNOTATED_ALLELES} distinct base-level alleles in (POS, REF, ALT) "
        "order across the variant files; ALTs of multi-allelic records are separate alleles",
        "observed_alleles": len(alleles),
        "selected": [a.key for a in chosen],
        "omitted_count": len(omitted),
        "omitted": [a.key for a in omitted[:MAX_OMITTED_LISTED]],
        "non_base_alleles_skipped": skipped,
    }
    if any(c.truncation() is not None for c in ok_variant_files):
        result.summary["selection"]["note"] = (
            "a variant file was truncated; alleles beyond its returned records were not considered"
        )
    if not chosen:
        result.summary["status"] = "no_alleles"
        result.statuses.extend(
            SourceStatus(
                source=s, state=SourceState.SKIPPED, message="no base-level allele observed"
            )
            for s in usable
        )
        return result

    facade = ctx.component(FACADE)
    if facade is None:
        info = ErrorInfo(
            code=ErrorCode.UNSUPPORTED,
            message="reference evidence is not available in this build",
            hint="planned in E8",
            source="reference_evidence",
        )
        result.errors.append(info)
        result.statuses.extend(
            SourceStatus(source=s, state=SourceState.NOT_IMPLEMENTED) for s in usable
        )
        return result

    per_allele = max(1, min(MAX_EVIDENCE_PER_ALLELE, ctx.limits.max_records // len(chosen)))
    blocked: list[str] = []
    for i, allele in enumerate(chosen):
        files = [s.file for s in allele.slots]
        if reference is not None:
            files.append(reference)  # REF checks and left-alignment read it
        egress = ctx.egress_for(files)
        consent_ok = not (egress.derived_from_private and not egress.consent)
        comp = Component(
            id=f"a{i}", kind="evidence", operation=None, record_cap=per_allele,
            meta={"allele": allele.key, "from_files": [s.id for s in allele.slots]},
        )  # fmt: skip
        if external and consent_ok:
            comp.meta["mode"] = "lookup_variant"
            comp.meta["sources"] = external
            comp.run = _lookup(
                facade, _spec(allele, assembly), external, reference, egress, per_allele
            )
        elif local:
            comp.meta["mode"] = "local_normalization"
            comp.meta["sources"] = [LOCAL_FASTA]
            comp.run = _normalize_locally(
                facade, _spec(allele, assembly), reference, egress, per_allele
            )
            if external:
                blocked.append(allele.key)
        else:
            blocked.append(allele.key)
            comp.meta["mode"] = "skipped_consent_required"
        result.components.append(comp)

    if blocked:
        result.summary["external_skipped"] = {
            "reason": "values derived from private files are not sent to external sources "
            "without allow_external_annotation=true on this call",
            "alleles": blocked,
            "sources": external,
        }
        result.errors.append(_consent_error(external))
        result.statuses.extend(
            SourceStatus(source=s, state=SourceState.SKIPPED, message="consent_required")
            for s in external
        )
    await run_components(ctx, result.components)
    for comp in result.components:
        if comp.output is not None and comp.output.data is not None:
            data = comp.output.data
            comp.records = [
                {"component": comp.id, "type": "evidence", "allele": comp.meta["allele"],
                 "record": r}
                for r in data.get("records", [])
            ]  # fmt: skip
            comp.meta["result"] = _facade_summary(data)
        if comp.output is not None:
            for st in comp.output.source_status:
                result.statuses.append(st.model_copy(update={"source": f"{comp.id}.{st.source}"}))
    return result


def _consent_error(sources: list[str]) -> ErrorInfo:
    return ErrorInfo(
        code=ErrorCode.CONSENT_REQUIRED,
        message="external annotation skipped: the alleles come from private files and "
        "allow_external_annotation is false; local data is still returned",
        source="reference_evidence",
        hint="pass allow_external_annotation=true to permit sending these alleles to "
        + (", ".join(sources) or "the requested sources"),
        details={"sources": sources},
    )


def _spec(allele: Allele, assembly: str) -> VariantSpec | InvalidInputError:
    try:
        return VariantSpec(
            assembly=assembly, contig=allele.contig, pos=allele.pos, ref=allele.ref, alt=allele.alt
        )
    except ValueError as exc:
        return InvalidInputError(f"allele {allele.key} cannot be looked up: {exc}")


def _lookup(facade: Any, spec: Any, sources: list[str], reference: Any, egress: Any, cap: int):
    async def run(sub: OperationContext) -> OperationOutput:
        if isinstance(spec, InvalidInputError):
            raise spec
        return await facade.lookup_variant(
            with_max_records(sub, cap), spec, egress=egress, sources=sources, reference=reference
        )

    return run


def _normalize_locally(facade: Any, spec: Any, reference: Any, egress: Any, cap: int):
    async def run(sub: OperationContext) -> OperationOutput:
        if isinstance(spec, InvalidInputError):
            raise spec
        return await facade.normalize_variant(
            with_max_records(sub, cap), spec, egress=egress, sources=[LOCAL_FASTA],
            reference=reference,
        )  # fmt: skip

    return run


def _facade_summary(data: dict[str, Any]) -> dict[str, Any]:
    keys = ("result_status", "canonical_variant", "normalization", "alleles", "limitations")
    return {k: data[k] for k in keys if k in data}
