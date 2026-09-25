"""E5 signal provider: get_signal for bigWig, get_features for bigBed.

pyBigWig runs in isolated child processes. Remote files must first pass the storage range
preflight; the redirect-resolved final URL is what pyBigWig opens, so nothing is ever
downloaded whole. bigWig/bigBed files carry no assembly: it is reported as caller-asserted.
"""

from __future__ import annotations

from typing import Any

import pyBigWig

from genomics_mcp.context import OperationContext
from genomics_mcp.errors import ErrorCode, ErrorInfo, InvalidInputError, UnsupportedError
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.requests import FeaturesRequest, SignalRequest
from genomics_mcp.result import OperationOutput, Truncation

TASKS = "genomics_mcp.signal.native_signal"
METHOD = f"pyBigWig {pyBigWig.__version__}" if hasattr(pyBigWig, "__version__") else "pyBigWig"
REMOTE_CAPABLE = bool(pyBigWig.remote)


def _assembly(req: Any) -> dict[str, Any]:
    return {
        "requested": req.interval.assembly,
        "file_declared": None,
        "status": "file_metadata_asserted" if req.file.assembly else "caller_asserted",
        "note": "bigWig/bigBed headers do not record an assembly",
    }


def _with_call(fn):
    async def handler(req: Any, ctx: OperationContext) -> OperationOutput:
        async with ctx.require_component("storage").get(ctx).native_call(ctx) as call:
            return await fn(req, ctx, call)

    handler.__name__ = fn.__name__
    return handler


async def _prepare(req: Any, ctx: OperationContext, call: Any) -> tuple[Any, Any, dict[str, Any]]:
    storage = call.manager
    resolved = await ctx.resolve_file(req.file, interval=req.interval)
    storage.require_ready(resolved, needs_index=False)
    if resolved.local_path is None and not REMOTE_CAPABLE:
        raise UnsupportedError(
            "this installation's pyBigWig was built without libcurl, so remote bigWig/bigBed "
            "files cannot be read",
            hint="reinstall with pyBigWig built from source against libcurl (see "
            "docs/data-access.md, 'Installation'), or fetch the file with fetch_file",
        )
    params = await call.reader_params(resolved)
    params.update(interval=req.interval.model_dump(), max_records=ctx.limits.max_records)
    return storage, resolved, params


def _late(result: dict[str, Any], what: str) -> list[ErrorInfo]:
    if result.get("complete", True):
        return []
    return [
        ErrorInfo(
            code=ErrorCode.TIMEOUT,
            message=f"{what} finished after the soft deadline",
            retryable=True,
        )
    ]


@_with_call
async def get_signal_bigwig(
    req: SignalRequest, ctx: OperationContext, call: Any
) -> OperationOutput:
    if req.bins is not None:
        if req.bins > req.interval.length:
            raise InvalidInputError("bins cannot exceed the interval length")
        if req.bins > ctx.limits.max_records:
            raise InvalidInputError(
                f"bins={req.bins} exceeds max_records={ctx.limits.max_records}",
                hint="request fewer bins",
            )
    storage, resolved, params = await _prepare(req, ctx, call)
    params.update(bins=req.bins, summary=req.summary)
    result = await call.run(f"{TASKS}:bigwig", params, what="get_signal")
    records = result["records"]
    truncation = None
    if len(records) < result["available"]:
        truncation = Truncation(
            reason="max_records",
            limit=ctx.limits.max_records,
            returned=len(records),
            available=result["available"],
        )
    data = {
        "records": records,
        "mode": result["mode"],
        "summary": result["summary"],
        "interval": req.interval,
        "file": resolved.file.display_uri(),
        "assembly": _assembly(req),
        "contig_length": result["contig_length"],
        "file_header": result["file_header"],
        "notes": [
            "summary and bins use exact=True (full-resolution data, not zoom levels)",
            "null means no data in that span, not zero",
            "records are the file's data intervals clipped to the requested interval"
            if result["mode"] == "intervals"
            else "bin i covers [start + i*L//n, start + (i+1)*L//n)",
        ],
    }
    return OperationOutput(
        data=data,
        truncation=truncation,
        errors=_late(result, "get_signal"),
        provenance=[
            storage.provenance(
                resolved,
                method=f"{METHOD} stats(exact=True)"
                + (" via HTTP byte ranges" if resolved.local_path is None else ""),
                transformations=["0-based half-open coordinates"],
            )
        ],
    )


@_with_call
async def get_features_bigbed(
    req: FeaturesRequest, ctx: OperationContext, call: Any
) -> OperationOutput:
    if req.feature_types:
        raise InvalidInputError(
            "feature_types is not defined for bigBed (no feature type column); omit it",
        )
    storage, resolved, params = await _prepare(req, ctx, call)
    result = await call.run(f"{TASKS}:bigbed", params, what="get_features")
    records = result["records"]
    truncation = None
    if len(records) < result["available"]:
        truncation = Truncation(
            reason="max_records",
            limit=ctx.limits.max_records,
            returned=len(records),
            available=result["available"],
        )
    data = {
        "records": records,
        "format": "bigbed",
        "schema": result["schema"],
        "interval": req.interval,
        "file": resolved.file.display_uri(),
        "assembly": _assembly(req),
        "contig_length": result["contig_length"],
        "coordinates": "start/end are the file's 0-based half-open BED coordinates",
    }
    return OperationOutput(
        data=data,
        truncation=truncation,
        errors=_late(result, "get_features"),
        provenance=[
            storage.provenance(
                resolved, method=f"{METHOD} entries", transformations=["columns named by autoSql"]
            )
        ],
    )


def register(registry: Registry) -> None:
    registry.register(
        Operation.GET_SIGNAL,
        "bigwig",
        get_signal_bigwig,
        provider=__name__,
        description="remote bigWig " + ("supported" if REMOTE_CAPABLE else "not supported"),
    )
    registry.register(Operation.GET_FEATURES, "bigbed", get_features_bigbed, provider=__name__)
