"""Regressions: whole-call deadline keeps completed fan-out results; every envelope fits the
response byte cap, with omitted metadata marked."""

from __future__ import annotations

import asyncio
import time

from genomics_mcp.errors import ErrorCode, ErrorInfo
from genomics_mcp.models import Provenance, SourceState, SourceStatus
from genomics_mcp.registry import DEFAULT_KEY, Operation, Registry
from genomics_mcp.result import OperationOutput, ResultStatus, ToolResult, fit_to_response_budget
from genomics_mcp.service import GenomicsService


def _service(settings, register) -> GenomicsService:
    reg = Registry()
    register(reg)
    return GenomicsService(settings, reg, load_providers=False)


async def test_stalled_source_at_overall_deadline_keeps_fast_result(settings):
    settings.limits.interactive_timeout_s = 0.05

    async def fast(req, ctx):
        return OperationOutput(data={"symbol": "BRCA2"})

    async def stalled(req, ctx):
        await asyncio.sleep(3600)

    def register(r):
        r.register(Operation.LOOKUP_GENE, "hgnc", fast, provider="t")
        r.register(Operation.LOOKUP_GENE, "gnomad", stalled, provider="t")

    svc = _service(settings, register)
    started = time.monotonic()
    res = await svc.call(Operation.LOOKUP_GENE, {"gene": "BRCA2"})
    assert time.monotonic() - started < 1.0  # still a hard, finite bound
    assert res.status is ResultStatus.PARTIAL
    assert res.data == {"by_source": {"hgnc": {"symbol": "BRCA2"}}}
    states = {s.source: s.state for s in res.source_status}
    assert states == {"hgnc": SourceState.OK, "gnomad": SourceState.TIMEOUT}


async def test_handler_that_ignores_the_deadline_is_still_bounded(settings):
    settings.limits.interactive_timeout_s = 0.05

    async def planner(req, ctx):
        await asyncio.sleep(3600)

    svc = _service(
        settings, lambda r: r.register(Operation.LOOKUP_GENE, DEFAULT_KEY, planner, provider="t")
    )
    started = time.monotonic()
    res = await svc.call(Operation.LOOKUP_GENE, {"gene": "BRCA2"})
    assert time.monotonic() - started < 1.5
    assert res.status is ResultStatus.ERROR and res.error.code == ErrorCode.TIMEOUT


async def test_external_cancellation_cancels_fan_out_workers(settings):
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def stalled(req, ctx):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fast(req, ctx):
        return OperationOutput(data={"ok": True})

    def register(r):
        r.register(Operation.LOOKUP_GENE, "hgnc", fast, provider="t")
        r.register(Operation.LOOKUP_GENE, "gnomad", stalled, provider="t")

    svc = _service(settings, register)
    task = asyncio.create_task(svc.call(Operation.LOOKUP_GENE, {"gene": "BRCA2"}))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(cancelled.wait(), 1)  # the worker did not outlive the call


def _big_provenance(n: int = 20) -> list[Provenance]:
    return [
        Provenance(source="synthetic", source_record_id=str(i), transformations=["x" * 600])
        for i in range(n)
    ]


def test_budget_error_fallback_fits_with_large_provenance():
    # Reproduction from the independent review: the budget_exceeded envelope itself was over the cap.
    result = ToolResult(
        operation="lookup_variant",
        status=ResultStatus.OK,
        data={"value": "x" * 10_000},
        provenance=_big_provenance(),
    )
    out = fit_to_response_budget(result, 4096)
    assert out.json_size() <= 4096
    assert out.status is ResultStatus.ERROR and out.error.code == ErrorCode.BUDGET_EXCEEDED
    assert out.metadata_omitted["provenance"] > 0
    assert len(out.provenance) + out.metadata_omitted["provenance"] == 20


def test_large_source_status_and_error_text_are_trimmed_and_marked():
    statuses = [
        SourceStatus(source=f"s{i}", state=SourceState.TIMEOUT, message="m" * 400)
        for i in range(40)
    ]
    err = ErrorInfo(code=ErrorCode.UPSTREAM_ERROR, message="e" * 9000, details={"blob": "d" * 3000})
    result = ToolResult(
        operation="lookup_gene", status=ResultStatus.ERROR, error=err, source_status=statuses
    )
    out = fit_to_response_budget(result, 4096)
    assert out.json_size() <= 4096
    # The original error is kept (shortened), not replaced by a generic one.
    assert out.error.code == ErrorCode.UPSTREAM_ERROR and out.error.message.startswith("eee")
    assert out.metadata_omitted["source_status"] == 40 - len(out.source_status)
    assert out.metadata_omitted.get("error_text") == 1


def test_record_trimming_with_large_metadata_keeps_some_records_and_marks_both():
    records = [{"i": i, "v": "r" * 200} for i in range(50)]
    result = ToolResult(
        operation="lookup_variant",
        status=ResultStatus.OK,
        data={"records": records},
        provenance=_big_provenance(30),
        warnings=["w" * 300] * 10,
    )
    out = fit_to_response_budget(result, 6000)
    assert out.json_size() <= 6000
    assert out.status is ResultStatus.OK
    n = len(out.data["records"])
    assert 0 < n < 50
    assert out.truncation.reason == "max_response_bytes" and out.truncation.available == 50
    assert out.metadata_omitted and out.metadata_omitted["provenance"] > 0
    assert out.data["records"] == records[:n]


def test_complete_small_result_is_unchanged_and_unmarked():
    result = ToolResult(operation="x", status=ResultStatus.OK, data={"records": [1, 2]})
    assert fit_to_response_budget(result, 4096) is result
    assert result.metadata_omitted is None


def test_multibyte_error_text_is_shortened_by_bytes_and_keeps_code():
    # Coordinator reproduction: 4,287 bytes > 4,096 when shortening counted characters.
    from genomics_mcp.errors import InvalidInputError

    err = InvalidInputError("😀" * 10_000, hint="😀" * 10_000).info
    result = ToolResult(operation="get_reads", status=ResultStatus.ERROR, error=err)
    out = fit_to_response_budget(result, 4096)
    assert out.json_size() <= 4096
    assert out.error.code == ErrorCode.INVALID_INPUT
    assert out.error.message.startswith("😀") and out.error.message.endswith("...")
    assert out.metadata_omitted == {"error_hint": 1, "error_text": 1}


def test_sole_tiny_record_survives_when_only_metadata_is_large():
    # Coordinator reproduction: previously budget_exceeded with data=None.
    result = ToolResult(
        operation="get_reads",
        status=ResultStatus.OK,
        data={"records": [{"read": "tiny"}]},
        provenance=_big_provenance(),
    )
    out = fit_to_response_budget(result, 4096)
    assert out.json_size() <= 4096
    assert out.status is ResultStatus.OK
    assert out.data == {"records": [{"read": "tiny"}]}
    assert out.truncation is None  # no record was dropped, so none is reported
    assert out.metadata_omitted["provenance"] == 20 - len(out.provenance)


def test_all_records_kept_when_metadata_compaction_suffices():
    records = [{"i": i} for i in range(5)]
    result = ToolResult(
        operation="x",
        status=ResultStatus.OK,
        data={"records": records},
        provenance=_big_provenance(30),
    )
    out = fit_to_response_budget(result, 4096)
    assert out.json_size() <= 4096 and out.data["records"] == records and out.truncation is None
    assert out.metadata_omitted["provenance"] > 0
