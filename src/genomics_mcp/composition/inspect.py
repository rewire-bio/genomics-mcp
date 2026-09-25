"""inspect_locus: bounded data from every given file at one interval, plus opt-in evidence.

Per file format:

| Format | Components (registered handler) |
| --- | --- |
| bam, cram | `get_reads` (no sequences or qualities) and `get_coverage` (binned + summary) |
| vcf, bcf | `get_variants` with genotypes (first `MAX_SAMPLES_INLINE` samples per record) |
| fasta (and `reference`) | `get_sequence`, only for intervals up to `SEQUENCE_MAX_BP` |
| bed, gff3, gtf, bigbed | `get_features` |
| bigwig | `get_signal` (binned mean + summary) |

Anything else is an explicit per-file error. Every component's records go to the top-level
`data.records` as `{component, type, record}`, interleaved across components, so the
response byte budget trims evenly instead of dropping whole files.
"""

from __future__ import annotations

from typing import Any

from genomics_mcp.composition.annotate import annotate
from genomics_mcp.composition.common import (
    Component,
    FileSlot,
    bounded_meta,
    check_assemblies,
    configured_timeout,
    dispatch,
    fit_metadata,
    interleave,
    jsonable,
    make_slot,
    overall_truncation,
    plan_slots,
    run_components,
    share,
    source_gate,
    tag,
    with_max_records,
)
from genomics_mcp.context import OperationContext, state_for
from genomics_mcp.errors import ErrorCode, ErrorInfo, InvalidInputError
from genomics_mcp.models import FileFormat, Interval, SourceState, SourceStatus
from genomics_mcp.registry import Operation
from genomics_mcp.requests import (
    CoverageRequest,
    FeaturesRequest,
    InspectLocusRequest,
    ReadsRequest,
    SequenceRequest,
    SignalRequest,
    VariantsRequest,
)
from genomics_mcp.result import OperationOutput

READS_CAP = 200
VARIANTS_CAP = 500
FEATURES_CAP = 200
TARGET_BINS = 20
MAX_SAMPLES_INLINE = 20
SEQUENCE_MAX_BP = 10_000
PHASE_ONE_SHARE = 0.6
"""With annotation requested, file reads may use this share of the remaining deadline."""

_FEATURES = {FileFormat.BED, FileFormat.GFF3, FileFormat.GTF, FileFormat.BIGBED}
_NOT_LOCUS = {
    FileFormat.FASTQ: "FASTQ is not queryable by locus; fetch_file can download it",
    FileFormat.TSV: "TSV files are not queryable by locus",
    FileFormat.OTHER: "this file format is not queryable by locus",
}


