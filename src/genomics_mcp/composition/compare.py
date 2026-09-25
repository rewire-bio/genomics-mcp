"""compare_samples: literal per-file and per-sample observations at one interval.

- BAM/CRAM: read depth (`get_coverage`, default filters) summary and aligned bins.
- bigWig: signal mean (`get_signal`) summary and the same aligned bins.
- VCF/BCF: genotypes per sample (`get_variants`), merged into sites by (contig, POS, REF).

Samples are always file-qualified (`f1:NA12878`): equal names in different files are not
assumed to be the same individual. Alignment and signal files are compared per file; a
sample is attached only when the FileRef carries a source-asserted relationship.

A sample without a record at a site is `no_record` (a VCF without a record is not a
homozygous-reference call), `not_read` when its file was truncated before that position,
and a failed file is listed in `unavailable_files`, never as zero depth or a reference call.
Depths are raw counts: no library-size or other normalization is applied. Nothing here
states an association, a cause or a clinical meaning.
"""

from __future__ import annotations

from typing import Any

from genomics_mcp.composition.common import (
    Component,
    FileSlot,
    check_assemblies,
    configured_timeout,
    dispatch,
    fit_metadata,
    jsonable,
    overall_truncation,
    plan_slots,
    run_components,
    source_gate,
    with_max_records,
)
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import ErrorCode, GenomicsError, InvalidInputError
from genomics_mcp.models import EntityKind, FileFormat, Interval, SourceState, SourceStatus
from genomics_mcp.registry import Operation
from genomics_mcp.requests import (
    CompareSamplesRequest,
    CoverageRequest,
    SignalRequest,
    VariantsRequest,
)
from genomics_mcp.result import OperationOutput

COMPARABLE = (FileFormat.BAM, FileFormat.CRAM, FileFormat.VCF, FileFormat.BCF, FileFormat.BIGWIG)
TARGET_BINS = 20
SITES_CAP = 1000
MAX_SAMPLES_PER_FILE = 50
MAX_SAMPLE_RETRIES = 3

NORMALIZATION = {
    "library_size_normalized": False,
    "detail": "depths are raw per-base read counts after the listed filters; signal values "
    "are the files' own values. No library-size, GC or other normalization is applied, so "
    "values from different files are not directly comparable as expression or copy number",
}
INTERPRETATION = (
    "literal observations only; differences between files or samples are not association, "
    "causal or clinical findings"
)


def aligned_bins(length: int, target: int) -> int:
    """Largest bin count <= target that divides the interval, so every file's bins align."""
    for n in range(max(1, min(target, length)), 0, -1):
        if length % n == 0:
            return n
    return 1


def call_class(genotype: dict[str, Any] | None) -> str:
    """Literal description of one call from its allele indexes (never inferred from absence)."""
    if not genotype:
        return "no_gt_field"
    alleles = genotype.get("alleles") or []
    if not alleles or all(a is None for a in alleles):
        return "missing"
    if any(a is None for a in alleles):
        return "partially_missing"
    if len(alleles) == 1:
        return "haploid_reference" if alleles[0] == 0 else "haploid_alternate"
    if all(a == 0 for a in alleles):
        return "homozygous_reference"
    if len(set(alleles)) == 1:
        return "homozygous_alternate"
    return "heterozygous"


def _variants_runner(comp: Component, file: Any, iv: Interval, wanted: list[str] | None):
    async def run(sub: OperationContext) -> OperationOutput:
        sub = with_max_records(sub, comp.record_cap)
        samples = list(wanted) if wanted is not None else None
        absent: list[str] = []
        for _ in range(MAX_SAMPLE_RETRIES):
            request = VariantsRequest(
                file=file, interval=iv, samples=samples, include_genotypes=True
            )
            try:
                out = await dispatch(sub, Operation.GET_VARIANTS, request)
            except GenomicsError as exc:  # typed code, whatever process raised it
                unknown = exc.info.details.get("unknown") if samples else None
                if exc.info.code is not ErrorCode.INVALID_INPUT or not unknown:
                    raise
                absent += [s for s in samples if s in unknown]  # type: ignore[union-attr]
                samples = [s for s in samples if s not in unknown]  # type: ignore[union-attr]
                continue
            if wanted is not None:
                comp.meta["requested_samples_absent"] = absent
            return out
        raise InvalidInputError("requested samples could not be matched to this file's header")

    return run


