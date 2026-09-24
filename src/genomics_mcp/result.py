"""Result envelope, limit helpers and response-size enforcement shared by every tool.

Handlers return `OperationOutput`. The service wraps it into `ToolResult` and calls
`fit_to_response_budget`, which trims `data.records` (or a top-level list) and records
the truncation. If nothing fits, the result becomes a `budget_exceeded` error rather
than an empty success.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from genomics_mcp.config import Limits
from genomics_mcp.errors import BudgetExceededError, ErrorInfo, InvalidInputError
from genomics_mcp.models import Interval, Provenance, SourceStatus

SCHEMA_VERSION = "1"


class ResultStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    """Some data returned; `errors`/`source_status` explain what is missing."""
    ERROR = "error"


TruncationReason = Literal[
    "max_records", "max_response_bytes", "max_region_bp", "source_page", "max_depth"
]


class Truncation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    truncated: bool = True
    reason: TruncationReason
    limit: int | None = None
    returned: int | None = None
    available: int | None = Field(default=None, description="Total available, if known.")
    next_cursor: str | None = None


class AppliedLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_region_bp: int
    max_records: int
    max_response_bytes: int
    timeout_s: float


class ToolResult(BaseModel):
    """Envelope returned by every tool, as structured content and as JSON text."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    operation: str
    status: ResultStatus
    data: Any = None
    error: ErrorInfo | None = Field(default=None, description="Set when status is error.")
    errors: list[ErrorInfo] = Field(
        default_factory=list, description="Per-source failures in partial results."
    )
    source_status: list[SourceStatus] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    truncation: Truncation | None = None
    limits: AppliedLimits | None = None

    def json_size(self) -> int:
        return len(self.model_dump_json().encode())


class OperationOutput(BaseModel):
    """What a registered handler returns. Put record lists under `data["records"]`."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    data: Any = None
    provenance: list[Provenance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorInfo] = Field(default_factory=list)
    source_status: list[SourceStatus] = Field(default_factory=list)
    truncation: Truncation | None = None


class EffectiveLimits(BaseModel):
    """Per-call limits: configured defaults, optionally lowered by the caller."""

    model_config = ConfigDict(extra="forbid")

    max_region_bp: int
    max_records: int
    max_response_bytes: int
    timeout_s: float
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        limits: Limits,
        *,
        max_records: int | None = None,
        max_response_bytes: int | None = None,
        max_region_bp: int | None = None,
    ) -> EffectiveLimits:
        warnings: list[str] = []

        def lower(name: str, requested: int | None, configured: int) -> int:
            if requested is None:
                return configured
            if requested <= 0:
                raise InvalidInputError(f"{name} must be positive")
            if requested > configured:
                warnings.append(
                    f"{name}={requested} exceeds the configured limit; using {configured}"
                )
                return configured
            return requested

        return cls(
            max_region_bp=lower("max_region_bp", max_region_bp, limits.max_region_bp),
            max_records=lower("max_records", max_records, limits.max_records),
            max_response_bytes=lower(
                "max_response_bytes", max_response_bytes, limits.max_response_bytes
            ),
            timeout_s=limits.interactive_timeout_s,
            warnings=warnings,
        )

    def applied(self) -> AppliedLimits:
        return AppliedLimits(
            max_region_bp=self.max_region_bp,
            max_records=self.max_records,
            max_response_bytes=self.max_response_bytes,
            timeout_s=self.timeout_s,
        )


def check_region(interval: Interval, limits: EffectiveLimits) -> None:
    """Reject intervals over the region limit. Never silently shrinks the region."""
    if interval.length > limits.max_region_bp:
        raise BudgetExceededError(
            f"interval spans {interval.length} bp; limit is {limits.max_region_bp} bp",
            hint="split the interval or query a smaller region",
            details={"length": interval.length, "max_region_bp": limits.max_region_bp},
        )


def take_records[T](
    records: Iterable[T], max_records: int, *, reason: TruncationReason = "max_records"
) -> tuple[list[T], Truncation | None]:
    """Take up to `max_records`, reading one extra item to detect truncation."""
    out: list[T] = []
    for item in records:
        if len(out) == max_records:
            return out, Truncation(reason=reason, limit=max_records, returned=max_records)
        out.append(item)
    return out, None


def error_result(operation: str, error: ErrorInfo, **extra: Any) -> ToolResult:
    return ToolResult(operation=operation, status=ResultStatus.ERROR, error=error, **extra)


def _records_container(data: Any) -> tuple[list[Any] | None, str | None]:
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"], "records"
    return None, None


def fit_to_response_budget(result: ToolResult, max_bytes: int) -> ToolResult:
    """Trim records until the serialized envelope fits `max_bytes`."""
    if result.json_size() <= max_bytes:
        return result
    data = result.model_dump(mode="json")["data"]
    records, key = _records_container(data)
    if records is None or not records:
        return _over_budget(result, max_bytes)

    def with_n(n: int) -> ToolResult:
        new_data: Any = records[:n] if key is None else {**data, key: records[:n]}
        prior = result.truncation
        trunc = Truncation(
            reason="max_response_bytes",
            limit=max_bytes,
            returned=n,
            available=prior.available if prior else None,
        )
        warnings = [
            *result.warnings,
            "response trimmed to fit max_response_bytes; narrow the interval or lower max_records",
        ]
        return result.model_copy(
            update={"data": new_data, "truncation": trunc, "warnings": warnings}
        )

    lo, hi = 0, len(records) - 1
    best: ToolResult | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = with_n(mid)
        if candidate.json_size() <= max_bytes:
            best, lo = candidate, mid + 1
        else:
            hi = mid - 1
    if best is None or best.truncation is None or best.truncation.returned == 0:
        return _over_budget(result, max_bytes)
    return best


def _over_budget(result: ToolResult, max_bytes: int) -> ToolResult:
    err = BudgetExceededError(
        f"response exceeds max_response_bytes ({max_bytes}) and cannot be trimmed",
        hint="narrow the interval, lower max_records or request summaries",
        details={"max_response_bytes": max_bytes},
    ).info
    return ToolResult(
        operation=result.operation,
        status=ResultStatus.ERROR,
        error=err,
        source_status=result.source_status,
        provenance=result.provenance,
        limits=result.limits,
    )