def _sequence_cap(ctx: OperationContext) -> int:
    return min(SEQUENCE_MAX_BP, ctx.limits.max_response_bytes // 8)


def _bins(total_records: int, parts: int, length: int) -> int:
    return max(1, min(TARGET_BINS, share(total_records, parts, TARGET_BINS), length))


def _plan_file(slot: FileSlot, iv: Interval, req: InspectLocusRequest) -> list[Component]:
    f, fmt = slot.file, slot.fmt
    if fmt in (FileFormat.BAM, FileFormat.CRAM, FileFormat.SAM):
        # CRAM: the file's own reference_uri wins; else the call's reference, which the reader
        # accepts only if its MD5 matches the CRAM header.
        cram_ref = req.reference if fmt is FileFormat.CRAM and not f.reference_uri else None
        reads = Component(f"{slot.id}.reads", "alignments", Operation.GET_READS, slot)
        reads.run = lambda sub: dispatch(
            with_max_records(sub, reads.record_cap), Operation.GET_READS,
            ReadsRequest(file=f, interval=iv, reference=cram_ref, include_sequence=False),
        )  # fmt: skip
        reads.notes.append("read sequences and base qualities are not included")
        comps = [reads]
        if cram_ref is not None:
            reads.notes.append("CRAM decoded with the call's reference (checked against header M5)")
        if fmt is not FileFormat.SAM:
            cov = Component(f"{slot.id}.coverage", "coverage", Operation.GET_COVERAGE, slot)
            cov.run = lambda sub: dispatch(
                with_max_records(sub, cov.record_cap), Operation.GET_COVERAGE,
                CoverageRequest(file=f, interval=iv, reference=cram_ref,
                                bin_size=-(-iv.length // cov.record_cap)),
            )  # fmt: skip
            comps.append(cov)
        return comps
    if fmt in (FileFormat.VCF, FileFormat.BCF):
        var = Component(f"{slot.id}.variants", "variants", Operation.GET_VARIANTS, slot)
        var.run = lambda sub: dispatch(
            with_max_records(sub, var.record_cap), Operation.GET_VARIANTS,
            VariantsRequest(file=f, interval=iv, include_genotypes=True),
        )  # fmt: skip
        return [var]
    if fmt is FileFormat.FASTA:
        return [_sequence(slot, iv)]
    if fmt in _FEATURES:
        feat = Component(f"{slot.id}.features", "features", Operation.GET_FEATURES, slot)
        feat.run = lambda sub: dispatch(
            with_max_records(sub, feat.record_cap), Operation.GET_FEATURES,
            FeaturesRequest(file=f, interval=iv),
        )  # fmt: skip
        return [feat]
    if fmt is FileFormat.BIGWIG:
        sig = Component(f"{slot.id}.signal", "signal", Operation.GET_SIGNAL, slot)
        sig.run = lambda sub: dispatch(
            with_max_records(sub, sig.record_cap), Operation.GET_SIGNAL,
            SignalRequest(file=f, interval=iv, bins=sig.record_cap, summary="mean"),
        )  # fmt: skip
        return [sig]
    comp = Component(slot.id, "file", None, slot)
    if fmt is None:
        info = ErrorInfo(
            code=ErrorCode.INVALID_INPUT,
            message="cannot infer the file format from its name; set file.format",
        )
    else:
        info = ErrorInfo(code=ErrorCode.UNSUPPORTED, message=_NOT_LOCUS[fmt])
    comp.skip(info, state_for(info))
    return [comp]


def _sequence(slot: FileSlot, iv: Interval) -> Component:
    seq = Component(f"{slot.id}.sequence", "sequence", Operation.GET_SEQUENCE, slot)
    seq.run = lambda sub: dispatch(
        sub, Operation.GET_SEQUENCE, SequenceRequest(file=slot.file, interval=iv)
    )
    return seq


def _collect(c: Component) -> None:
    """Move a successful component's records to tagged top-level records; keep small metadata."""
    if not c.ok:
        return
    data: dict[str, Any] = c.output.data  # type: ignore[union-attr]
    records = list(data.get("records") or [])
    if c.kind == "variants":
        records = _limit_samples(c, data, records)
        c.meta.update(bounded_meta(data, drop=("samples",)))
    else:
        c.meta.update(bounded_meta(data))
    rtype = {
        "alignments": "alignment",
        "coverage": "coverage_bin",
        "variants": "variant",
        "sequence": "sequence",
        "features": "feature",
        "signal": "signal_bin",
    }[c.kind]
    if c.kind == "alignments":
        for r in records:
            r.pop("sequence", None)
            r.pop("base_qualities", None)
    c.records = [tag(c, rtype, jsonable(r)) for r in records]


def _limit_samples(c: Component, data: dict[str, Any], records: list[dict]) -> list[dict]:
    names = list(data.get("samples") or [])
    keep = names[:MAX_SAMPLES_INLINE]
    c.meta["samples_total"] = len(names)
    c.meta["samples_returned"] = keep
    if len(names) <= MAX_SAMPLES_INLINE:
        return records
    c.meta["samples_omitted"] = len(names) - len(keep)
    c.notes.append(
        f"genotypes are shown for the first {MAX_SAMPLES_INLINE} of {len(names)} samples in "
        "header order; use compare_samples or get_variants with `samples` for others"
    )
    wanted = set(keep)
    out = []
    for r in records:
        if isinstance(r.get("samples"), dict):
            r = {**r, "samples": {k: v for k, v in r["samples"].items() if k in wanted}}
        out.append(r)
    return out


def _consistency(components: list[Component]) -> tuple[dict[str, Any], list[ErrorInfo]]:
    lengths = {c.id: c.meta["contig_length"] for c in components if c.meta.get("contig_length")}
    distinct = sorted(set(lengths.values()))
    report: dict[str, Any] = {"contig_lengths": lengths, "consistent": len(distinct) <= 1}
    if len(distinct) <= 1:
        return report, []
    msg = (
        f"files report different lengths for the contig ({', '.join(map(str, distinct))}); "
        "they are probably not on the same build. Nothing was lifted over or renamed"
    )
    return report, [
        ErrorInfo(code=ErrorCode.INVALID_INPUT, message=msg, source="consistency",
                  details={"contig_lengths": lengths})
    ]  # fmt: skip


async def inspect_locus(req: InspectLocusRequest, ctx: OperationContext) -> OperationOutput:
    iv = req.interval
    if not req.files and req.reference is None and not req.reference_sources:
        raise InvalidInputError("nothing to inspect: give files, reference or reference_sources")
    ctx.check_region(iv)
    check_assemblies(iv, req.files, req.reference)
    slots, warnings = plan_slots(ctx, req.files)

    components: list[Component] = []
    ref_slot = None
    if req.reference is not None:
        ref_slot = make_slot("reference", -1, req.reference)
    for slot in ([ref_slot] if ref_slot else []) + slots:
        planned = [_sequence(slot, iv)] if slot is ref_slot else _plan_file(slot, iv, req)
        gate = source_gate(ctx, slot)
        if gate is not None:
            # keep planned roles and ids, mark disabled, never read
            for comp in planned:
                comp.run = None
                if comp.error is None:
                    comp.skip(gate, SourceState.DISABLED)
        components.extend(planned)

    seq_cap = _sequence_cap(ctx)
    for c in components:
        if c.kind == "sequence" and c.error is None and iv.length > seq_cap:
            c.run = None
            c.state = SourceState.SKIPPED
            c.notes.append(
                f"sequence not returned: the interval is {iv.length} bp and inspect_locus "
                f"returns at most {seq_cap} bp; use get_sequence"
            )
    runnable = [c for c in components if c.run is not None]
    total = ctx.limits.max_records
    caps = {"alignments": READS_CAP, "variants": VARIANTS_CAP, "features": FEATURES_CAP}
    for c in runnable:
        c.timeout_s = configured_timeout(ctx, c.slot)
        if c.kind in ("coverage", "signal"):
            c.record_cap = _bins(total, len(runnable), iv.length)
        else:
            c.record_cap = share(total, len(runnable), caps.get(c.kind, 1))

    variant_comps = [c for c in components if c.kind == "variants"]
    budget = None
    if req.reference_sources and variant_comps:
        budget = ctx.deadline.remaining() * PHASE_ONE_SHARE
    await run_components(ctx, components, budget_s=budget)
    for c in components:
        _collect(c)

    annotation = await annotate(
        ctx, req.reference_sources, variant_comps, req.reference,
        assembly=iv.assembly, had_variant_files=bool(variant_comps),
    )  # fmt: skip
    consistency, consistency_errors = _consistency(components)

    everything = components + annotation.components
    merged = interleave([c.records for c in everything])
    records, dropped = merged[:total], max(0, len(merged) - total)
    errors = [e for c in everything for e in c.errors()] + annotation.errors + consistency_errors
    statuses = [
        SourceStatus(source=c.id, state=c.state, message=c.error.message if c.error else None)
        for c in components
    ] + annotation.statuses
    provenance = [p for c in everything if c.output is not None for p in c.output.provenance]
    warnings += [
        f"{c.id}: {w}" for c in everything if c.output is not None for w in c.output.warnings
    ]
    if dropped:
        warnings.append(
            f"{dropped} records beyond max_records={total} were dropped evenly across components"
        )
    produced = any(c.ok for c in everything)
    if not produced and errors:
        return OperationOutput(
            data=None, errors=errors, source_status=statuses, provenance=provenance,
            warnings=warnings,
        )  # fmt: skip

    data = {
        "interval": iv,
        "assembly": {
            "requested": iv.assembly,
            "policy": "files must use this assembly exactly; no liftover, build substitution "
            "or contig renaming. Each component's `assembly` says whether it was only asserted "
            "(by the caller or file metadata) or declared in the file header",
        },
        "files": ([ref_slot.describe()] if ref_slot else []) + [s.describe() for s in slots],
        "components": [c.entry() for c in components],
        "annotation": {
            **annotation.summary,
            "components": [c.entry() for c in annotation.components],
        },
        "consistency": consistency,
        "record_layout": "records are {component, type, record}; `record` is the handler's "
        "native record; records are interleaved across components",
        "records": records,
    }
    fit_metadata(
        data, ctx.limits.max_response_bytes, [("components",), ("annotation", "components")]
    )
    return OperationOutput(
        data=data,
        errors=errors,
        source_status=statuses,
        provenance=provenance,
        warnings=warnings,
        truncation=overall_truncation(everything, len(records), total, dropped),
    )
