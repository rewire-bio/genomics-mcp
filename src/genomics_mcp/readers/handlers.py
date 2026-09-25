"""E4 handlers: get_reads, get_coverage, get_pileup (BAM/CRAM) and get_variants (VCF/BCF)."""

from __future__ import annotations

from typing import Any

import pysam

from genomics_mcp.context import OperationContext
from genomics_mcp.errors import (
    ErrorCode,
    ErrorInfo,
    GenomicsError,
    InvalidInputError,
    PreparationRequiredError,
)
from genomics_mcp.models import FileFormat, FileRef
from genomics_mcp.requests import CoverageRequest, PileupRequest, ReadsRequest, VariantsRequest
from genomics_mcp.result import OperationOutput, Truncation

ALIGN = "genomics_mcp.readers.native_align"
VARIANTS = "genomics_mcp.readers.native_variants"
METHOD = f"pysam {pysam.__version__} / HTSlib {pysam.version.__htslib_version__}"
COORDS = "0-based half-open coordinates"
COVERAGE_INPUT_READ_CAP = 5_000_000
"""Reads processed per coverage call. Separate from max_records (an output cap); hitting it
makes the result partial and marks later positions as not computed."""


def _storage(ctx: OperationContext) -> Any:
    return ctx.require_component("storage").get(ctx)


def _interval(req: Any) -> dict[str, Any]:
    return req.interval.model_dump()


def _with_call(fn):
    """Give the handler its own native-call lease (proxy routes released when it returns)."""

    async def handler(req: Any, ctx: OperationContext) -> OperationOutput:
        async with _storage(ctx).native_call(ctx) as call:
            return await fn(req, ctx, call)

    handler.__name__ = fn.__name__
    handler.__doc__ = fn.__doc__
    return handler


async def _reference_params(
    ctx: OperationContext, call: Any, file: FileRef, reference: FileRef | None, fmt: FileFormat
) -> dict[str, Any] | None:
    storage = call.manager
    ref = reference
    if ref is None and file.reference_uri:
        ref = FileRef(uri=file.reference_uri, format=FileFormat.FASTA, visibility=file.visibility)
    if ref is None:
        return None
    resolved = await ctx.resolve_file(ref)
    storage.require_ready(resolved, needs_index=True)
    if resolved.local_path is None:
        raise PreparationRequiredError(
            "the reference FASTA must be local so its MD5 can be checked and HTSlib can read it "
            "without network lookups",
            hint="fetch it with fetch_file (with an explicit budget if large) and pass the "
            "local artifact path",
            details={"reference": ref.display_uri()},
        )
    params = await call.reader_params(resolved)
    params["path"] = str(resolved.local_path)
    params["display"] = ref.display_uri()
    return params


async def _alignment_params(
    req: Any, ctx: OperationContext, call: Any
) -> tuple[Any, Any, dict[str, Any]]:
    storage = call.manager
    resolved = await ctx.resolve_file(req.file, interval=req.interval)
    fmt = _served_format(req, resolved)
    storage.require_ready(resolved, needs_index=True)
    params = await call.reader_params(resolved)
    params.update(
        format=fmt.value,
        interval=_interval(req),
        file_assembly=req.file.assembly,
        reference=await _reference_params(ctx, call, req.file, req.reference, fmt),
        max_records=ctx.limits.max_records,
        md5_cache_dir=str(ctx.settings.paths.work_dir / "cache" / "ref-md5"),
        seal_dir=str(ctx.settings.paths.work_dir / ".isolation" / "ref-seal"),
    )
    return storage, resolved, params


async def _run(call: Any, task: str, params: dict[str, Any], what: str):
    try:
        return await call.run(task, params, what=what)
    except GenomicsError as exc:
        stderr = str(exc.info.details.get("native_stderr", ""))
        if params.get("format") == "cram" and (
            "fetch reference" in stderr or "populate reference" in stderr
        ):
            raise PreparationRequiredError(
                "the CRAM needs its reference to decode and none was supplied or embedded",
                hint="pass reference= a local FASTA whose MD5 matches the header @SQ M5",
                details={"cram_reference": "missing"},
            ) from None
        raise


def _incomplete(result: dict[str, Any], what: str) -> list[ErrorInfo]:
    if result.get("complete", True):
        return []
    return [
        ErrorInfo(
            code=ErrorCode.TIMEOUT,
            message=f"{what} stopped at the deadline before reading the whole interval; "
            "records returned are only those read so far",
            retryable=True,
            hint="query a smaller interval",
        )
    ]


def _served_format(req: Any, resolved: Any) -> FileFormat:
    """Format of the bytes actually served. A region resolver may serve another format than
    the source (EGA htsget returns BAM for CRAM sources); readers follow the served format."""
    served = resolved.file.effective_format() or req.file.effective_format()
    requested = req.file.effective_format()
    families = ({FileFormat.BAM, FileFormat.CRAM}, {FileFormat.VCF, FileFormat.BCF})
    if (
        requested
        and served != requested
        and not any(requested in f and served in f for f in families)
    ):
        raise InvalidInputError(
            f"the resolver served {served.value} for a {requested.value} request",
            details={"requested": requested.value, "served": served.value},
        )
    return served