def _plan(slot: FileSlot, iv: Interval, req: CompareSamplesRequest, nbins: int) -> Component:
    f = slot.file
    if slot.fmt in (FileFormat.BAM, FileFormat.CRAM):
        cram_ref = req.reference if slot.fmt is FileFormat.CRAM and not f.reference_uri else None
        comp = Component(f"{slot.id}.coverage", "coverage", Operation.GET_COVERAGE, slot)
        comp.record_cap = nbins
        comp.run = lambda sub: dispatch(
            with_max_records(sub, nbins), Operation.GET_COVERAGE,
            CoverageRequest(file=f, interval=iv, reference=cram_ref, bin_size=iv.length // nbins),
        )  # fmt: skip
        return comp
    if slot.fmt is FileFormat.BIGWIG:
        comp = Component(f"{slot.id}.signal", "signal", Operation.GET_SIGNAL, slot)
        comp.record_cap = nbins
        comp.run = lambda sub: dispatch(
            with_max_records(sub, nbins), Operation.GET_SIGNAL,
            SignalRequest(file=f, interval=iv, bins=nbins, summary="mean"),
        )  # fmt: skip
        return comp
    comp = Component(f"{slot.id}.variants", "variants", Operation.GET_VARIANTS, slot)
    comp.run = _variants_runner(comp, f, iv, req.samples)
    return comp


def _sample_links(slot: FileSlot) -> list[dict[str, str]]:
    return [
        {"accession": link.accession, "relation": link.relation, "asserted_by": link.source}
        for link in slot.file.relationships
        if link.kind is EntityKind.SAMPLE
    ]


def _collect(
    comp: Component,
    slot: FileSlot,
    bins: dict[tuple[int, int], dict[str, Any]],
    sites: _Sites,
    sample_keys: list[dict[str, str]],
    vcf_state: dict[str, tuple[list[str], int | None, bool]],
    matched: set[str],
) -> None:
    data = comp.output.data  # type: ignore[union-attr]
    recs = list(data.get("records") or [])
    comp.records = recs
    for key in ("assembly", "contig_length", "applied_filters", "header_reference"):
        if key in data:
            comp.meta[key] = jsonable(data[key])
    if comp.kind in ("coverage", "signal"):
        measure = "read_depth" if comp.kind == "coverage" else "signal_mean"
        comp.meta["measure"] = measure
        comp.meta["summary"] = jsonable(data.get("summary"))
        for k in ("complete", "complete_until", "reads_processed", "bin_size", "notes"):
            if k in data:
                comp.meta[k] = jsonable(data[k])
        for r in recs:
            row = bins.setdefault(
                (r["start"], r["end"]),
                {"type": "bin", "start": r["start"], "end": r["end"], "values": {}},
            )
            row["values"][slot.id] = {
                "measure": measure,
                **{k: jsonable(v) for k, v in r.items() if k not in ("start", "end")},
            }
        return
    names = list(data.get("samples") or [])
    matched.update(names)  # every sample present, including ones capped from the display
    kept = names[:MAX_SAMPLES_PER_FILE]
    comp.meta["samples_total"] = len(names)
    if len(names) > len(kept):
        comp.meta["samples_omitted"] = len(names) - len(kept)
        comp.notes.append(
            f"first {MAX_SAMPLES_PER_FILE} samples in header order; pass `samples` to choose others"
        )
    for r in recs:
        sites.add(slot.id, r, kept)
    sample_keys += [{"key": f"{slot.id}:{n}", "file": slot.id, "sample": n} for n in kept]
    incomplete = comp.truncation() is not None or bool(comp.output.errors)  # type: ignore[union-attr]
    vcf_state[slot.id] = (kept, _last_pos(recs), incomplete)


def _last_pos(records: list[dict[str, Any]]) -> int | None:
    return max((int(r["pos"]) for r in records), default=None)


class _Sites:
    """Merge variant records from several files into sites keyed by (contig, POS, REF)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int, str], dict[str, Any]] = {}

    def add(self, fid: str, rec: dict[str, Any], samples: list[str]) -> None:
        key = (rec["contig"], int(rec["pos"]), rec["ref"])
        row = self.rows.setdefault(
            key,
            {"type": "variant_site", "contig": key[0], "pos": key[1], "ref": key[2],
             "file_records": {}, "calls": []},
        )  # fmt: skip
        recs = row["file_records"].setdefault(fid, [])
        recs.append(
            {k: jsonable(rec.get(k)) for k in ("start", "end", "ids", "alts", "qual", "filters",
                                                "filter_status")}
        )  # fmt: skip
        per = rec.get("samples") or {}
        for name in samples:
            entry = per.get(name)
            if entry is None:
                continue
            gt = entry.get("genotype")
            cls = call_class(gt)
            row["calls"].append(
                {"sample_key": f"{fid}:{name}", "file": fid, "sample": name,
                 "record": len(recs) - 1,
                 "observation": "missing_call" if cls in ("missing", "no_gt_field") else "called",
                 "call_class": cls, "genotype": jsonable(gt), "format": jsonable(entry.get("format"))}
            )  # fmt: skip


async def compare_samples(req: CompareSamplesRequest, ctx: OperationContext) -> OperationOutput:
    iv = req.interval
    ctx.check_region(iv)
    check_assemblies(iv, req.files, req.reference)
    slots, warnings = plan_slots(ctx, req.files)
    bad = [
        {"file": s.id, "format": s.fmt.value if s.fmt else None}
        for s in slots
        if s.fmt not in COMPARABLE
    ]
    if bad:
        raise InvalidInputError(
            "compare_samples accepts bam, cram, vcf, bcf and bigwig files (set file.format if "
            "it cannot be inferred)",
            details={"unsupported": bad},
        )
    total = ctx.limits.max_records
    nbins = aligned_bins(iv.length, max(1, min(TARGET_BINS, total // (2 * len(slots)))))
    variant_files = sum(1 for s in slots if s.fmt in (FileFormat.VCF, FileFormat.BCF))
    site_cap = max(1, min(SITES_CAP, total // max(1, variant_files)))

    components: list[Component] = []
    for slot in slots:
        comp = _plan(slot, iv, req, nbins)
        gate = source_gate(ctx, slot)
        if gate is not None:
            # keep the planned role so the file is listed as unavailable for it; never read
            comp.run = None
            comp.skip(gate, SourceState.DISABLED)
            components.append(comp)
            continue
        if comp.kind == "variants":
            comp.record_cap = site_cap
        comp.timeout_s = configured_timeout(ctx, slot)
        components.append(comp)
    await run_components(ctx, components)

    bins: dict[tuple[int, int], dict[str, Any]] = {}
    sites = _Sites()
    sample_keys: list[dict[str, str]] = []
    matched: set[str] = set()
    files_out: list[dict[str, Any]] = []
    vcf_state: dict[str, tuple[list[str], int | None, bool]] = {}
    for comp in components:
        slot = comp.slot
        assert slot is not None
        if comp.ok:
            _collect(comp, slot, bins, sites, sample_keys, vcf_state, matched)
        entry = comp.entry()
        entry["component"] = entry.pop("id")
        entry.pop("file", None)
        head = slot.describe()
        if comp.kind != "variants":
            links = _sample_links(slot)
            head["sample_links"] = links
            if not links:
                head["sample_note"] = "compared per file; no sample relationship is asserted"
        files_out.append({**head, **entry})

    failed_signal = [
        c.slot.id for c in components if c.kind in ("coverage", "signal") and not c.ok and c.slot
    ]
    failed_vcf = [c.slot.id for c in components if c.kind == "variants" and not c.ok and c.slot]
    bin_rows = [bins[k] for k in sorted(bins)]
    for row in bin_rows:
        row["unavailable_files"] = failed_signal
    site_rows = [sites.rows[k] for k in sorted(sites.rows, key=lambda k: (k[1], k[2], k[0]))]
    for row in site_rows:
        seen = {c["sample_key"] for c in row["calls"]}
        for fid, (names, last, incomplete) in vcf_state.items():
            has_record = fid in row["file_records"]
            for name in names:
                key = f"{fid}:{name}"
                if key in seen:
                    continue
                if has_record:
                    obs = "missing_call"  # record present, sample column absent
                elif incomplete and (last is None or row["pos"] >= last):
                    obs = "not_read"
                else:
                    obs = "no_record"
                row["calls"].append({"sample_key": key, "file": fid, "sample": name,
                                     "observation": obs})  # fmt: skip
        row["unavailable_files"] = failed_vcf
        counts: dict[str, int] = {}
        for c in row["calls"]:
            label = c.get("call_class") if c["observation"] == "called" else c["observation"]
            counts[label] = counts.get(label, 0) + 1
        distinct = {
            tuple(sorted(str(b) for b in c["genotype"]["allele_bases"]))
            for c in row["calls"]
            if c["observation"] == "called" and c["call_class"] != "partially_missing"
        }
        row["summary"] = {"counts": counts, "distinct_called_allele_sets": len(distinct)}

    records = bin_rows + site_rows
    dropped = max(0, len(records) - total)
    records = records[:total]
    errors = [e for c in components for e in c.errors()]
    statuses = [
        SourceStatus(source=c.id, state=c.state, message=c.error.message if c.error else None)
        for c in components
    ]
    provenance = [p for c in components if c.output is not None for p in c.output.provenance]
    warnings += [
        f"{c.id}: {w}" for c in components if c.output is not None for w in c.output.warnings
    ]
    if not any(c.ok for c in components):
        return OperationOutput(
            data=None, errors=errors, source_status=statuses, provenance=provenance,
            warnings=warnings,
        )  # fmt: skip

    duplicates: dict[str, list[str]] = {}
    for s in sample_keys:
        duplicates.setdefault(s["sample"], []).append(s["key"])
    duplicates = {k: v for k, v in duplicates.items() if len(v) > 1}
    absent_everywhere = None
    if req.samples is not None:
        absent_everywhere = [s for s in req.samples if s not in matched]

    truncation = overall_truncation(components, len(records), total, dropped)
    data = {
        "interval": iv,
        "assembly": {"requested": iv.assembly, "policy": "exact match; no liftover"},
        "files": files_out,
        "samples": sample_keys,
        "duplicate_sample_names": duplicates,
        "samples_requested_not_found": absent_everywhere,
        "sample_filter": "`samples` selects VCF/BCF sample columns; alignment and signal files "
        "are compared per file",
        "bins": {"count": nbins, "size": iv.length // nbins, "aligned": True},
        "normalization": NORMALIZATION,
        "interpretation": INTERPRETATION,
        "observation_codes": {
            "called": "a genotype call; call_class is from its allele indexes",
            "missing_call": "a record exists but the genotype is missing ('.')",
            "no_record": "the file has no record here; this is not a reference call",
            "not_read": "the file was truncated or stopped before this position",
            "unavailable_files": "files that failed; they have no values, not zero",
        },
        "records": records,
    }
    fit_metadata(data, ctx.limits.max_response_bytes, [("files",)])
    return OperationOutput(
        data=data,
        errors=errors,
        source_status=statuses,
        provenance=provenance,
        warnings=warnings,
        truncation=truncation,
    )
