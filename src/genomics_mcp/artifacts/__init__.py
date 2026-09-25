"""E3 artifacts provider: transfers, FASTA sequence and indexed BED/GFF3/GTF features.

Registers fetch_file / get_transfer_status / cancel_transfer (`default`), get_sequence for
`fasta`, get_features for `bed`, `gff3`, `gtf`, and the `transfers` component.
"""

from __future__ import annotations

from typing import Any

import pysam

from genomics_mcp.artifacts.fetch import (
    COMPONENT,
    LazyTransfers,
    cancel_transfer,
    fetch_file,
    get_transfer_status,
)
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import ErrorCode, ErrorInfo, InvalidInputError
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.requests import FeaturesRequest, SequenceRequest
from genomics_mcp.result import OperationOutput, Truncation

TASKS = "genomics_mcp.artifacts.native_tasks"
METHOD = f"pysam {pysam.__version__} / HTSlib {pysam.version.__htslib_version__}"


def _with_call(fn):
    async def handler(req: Any, ctx: OperationContext) -> OperationOutput:
        async with ctx.require_component("storage").get(ctx).native_call(ctx) as call:
            return await fn(req, ctx, call)

    handler.__name__ = fn.__name__
    return handler


def _assembly(req: Any, kind: str) -> dict[str, Any]:
    return {
        "requested": req.interval.assembly,
        "file_declared": None,
        "status": "file_metadata_asserted" if req.file.assembly else "caller_asserted",
        "note": f"{kind} files do not record an assembly; it is not verified",
    }


@_with_call
async def get_sequence(req: SequenceRequest, ctx: OperationContext, call: Any) -> OperationOutput:
    storage = ctx.require_component("storage").get(ctx)
    resolved = await ctx.resolve_file(req.file, interval=req.interval)
    storage.require_ready(resolved, needs_index=True)
    params = await call.reader_params(resolved)
    params.update(interval=req.interval.model_dump())
    async with storage.staged_indexes(ctx, resolved, params) as staged:
        result = await call.run(f"{TASKS}:sequence", staged, what="get_sequence")
    iv = req.interval
    record = {
        "contig": iv.contig,
        "start": iv.start,
        "end": iv.end,
        "assembly": iv.assembly,
        "length": len(result["sequence"]),
        "sequence": result["sequence"],
    }
    return OperationOutput(
        data={
            "records": [record],
            "file": resolved.file.display_uri(),
            "assembly": _assembly(req, "FASTA"),
            "contig_length": result["contig_length"],
            "notes": ["sequence case is preserved as in the file (lowercase may be soft-masked)"],
        },
        provenance=[
            storage.provenance(
                resolved, method=f"{METHOD} faidx fetch", transformations=["0-based half-open"]
            )
        ],
    )


@_with_call
async def get_features(req: FeaturesRequest, ctx: OperationContext, call: Any) -> OperationOutput:
    fmt = req.file.effective_format()
    if fmt is not None and fmt.value == "bed" and req.feature_types:
        raise InvalidInputError("feature_types is not defined for BED (no type column); omit it")
    storage = ctx.require_component("storage").get(ctx)
    resolved = await ctx.resolve_file(req.file, interval=req.interval)
    storage.require_ready(resolved, needs_index=True)
    params = await call.reader_params(resolved)
    params.update(
        interval=req.interval.model_dump(),
        format=fmt.value,
        feature_types=req.feature_types,
        max_records=ctx.limits.max_records,
    )
    result = await call.run(f"{TASKS}:features", params, what="get_features")
    records = result["records"]
    truncation = (
        Truncation(reason="max_records", limit=ctx.limits.max_records, returned=len(records))
        if result["truncated"]
        else None
    )
    errors = []
    if not result["complete"]:
        errors.append(
            ErrorInfo(code=ErrorCode.TIMEOUT, message="stopped at the deadline", retryable=True)
        )
    conversion = (
        "BED start/end are already 0-based half-open"
        if fmt.value == "bed"
        else f"{fmt.value.upper()} 1-based closed start converted to 0-based (start - 1); end kept"
    )
    return OperationOutput(
        data={
            "records": records,
            "format": fmt.value,
            "interval": req.interval,
            "file": resolved.file.display_uri(),
            "assembly": _assembly(req, fmt.value.upper()),
            "coordinates": conversion,
            "applied_filters": {"feature_types": req.feature_types},
        },
        truncation=truncation,
        errors=errors,
        provenance=[
            storage.provenance(
                resolved, method=f"{METHOD} tabix fetch", transformations=[conversion]
            )
        ],
    )


def register(registry: Registry) -> None:
    lazy = LazyTransfers()
    registry.provide(COMPONENT, lazy)
    registry.register(Operation.FETCH_FILE, "default", fetch_file, provider=__name__)
    registry.register(
        Operation.GET_TRANSFER_STATUS, "default", get_transfer_status, provider=__name__
    )
    registry.register(Operation.CANCEL_TRANSFER, "default", cancel_transfer, provider=__name__)
    registry.register(Operation.GET_SEQUENCE, "fasta", get_sequence, provider=__name__)
    for fmt in ("bed", "gff3", "gtf"):
        registry.register(Operation.GET_FEATURES, fmt, get_features, provider=__name__)

    async def close() -> None:
        if lazy.manager is not None:
            await lazy.manager.shutdown()

    registry.on_shutdown(close)