def _region_info(resolved: Any, req: Any) -> dict[str, Any]:
    """What a region resolver reported about the bounded slice it served."""
    if resolved.region is None:
        return {}
    info: dict[str, Any] = {
        "served_format": resolved.file.effective_format().value,
        "requested_format": req.file.effective_format().value,
        "slice_region": resolved.region,
        "post_filtered": "records are filtered to the interval after reading the slice",
    }
    meta = (resolved.file.native or {}).get("region_artifact")
    if isinstance(meta, dict):
        info["provider"] = {
            k: meta.get(k)
            for k in (
                "provider",
                "bytes",
                "sha256",
                "records_received",
                "records_overlapping",
                "checksum_scope",
            )
            if k in meta
        }
    return {"region_slice": info}


def _common(result: dict[str, Any], resolved: Any) -> dict[str, Any]:
    out = {
        "file": resolved.file.display_uri(),
        "assembly": result.get("assembly"),
        "contig_length": result.get("contig_length"),
    }
    if "reference" in result:
        out["reference"] = result["reference"]
    return out


# --------------------------------------------------------------------------- reads


@_with_call
async def get_reads(req: ReadsRequest, ctx: OperationContext, call: Any) -> OperationOutput:
    storage, resolved, params = await _alignment_params(req, ctx, call)
    params.update(
        require_flags=req.require_flags,
        exclude_flags=req.exclude_flags,
        min_mapping_quality=req.min_mapping_quality,
        include_sequence=req.include_sequence,
    )
    result = await _run(call, f"{ALIGN}:reads", params, "get_reads")
    records = result["records"]
    truncation = (
        Truncation(reason="max_records", limit=ctx.limits.max_records, returned=len(records))
        if result["truncated"]
        else None
    )
    data = {
        "records": records,
        "interval": req.interval,
        **_common(result, resolved),
        **_region_info(resolved, req),
        "applied_filters": {
            "require_flags": req.require_flags,
            "exclude_flags": req.exclude_flags,
            "min_mapping_quality": req.min_mapping_quality,
            "overlap": "records overlapping the interval (samtools view region semantics)",
            "include_sequence": req.include_sequence,
            "samtools_equivalent": f"samtools view -f {req.require_flags} -F "
            f"{req.exclude_flags} -q {req.min_mapping_quality} FILE {req.interval.to_region()}",
        },
    }
    return OperationOutput(
        data=data,
        truncation=truncation,
        errors=_incomplete(result, "get_reads"),
        provenance=[
            storage.provenance(resolved, method=f"{METHOD} fetch", transformations=[COORDS])
        ],
    )


# --------------------------------------------------------------------------- coverage


def _depth_command(req: CoverageRequest) -> str:
    base = 0x704
    g = base & ~req.exclude_flags
    parts = ["samtools depth -a"]
    if g:
        parts.append(f"-g {g:#x}")
    if req.exclude_flags:
        parts.append(f"-G {req.exclude_flags:#x}")
    parts.append(f"-Q {req.min_mapping_quality} -q {req.min_base_quality}")
    parts.append(f"-r {req.interval.to_region()} FILE")
    return " ".join(parts)


@_with_call
async def get_coverage(req: CoverageRequest, ctx: OperationContext, call: Any) -> OperationOutput:
    storage, resolved, params = await _alignment_params(req, ctx, call)
    if req.bin_size is not None and req.bin_size > req.interval.length:
        raise InvalidInputError("bin_size is larger than the interval")
    params.update(
        exclude_flags=req.exclude_flags,
        min_mapping_quality=req.min_mapping_quality,
        min_base_quality=req.min_base_quality,
        bin_size=req.bin_size,
        max_input_reads=COVERAGE_INPUT_READ_CAP,
    )
    result = await _run(call, f"{ALIGN}:coverage", params, "get_coverage")
    records = result["records"]
    truncation = None
    if len(records) < result["available"]:
        truncation = Truncation(
            reason="max_records",
            limit=ctx.limits.max_records,
            returned=len(records),
            available=result["available"],
        )
    errors: list[ErrorInfo] = []
    if not result["complete"]:
        errors.append(
            ErrorInfo(
                code=ErrorCode.TIMEOUT
                if result["stop_reason"] == "deadline"
                else ErrorCode.BUDGET_EXCEEDED,
                message="coverage input processing stopped early "
                f"({result['stop_reason']}); depth is computed only for positions before "
                f"{result['complete_until']}. Later positions are not computed (not zero).",
                hint="query a smaller interval",
                details={"complete_until": result["complete_until"]},
            )
        )
    data = {
        "records": records,
        "mode": "bins" if req.bin_size else "per_base",
        "bin_size": req.bin_size,
        "interval": req.interval,
        "summary": result["summary"],
        "complete": result["complete"],
        "complete_until": result["complete_until"],
        "reads_processed": result["reads_processed"],
        **_common(result, resolved),
        **_region_info(resolved, req),
        "applied_filters": {
            "exclude_flags": req.exclude_flags,
            "min_mapping_quality": req.min_mapping_quality,
            "min_base_quality": req.min_base_quality,
            "counts": "bases aligned by M/=/X; deletions and reference skips are not counted",
            "overlapping_mates": "both counted (samtools depth default, no -s)",
            "samtools_equivalent": _depth_command(req),
        },
    }
    return OperationOutput(
        data=data,
        truncation=truncation,
        errors=errors,
        provenance=[
            storage.provenance(
                resolved, method=f"{METHOD} fetch + CIGAR depth", transformations=[COORDS]
            )
        ],
    )


