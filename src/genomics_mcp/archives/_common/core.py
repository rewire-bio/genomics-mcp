"""Bridge between the archive/catalog clients and core: models, errors, limits, paging, HTTP.

Clients keep their own tested `SourceHttp` (host allowlists, bounded bodies, retries, manual
redirects, core `check_network_destination` on every hop). This module applies the per-call
core contract around them: the call deadline, `[sources.<name>]` timeout/rate settings,
`max_records` page sizes, conversion to core models and error codes, and redaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from pydantic import BaseModel

from genomics_mcp import errors as core_errors
from genomics_mcp import models as core_models
from genomics_mcp.archives._common.errors import SourceError
from genomics_mcp.archives._common.http import RateLimiter, make_client
from genomics_mcp.archives._common.models import SourcePage
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import ErrorCode, GenomicsError, redact_obj, register_secret
from genomics_mcp.result import OperationOutput, Truncation

_ERRORS: dict[str, type[GenomicsError]] = {
    "not_found": core_errors.NotFoundError,
    "unauthorized": core_errors.UnauthorizedError,
    "unsupported": core_errors.UnsupportedError,
    "invalid_input": core_errors.InvalidInputError,
    "preparation_required": core_errors.PreparationRequiredError,
    "upstream_error": core_errors.UpstreamError,
    "timeout": core_errors.DeadlineExceededError,
    "budget_exceeded": core_errors.BudgetExceededError,
}
_CORE_TYPES: dict[str, type[BaseModel]] = {
    "FileRef": core_models.FileRef,
    "Study": core_models.Study,
    "Dataset": core_models.Dataset,
    "Sample": core_models.Sample,
    "Reference": core_models.Reference,
    "Provenance": core_models.Provenance,
    "Interval": core_models.Interval,
    "Artifact": core_models.LocalArtifact,
}

HttpFactory = Callable[[], httpx.AsyncClient]


def to_core_error(exc: SourceError) -> GenomicsError:
    cls = _ERRORS.get(exc.code, GenomicsError)
    code = None if cls is not GenomicsError else ErrorCode.INTERNAL_ERROR
    return cls(
        exc.message,
        code=code,
        source=exc.source,
        retryable=exc.retryable,
        hint=exc.hint,
        details=exc.details,
    )


def to_core(obj: BaseModel) -> BaseModel:
    """Validate a client model as the core model of the same name; native data is redacted."""
    cls = _CORE_TYPES[type(obj).__name__]
    data = obj.model_dump()
    return cls.model_validate(redact_obj(data) if "native" in data else data)


class SourceRuntime:
    """Per-source state kept across calls: shared rate limiters and the HTTP client factory."""

    def __init__(self, source: str, *, http_factory: HttpFactory | None = None) -> None:
        self.source = source
        self.http_factory = http_factory or make_client
        self._limiters: dict[str, RateLimiter] = {}

    def limiter(self, name: str, default_interval_s: float, ctx: OperationContext) -> RateLimiter:
        """Shared limiter; `[sources.<name>].requests_per_minute` may only lower the rate."""
        cfg = ctx.settings.source(self.source)
        interval = default_interval_s
        if cfg.requests_per_minute:
            interval = max(interval, 60.0 / cfg.requests_per_minute)
        lim = self._limiters.get(name)
        if lim is None:
            lim = self._limiters[name] = RateLimiter(interval)
        lim.min_interval_s = interval
        return lim

    def timeout(self, ctx: OperationContext) -> float:
        """Remaining call deadline, lowered by the source's configured timeout."""
        remaining = ctx.deadline.ensure(ctx.operation.value)
        cfg = ctx.settings.source(self.source).timeout_s
        return min(remaining, cfg) if cfg else remaining

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        c = self.http_factory()
        try:
            yield c
        finally:
            await c.aclose()

    def api_key(self, ctx: OperationContext) -> Any:
        key = ctx.settings.source_api_key(self.source)
        if key is not None:
            register_secret(key.get_secret_value())
        return key


def share_limiters(runtime: SourceRuntime, ctx: OperationContext, *https: Any) -> None:
    """Replace each SourceHttp's limiter with the runtime's shared one (keyed by host set)."""
    for h in https:
        key = ",".join(sorted(h.policy.allowed_hosts))
        h.limiter = runtime.limiter(key, h.policy.min_interval_s, ctx)


def page_limit(req: Any, ctx: OperationContext, source_max: int) -> int:
    requested = getattr(req, "max_records", None) or ctx.limits.max_records
    return max(1, min(requested, ctx.limits.max_records, source_max))


def page_output(
    page: SourcePage,
    *,
    filter_fn: Callable[[Any], bool] | None = None,
    extra_warnings: Iterable[str] = (),
) -> OperationOutput:
    """Records under data.records, source paging as explicit `source_page` truncation."""
    items = [i for i in page.items if filter_fn is None or filter_fn(i)]
    records = [to_core(i).model_dump(mode="json") for i in items]
    warnings = [*page.warnings, *extra_warnings]
    if filter_fn is not None and len(items) != len(page.items):
        warnings.append(
            f"{len(page.items) - len(items)} record(s) on this source page did not match the "
            "requested formats"
        )
    trunc = None
    if page.next_cursor:
        trunc = Truncation(
            reason="source_page",
            returned=len(records),
            available=page.total,
            next_cursor=page.next_cursor,
        )
    data = {"records": records, "next_cursor": page.next_cursor, "total": page.total}
    return OperationOutput(
        data=data,
        truncation=trunc,
        warnings=warnings,
        provenance=[to_core(p) for p in page.provenance],
    )
