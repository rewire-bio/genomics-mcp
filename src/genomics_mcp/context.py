"""Per-call context handed to every handler, plus concurrent fan-out with failure isolation."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from genomics_mcp.config import Settings
from genomics_mcp.contracts import REGION_ONLY_SCHEMES, RegionFileResolver, ResolvedFile
from genomics_mcp.errors import (
    DeadlineExceededError,
    ErrorCode,
    ErrorInfo,
    GenomicsError,
    UnsupportedError,
    UpstreamError,
    redact,
)
from genomics_mcp.models import FileRef, Interval, SourceState, SourceStatus
from genomics_mcp.public import Deadline, EgressContext, PublicHttpClient
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import EffectiveLimits, OperationOutput, check_region
from genomics_mcp.security import resolve_local_path

log = logging.getLogger("genomics_mcp.context")


@dataclass
class OperationContext:
    operation: Operation
    settings: Settings
    limits: EffectiveLimits
    deadline: Deadline
    registry: Registry
    http: PublicHttpClient
    request_id: str
    allow_external_annotation: bool = False
    """Per-call consent to send private-file-derived values to external sources."""

    def component(self, name: str) -> Any | None:
        return self.registry.component(name)

    def require_component(self, name: str) -> Any:
        comp = self.registry.component(name)
        if comp is None:
            raise UnsupportedError(f"component {name!r} is not available in this build")
        return comp

    def check_region(self, interval: Interval) -> None:
        check_region(interval, self.limits)

    def resolve_local_path(self, path: str | Path, *, must_exist: bool = True) -> Path:
        return resolve_local_path(self.settings, path, must_exist=must_exist)

    async def resolve_file(
        self, file: FileRef, *, interval: Interval | None = None
    ) -> ResolvedFile:
        """Resolve a file for reading. Pass `interval` for every region query.

        With an interval, a resolver implementing `RegionFileResolver.resolve_region` is used
        (e.g. EGA htsget returns only that region). Region-only schemes (ega, htsget) are never
        resolved as whole files when an interval is given.
        """
        resolver = self.registry.resolver(file.scheme)
        if resolver is None:
            raise UnsupportedError(
                f"no storage resolver for {file.scheme!r} URIs in this build",
                details={"scheme": file.scheme},
            )
        if interval is not None:
            if isinstance(resolver, RegionFileResolver):
                resolved = await resolver.resolve_region(file, interval, self)
                if file.scheme in REGION_ONLY_SCHEMES and not _covers(resolved.region, interval):
                    raise UpstreamError(
                        f"{file.scheme!r} resolver did not return a region covering the request",
                        source=file.scheme,
                    )
                return resolved
            if file.scheme in REGION_ONLY_SCHEMES:
                raise UnsupportedError(
                    f"{file.scheme!r} resolver cannot serve a bounded region",
                    details={"scheme": file.scheme},
                )
        return await resolver.resolve(file, self)

    def egress_for(self, files: Iterable[FileRef]) -> EgressContext:
        return EgressContext.for_files(files, consent=self.allow_external_annotation)

    async def run_blocking[T](self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Run blocking I/O (pysam, pyBigWig) in a thread, bounded by the call deadline.

        On timeout the caller gets `timeout`; the worker thread finishes in the background,
        so blocking code must itself read bounded amounts.
        """
        remaining = self.deadline.ensure(self.operation.value)
        try:
            async with asyncio.timeout(remaining):
                return await asyncio.to_thread(fn, *args, **kwargs)
        except TimeoutError:
            raise DeadlineExceededError(
                f"{self.operation.value} exceeded the {self.deadline.seconds:g}s deadline"
            ) from None

    def child(self, timeout_s: float | None = None) -> OperationContext:
        """Context with a deadline no later than this one."""
        seconds = self.deadline.remaining()
        if timeout_s is not None:
            seconds = min(seconds, timeout_s)
        return dataclasses.replace(self, deadline=Deadline(seconds))


def _covers(region: Interval | None, interval: Interval) -> bool:
    return (
        region is not None
        and region.assembly == interval.assembly
        and region.contig == interval.contig
        and region.start <= interval.start
        and region.end >= interval.end
    )


_STATE_BY_CODE = {
    ErrorCode.NOT_FOUND: SourceState.NOT_FOUND,
    ErrorCode.UNAUTHORIZED: SourceState.UNAUTHORIZED,
    ErrorCode.TIMEOUT: SourceState.TIMEOUT,
    ErrorCode.UNSUPPORTED: SourceState.NOT_IMPLEMENTED,
    ErrorCode.UPSTREAM_ERROR: SourceState.UNAVAILABLE,
    ErrorCode.CONSENT_REQUIRED: SourceState.SKIPPED,
}


def state_for(error: ErrorInfo) -> SourceState:
    return _STATE_BY_CODE.get(error.code, SourceState.ERROR)


@dataclass
class FanOutResult:
    outputs: dict[str, OperationOutput] = field(default_factory=dict)
    statuses: list[SourceStatus] = field(default_factory=list)
    errors: list[ErrorInfo] = field(default_factory=list)


Task = Callable[[OperationContext], Awaitable[OperationOutput]]


async def fan_out(
    ctx: OperationContext,
    tasks: Mapping[str, Task],
    *,
    timeout_s: float | None = None,
    timeouts: Mapping[str, float | None] | None = None,
) -> FanOutResult:
    """Run named tasks concurrently. One task failing or timing out never fails the others.

    Each task gets its own bound: `timeouts[name]` if given, else `timeout_s`, always capped by
    the remaining call deadline. One task's short timeout never shortens another's.

    Unexpected exceptions become `internal_error` for that task only and are logged.
    """

    async def run(name: str, task: Task) -> tuple[str, OperationOutput | ErrorInfo]:
        own = timeouts.get(name) if timeouts and name in timeouts else timeout_s
        sub = ctx.child(own)
        try:
            async with asyncio.timeout(sub.deadline.remaining()):
                return name, await task(sub)
        except GenomicsError as exc:
            info = exc.info
        except TimeoutError:
            info = ErrorInfo(
                code=ErrorCode.TIMEOUT, message=f"{name} did not finish in time", retryable=True
            )
        except Exception as exc:  # noqa: BLE001 - isolate one source; reported, not hidden
            log.error("fan-out task %s failed: %s", name, redact(repr(exc)), exc_info=False)
            info = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message=f"{name} failed unexpectedly")
        if info.source is None:
            info = info.model_copy(update={"source": name})
        return name, info

    result = FanOutResult()
    for name, outcome in await asyncio.gather(*(run(n, t) for n, t in tasks.items())):
        if isinstance(outcome, ErrorInfo):
            result.errors.append(outcome)
            result.statuses.append(
                SourceStatus(source=name, state=state_for(outcome), message=outcome.message)
            )
        else:
            result.outputs[name] = outcome
            partial = bool(outcome.errors)
            result.statuses.append(
                SourceStatus(source=name, state=SourceState.PARTIAL if partial else SourceState.OK)
            )
            result.errors.extend(outcome.errors)
    return result