# --------------------------------------------------------------------------- pileup


@_with_call
async def get_pileup(req: PileupRequest, ctx: OperationContext, call: Any) -> OperationOutput:
    storage, resolved, params = await _alignment_params(req, ctx, call)
    if resolved.index_open_uri is None and resolved.region is None:
        raise PreparationRequiredError("pileup needs an indexed file")
    params.update(
        exclude_flags=req.exclude_flags,
        min_mapping_quality=req.min_mapping_quality,
        min_base_quality=req.min_base_quality,
        max_depth=req.max_depth,
        overlap_detection=True,
    )
    result = await _run(call, f"{ALIGN}:pileup", params, "get_pileup")
    records = result["records"]
    truncation = None
    warnings: list[str] = []
    if result["truncated"]:
        truncation = Truncation(
            reason="max_records", limit=ctx.limits.max_records, returned=len(records)
        )
    if result["positions_at_depth_limit"]:
        warnings.append(
            f"{result['positions_at_depth_limit']} positions reached max_depth={req.max_depth}; "
            "reads beyond it were not piled up there (see depth_limit_reached)"
        )
        if truncation is None:
            truncation = Truncation(reason="max_depth", limit=req.max_depth, returned=len(records))
    data = {
        "records": records,
        "interval": req.interval,
        "positions": "only positions with at least one read after read filters",
        **_common(result, resolved),
        **_region_info(resolved, req),
        "applied_filters": {
            "exclude_flags": req.exclude_flags,
            "min_mapping_quality": req.min_mapping_quality,
            "min_base_quality": req.min_base_quality,
            "max_depth": req.max_depth,
            "baq": False,
            "overlap_detection": True,
            "count_orphans": True,
            "notes": [
                "an entry (base, deletion or skip) is kept when quality[qpos] >= "
                "min_base_quality; for deletions and skips qpos is the next query base, as in "
                "samtools",
                "overlapping mate bases: the pair's lower-weight copy gets quality 0 and is "
                "removed by min_base_quality > 0 (samtools overlap detection)",
                "depth counts kept entries including deletions and reference skips, like the "
                "samtools mpileup depth column",
                "observations only; no variant calling",
            ],
            "samtools_equivalent": f"samtools mpileup -B -A -Q {req.min_base_quality} -q "
            f"{req.min_mapping_quality} --ff {req.exclude_flags:#x} -d {req.max_depth} -r "
            f"{req.interval.to_region()} FILE",
        },
    }
    return OperationOutput(
        data=data,
        truncation=truncation,
        warnings=warnings,
        errors=_incomplete(result, "get_pileup"),
        provenance=[
            storage.provenance(resolved, method=f"{METHOD} pileup", transformations=[COORDS])
        ],
    )


# --------------------------------------------------------------------------- variants


@_with_call
async def get_variants(req: VariantsRequest, ctx: OperationContext, call: Any) -> OperationOutput:
    storage = call.manager
    resolved = await ctx.resolve_file(req.file, interval=req.interval)
    fmt = _served_format(req, resolved)
    storage.require_ready(resolved, needs_index=True)
    params = await call.reader_params(resolved)
    params.update(
        format=fmt.value,
        interval=_interval(req),
        file_assembly=req.file.assembly,
        samples=req.samples,
        include_genotypes=req.include_genotypes,
        pass_only=req.pass_only,
        max_records=ctx.limits.max_records,
    )
    result = await _run(call, f"{VARIANTS}:variants", params, "get_variants")
    records = result["records"]
    truncation = (
        Truncation(reason="max_records", limit=ctx.limits.max_records, returned=len(records))
        if result["truncated"]
        else None
    )
    data = {
        "records": records,
        "interval": req.interval,
        "samples": result["samples"],
        "file": resolved.file.display_uri(),
        **_region_info(resolved, req),
        "assembly": result["assembly"],
        "header_reference": result["header_reference"],
        "contig_length": result["contig_length"],
        "coordinates": "pos is VCF POS (1-based); start/end are 0-based half-open over REF "
        "(END-aware)",
        "applied_filters": {
            "pass_only": req.pass_only,
            "samples": req.samples,
            "include_genotypes": req.include_genotypes,
            "overlap": "records whose REF span overlaps the interval (bcftools -r semantics)",
        },
    }
    return OperationOutput(
        data=data,
        truncation=truncation,
        errors=_incomplete(result, "get_variants"),
        provenance=[
            storage.provenance(
                resolved, method=f"{METHOD} VariantFile fetch", transformations=[COORDS]
            )
        ],
    )
