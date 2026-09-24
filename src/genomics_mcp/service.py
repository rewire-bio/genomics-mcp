"""Transport-independent service: validates requests, applies limits, dispatches, wraps results.

Every call returns a `ToolResult`. Errors are explicit envelopes; an operation without
a registered handler returns `unsupported` naming the epic that will provide it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from genomics_mcp import __version__
from genomics_mcp.catalog import PLANNED_SOURCES, SOURCE_SCHEMES
from genomics_mcp.config import Settings
from genomics_mcp.context import OperationContext, fan_out
from genomics_mcp.errors import (
    ErrorCode,
    ErrorInfo,
    GenomicsError,
    InvalidInputError,
    UnsupportedError,
    redact,
    redact_obj,
)
from genomics_mcp.models import SourceState, SourceStatus
from genomics_mcp.public import Deadline, PublicHttpClient
from genomics_mcp.registry import DEFAULT_KEY, OPERATIONS, Operation, Registry, SourceInfo
from genomics_mcp.requests import REQUEST_MODELS, ListSourcesRequest
from genomics_mcp.result import (
    EffectiveLimits,
    OperationOutput,
    ResultStatus,
    ToolResult,
    error_result,
    fit_to_response_budget,
)

log = logging.getLogger("genomics_mcp.service")


class GenomicsService:
    def __init__(
        self,
        settings: Settings,
        registry: Registry | None = None,
        *,
        load_providers: bool = True,
        http: PublicHttpClient | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry or Registry()
        for info in PLANNED_SOURCES:
            if self.registry.source(info.name) is None:
                self.registry.register_source(info)
        if load_providers:
            self.registry.load_providers()
        self._http = http

    @property
    def http(self) -> PublicHttpClient:
        if self._http is None:
            self._http = PublicHttpClient(self.settings)
        return self._http

    async def aclose(self) -> None:
        await self.registry.shutdown()
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------ public API
    async def call(
        self, operation: Operation | str, arguments: Mapping[str, Any] | BaseModel | None = None
    ) -> ToolResult:
        op = Operation(operation)
        request_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        limits = EffectiveLimits.build(self.settings.limits)
        try:
            request = self._validate(op, arguments)
            limits = EffectiveLimits.build(
                self.settings.limits,
                max_records=getattr(request, "max_records", None),
                max_response_bytes=getattr(request, "max_response_bytes", None),
            )
            ctx = OperationContext(
                operation=op,
                settings=self.settings,
                limits=limits,
                deadline=Deadline(limits.timeout_s),
                registry=self.registry,
                http=self.http,
                request_id=request_id,
                allow_external_annotation=bool(
                    getattr(request, "allow_external_annotation", False)
                ),
            )
            async with asyncio.timeout(ctx.deadline.remaining()):
                output = await self._dispatch(op, request, ctx)
            result = self._wrap(op, output, limits)
        except GenomicsError as exc:
            result = error_result(op.value, exc.info, limits=limits.applied())
        except TimeoutError:
            info = ErrorInfo(
                code=ErrorCode.TIMEOUT,
                message=f"{op.value} exceeded the {limits.timeout_s:g}s deadline",
                retryable=True,
            )
            result = error_result(op.value, info, limits=limits.applied())
        except Exception as exc:  # noqa: BLE001 - reported as internal_error, never as success
            log.error("request=%s op=%s unexpected %s", request_id, op.value, redact(repr(exc)))
            info = ErrorInfo(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"{op.value} failed unexpectedly (request {request_id})",
            )
            result = error_result(op.value, info, limits=limits.applied())
        result = fit_to_response_budget(result, limits.max_response_bytes)
        self._log_call(request_id, op, result, started, arguments)
        return result

    # ------------------------------------------------------------------ internals
    def _validate(
        self, op: Operation, arguments: Mapping[str, Any] | BaseModel | None
    ) -> BaseModel:
        model = REQUEST_MODELS[op]
        if isinstance(arguments, model):
            return arguments
        if isinstance(arguments, BaseModel):
            arguments = arguments.model_dump()
        try:
            return model.model_validate(dict(arguments or {}))
        except ValidationError as exc:
            problems = [
                {"field": ".".join(str(p) for p in e["loc"]) or "(request)", "problem": e["msg"]}
                for e in exc.errors()
            ]
            summary = "; ".join(f"{p['field']}: {p['problem']}" for p in problems)
            raise InvalidInputError(
                f"invalid arguments for {op.value}: {summary}", details={"errors": problems}
            ) from None

    def _unsupported(self, op: Operation, what: str) -> UnsupportedError:
        spec = OPERATIONS[op]
        keys = self.registry.keys(op)
        return UnsupportedError(
            f"{op.value} is not implemented {what} in this build",
            hint=f"planned in {', '.join(spec.planned_epics)}"
            + (f"; available: {', '.join(keys)}" if keys else ""),
            details={"operation": op.value, "available_keys": keys},
        )

    async def _dispatch(self, op: Operation, req: Any, ctx: OperationContext) -> OperationOutput:
        spec = OPERATIONS[op]
        if spec.dispatch == "builtin":
            return self._list_sources(req)

        if spec.dispatch == "source":
            key = req.source.strip().lower()
            if not self.settings.source(key).enabled:
                raise UnsupportedError(f"source {key} is disabled in configuration", source=key)
            h = self.registry.handler(op, key)
            if h is None:
                raise self._unsupported(op, f"for source {key!r}")
            return await h.handler(req, ctx)

        if spec.dispatch == "default":
            h = self.registry.handler(op, DEFAULT_KEY)
            if h is None:
                raise self._unsupported(op, "")
            return await h.handler(req, ctx)

        if spec.dispatch == "format":
            fmt = req.file.effective_format()
            if fmt is None:
                raise InvalidInputError(
                    "cannot infer the file format from its name; set file.format",
                    details={"accepted_formats": list(spec.formats)},
                )
            if fmt.value not in spec.formats:
                raise InvalidInputError(
                    f"{op.value} does not accept {fmt.value} files",
                    details={"accepted_formats": list(spec.formats)},
                )
            ctx.check_region(req.interval)
            h = self.registry.handler(op, fmt.value)
            if h is None:
                raise self._unsupported(op, f"for {fmt.value} files")
            return await h.handler(req, ctx)

        # fanout
        planner = self.registry.handler(op, DEFAULT_KEY)
        if planner is not None:
            return await planner.handler(req, ctx)
        return await self._fan_out(op, req, ctx)

    async def _fan_out(self, op: Operation, req: Any, ctx: OperationContext) -> OperationOutput:
        requested = [s.strip().lower() for s in (req.sources or self.registry.keys(op))]
        tasks = {}
        statuses: list[SourceStatus] = []
        errors: list[ErrorInfo] = []
        for key in dict.fromkeys(requested):
            h = self.registry.handler(op, key)
            if h is None:
                err = self._unsupported(op, f"for source {key!r}").info.model_copy(
                    update={"source": key}
                )
                errors.append(err)
                statuses.append(
                    SourceStatus(source=key, state=SourceState.NOT_IMPLEMENTED, message=err.message)
                )
            elif not self.settings.source(key).enabled:
                statuses.append(SourceStatus(source=key, state=SourceState.DISABLED))
            else:
                tasks[key] = functools.partial(h.handler, req)
        if not tasks:
            if errors and len(errors) == 1:
                raise UnsupportedError(
                    errors[0].message, hint=errors[0].hint, source=errors[0].source
                )
            raise self._unsupported(op, "for the requested sources")
        timeouts = [self.settings.source(k).timeout_s for k in tasks]
        per_task = min((t for t in timeouts if t), default=None)
        fo = await fan_out(ctx, tasks, timeout_s=per_task)
        data = {"by_source": {k: out.data for k, out in fo.outputs.items()}} if fo.outputs else None
        return OperationOutput(
            data=data,
            provenance=[p for out in fo.outputs.values() for p in out.provenance],
            warnings=[w for out in fo.outputs.values() for w in out.warnings],
            errors=[*errors, *fo.errors],
            source_status=[*statuses, *fo.statuses],
        )

    def _wrap(self, op: Operation, out: OperationOutput, limits: EffectiveLimits) -> ToolResult:
        warnings = [*limits.warnings, *out.warnings]
        common = {
            "operation": op.value,
            "errors": out.errors,
            "source_status": out.source_status,
            "provenance": out.provenance,
            "warnings": warnings,
            "truncation": out.truncation,
            "limits": limits.applied(),
        }
        if out.data is None and out.errors:
            codes = {e.code for e in out.errors}
            if len(out.errors) == 1:
                error = out.errors[0]
            else:
                code = codes.pop() if len(codes) == 1 else ErrorCode.UPSTREAM_ERROR
                error = ErrorInfo(
                    code=code,
                    message=f"all {len(out.errors)} sources failed; see errors",
                    retryable=any(e.retryable for e in out.errors),
                )
            return ToolResult(status=ResultStatus.ERROR, error=error, **common)
        status = ResultStatus.PARTIAL if out.errors else ResultStatus.OK
        return ToolResult(status=status, data=out.data, **common)

    def _list_sources(self, req: ListSourcesRequest) -> OperationOutput:
        items = [self.describe_source(info) for info in self.registry.sources()]
        if req.kind:
            items = [i for i in items if i["kind"] == req.kind]
        return OperationOutput(data={"records": items})

    def describe_source(self, info: SourceInfo) -> dict[str, Any]:
        ops = [op.value for op in Operation if self.registry.handler(op, info.name) is not None]
        schemes = [s for s in SOURCE_SCHEMES.get(info.name, ()) if self.registry.resolver(s)]
        cfg = self.settings.source(info.name)
        if not cfg.enabled:
            state = SourceState.DISABLED
        elif not ops and not schemes:
            state = SourceState.NOT_IMPLEMENTED
        elif info.auth == "required_key" and self.settings.source_api_key(info.name) is None:
            state = SourceState.NOT_CONFIGURED
        else:
            state = SourceState.OK
        return {
            "name": info.name,
            "title": info.title,
            "kind": info.kind,
            "state": state.value,
            "operations": ops,
            "schemes": schemes,
            "auth": info.auth,
            "homepage": info.homepage,
            "terms_url": info.terms_url,
            "notes": info.notes,
            "planned_epic": info.planned_epic,
        }

    def capabilities(self) -> dict[str, Any]:
        return {"server_version": __version__, **self.registry.capabilities()}

    def status(self) -> dict[str, Any]:
        return {
            "server_version": __version__,
            "sources": [self.describe_source(s) for s in self.registry.sources()],
            "config": self.settings.public_view(),
        }

    def _log_call(
        self,
        request_id: str,
        op: Operation,
        result: ToolResult,
        started: float,
        arguments: Any,
    ) -> None:
        extra = ""
        if self.settings.logging.log_query_content and isinstance(arguments, Mapping):
            extra = f" args={redact_obj(dict(arguments))!r}"
        log.info(
            "request=%s op=%s status=%s code=%s ms=%d%s",
            request_id,
            op.value,
            result.status.value,
            result.error.code.value if result.error else "-",
            (time.monotonic() - started) * 1000,
            extra,
        )
