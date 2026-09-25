"""Shared planning, dispatch and result assembly for the composition tools.

Components call the registered genomics handlers (`registry.handler(operation, format)`)
with typed sub-requests, under core `fan_out`. They never call `GenomicsService` and never
open files themselves, so storage, reader, consent and redaction rules stay in one place.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic_core import to_jsonable_python

from genomics_mcp.catalog import SOURCE_SCHEMES
from genomics_mcp.context import OperationContext, fan_out
from genomics_mcp.errors import (
    BudgetExceededError,
    ErrorCode,
    ErrorInfo,
    GenomicsError,
    InvalidInputError,
    UnsupportedError,
)
from genomics_mcp.models import FileFormat, FileRef, Interval, Provenance, SourceState
from genomics_mcp.registry import OPERATIONS, Operation
from genomics_mcp.result import OperationOutput, Truncation

# Nested lists copied into component metadata are cut to this many entries (and marked).
MAX_META_LIST = 50

Runner = Callable[[OperationContext], Awaitable[OperationOutput]]


# --------------------------------------------------------------------------- files


def file_label(file: FileRef) -> str:
    """Display form of a file: redacted, and without any query string or fragment."""
    shown = file.display_uri()
    if shown.startswith("/"):
        return shown
    parts = urlsplit(shown)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _storage_source(file: FileRef) -> str:
    for name, schemes in SOURCE_SCHEMES.items():
        if file.scheme in schemes:
            return name
    return file.scheme


@dataclass
class FileSlot:
    """One requested file, identified by its position (`f0`, `f1`, ...), never by its path."""

    id: str
    index: int
    file: FileRef
    fmt: FileFormat | None
    label: str
    config_sources: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "file": self.label,
            "format": self.fmt.value if self.fmt else None,
            "visibility": self.file.visibility.value,
            "assembly_asserted": self.file.assembly,
        }
        if self.file.source:
            out["source"] = self.file.source
        if self.file.accession:
            out["accession"] = self.file.accession
        return out


def make_slot(fid: str, index: int, file: FileRef) -> FileSlot:
    names = [_storage_source(file)]
    if file.source:
        names.insert(0, file.source.strip().lower())
    return FileSlot(
        fid, index, file, file.effective_format(), file_label(file), tuple(dict.fromkeys(names))
    )


def plan_slots(ctx: OperationContext, files: list[FileRef]) -> tuple[list[FileSlot], list[str]]:
    """Number the files and reject more than `limits.max_files_per_call` before any work."""
    cap = ctx.settings.limits.max_files_per_call
    if len(files) > cap:
        raise BudgetExceededError(
            f"{len(files)} files requested; at most {cap} per call",
            hint="split the files over several calls",
            details={"files": len(files), "max_files_per_call": cap},
        )
    slots: list[FileSlot] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}
    for i, f in enumerate(files):
        fid = f"f{i}"
        slot = make_slot(fid, i, f)
        key = f.accession or f.uri
        if key in seen:
            warnings.append(f"{fid} repeats {seen[key]} (same file); each is read separately")
        else:
            seen[key] = fid
        slots.append(slot)
    return slots, warnings


def check_assemblies(interval: Interval, files: Iterable[FileRef], reference: FileRef | None):
    """Every file with a declared assembly must use the interval's. No liftover or renaming."""
    bad = []
    for i, f in enumerate(files):
        if f.assembly and f.assembly != interval.assembly:
            bad.append({"file": f"f{i}", "path": file_label(f), "assembly": f.assembly})
    if reference is not None and reference.assembly and reference.assembly != interval.assembly:
        bad.append(
            {"file": "reference", "path": file_label(reference), "assembly": reference.assembly}
        )
    if bad:
        raise InvalidInputError(
            f"{len(bad)} file(s) declare an assembly other than {interval.assembly!r}; "
            "no liftover, build substitution or contig renaming is performed",
            details={"requested_assembly": interval.assembly, "mismatched": bad},
        )


def source_gate(ctx: OperationContext, slot: FileSlot) -> ErrorInfo | None:
    """A file whose source or storage backend is disabled in configuration is not read."""
    for name in slot.config_sources:
        if not ctx.settings.source(name).enabled:
            return ErrorInfo(
                code=ErrorCode.UNSUPPORTED,
                message=f"source {name!r} is disabled in configuration; {slot.id} was not read",
                source=slot.id,
                details={"disabled_source": name},
            )
    return None


def configured_timeout(ctx: OperationContext, slot: FileSlot | None) -> float | None:
    if slot is None:
        return None
    values = [ctx.settings.source(n).timeout_s for n in slot.config_sources]
    values = [v for v in values if v]
    return min(values) if values else None


# --------------------------------------------------------------------------- dispatch


def with_max_records(ctx: OperationContext, n: int) -> OperationContext:
    """Sub-context whose record limit is `n` (never above the caller's)."""
    n = max(1, min(n, ctx.limits.max_records))
    return dataclasses.replace(ctx, limits=ctx.limits.model_copy(update={"max_records": n}))


async def dispatch(ctx: OperationContext, op: Operation, request: Any) -> OperationOutput:
    """Call the registered handler for `op` and the file's format, as the service would."""
    spec = OPERATIONS[op]
    fmt = request.file.effective_format()
    if fmt is None:
        raise InvalidInputError(
            "cannot infer the file format from its name; set file.format",
            details={"accepted_formats": list(spec.formats)},
        )
    if fmt.value not in spec.formats:
        raise InvalidInputError(f"{op.value} does not accept {fmt.value} files")
    ctx.check_region(request.interval)
    h = ctx.registry.handler(op, fmt.value)
    if h is None:
        raise UnsupportedError(
            f"{op.value} is not available for {fmt.value} files in this build",
            hint=f"planned in {', '.join(spec.planned_epics)}",
            details={"operation": op.value, "format": fmt.value},
        )
    return await h.handler(request, ctx)


class _ComponentError(GenomicsError):
    """Carries an already-built ErrorInfo through fan_out, attributed to one component."""

    def __init__(self, info: ErrorInfo) -> None:
        self.info = info
        Exception.__init__(self, info.message)


def attribute(info: ErrorInfo, component: str) -> ErrorInfo:
    """Name the component in `source`; keep the original source in details."""
    if info.source == component:
        return info
    details = dict(info.details)
    if info.source is not None:
        details.setdefault("origin_source", info.source)
    details.setdefault("component", component)
    return info.model_copy(update={"source": component, "details": details})


@dataclass
class Component:
    id: str
    kind: str
    """Result role: sequence, alignments, coverage, variants, features, signal, evidence."""
    operation: Operation | None
    slot: FileSlot | None = None
    run: Runner | None = None
    timeout_s: float | None = None
    record_cap: int = 0
    notes: list[str] = field(default_factory=list)
    output: OperationOutput | None = None
    error: ErrorInfo | None = None
    state: SourceState = SourceState.SKIPPED
    records: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.output is not None and self.output.data is not None

    def skip(self, info: ErrorInfo, state: SourceState) -> None:
        self.error = attribute(info, self.id)
        self.state = state

    def errors(self) -> list[ErrorInfo]:
        if self.error is not None:
            return [self.error]
        if self.output is None:
            return []
        return [attribute(e, self.id) for e in self.output.errors]

    def truncation(self) -> Truncation | None:
        return self.output.truncation if self.output is not None else None

    def entry(self) -> dict[str, Any]:
        """Small, bounded description of this component for `data.components`."""
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "operation": self.operation.value if self.operation else None,
            "file": self.slot.id if self.slot else None,
            "status": self.state.value,
            "records_returned": len(self.records),
        }
        trunc = self.truncation()
        out["truncation"] = trunc.model_dump(mode="json", exclude_none=True) if trunc else None
        errs = self.errors()
        if errs:
            out["errors"] = [
                {"code": e.code.value, "message": e.message, **({"hint": e.hint} if e.hint else {})}
                for e in errs
            ]
        if self.meta:
            out.update(self.meta)
        if self.notes:
            out["notes"] = list(self.notes)
        if self.output is not None:
            out["provenance"] = [compact_provenance(p) for p in self.output.provenance]
        return out


def compact_provenance(p: Provenance) -> dict[str, Any]:
    return p.model_dump(mode="json", exclude_none=True, exclude_defaults=True) | {
        "source": p.source,
        "retrieved_at": p.retrieved_at.isoformat(),
    }


async def run_components(
    ctx: OperationContext, components: list[Component], *, budget_s: float | None = None
) -> None:
    """Run every runnable component concurrently; one failure or stall never fails the others.

    Each component is bounded by its configured source timeout, `budget_s` and the call
    deadline, whichever is first. Outcomes are stored on the components.
    """
    runnable = [c for c in components if c.run is not None and c.error is None]
    if not runnable:
        return

    def guarded(c: Component) -> Runner:
        async def go(sub: OperationContext) -> OperationOutput:
            try:
                return await c.run(sub)  # type: ignore[misc]
            except GenomicsError as exc:
                raise _ComponentError(attribute(exc.info, c.id)) from None

        return go

    def bound(c: Component) -> float | None:
        values = [v for v in (c.timeout_s, budget_s) if v is not None]
        return min(values) if values else None

    fo = await fan_out(
        ctx, {c.id: guarded(c) for c in runnable}, timeouts={c.id: bound(c) for c in runnable}
    )
    failures = {e.source: e for e in fo.errors}
    states = {s.source: s.state for s in fo.statuses}
    for c in runnable:
        if c.id in fo.outputs:
            c.output = fo.outputs[c.id]
            c.state = states.get(c.id, SourceState.OK)
            if c.output.data is None and not c.output.errors:
                c.error = ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=f"{c.id} returned no data and no error",
                    source=c.id,
                )
                c.state = SourceState.ERROR
        else:
            c.error = failures.get(c.id) or ErrorInfo(
                code=ErrorCode.INTERNAL_ERROR, message=f"{c.id} failed", source=c.id
            )
            c.state = states.get(c.id, SourceState.ERROR)


# --------------------------------------------------------------------------- assembly


def jsonable(value: Any) -> Any:
    return to_jsonable_python(value)


def bounded_meta(data: dict[str, Any], *, drop: Iterable[str] = ()) -> dict[str, Any]:
    """Handler metadata without records; long nested lists are cut and marked."""
    skip = {"records", "file", "interval", *drop}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in skip:
            continue
        value = jsonable(value)
        if isinstance(value, list) and len(value) > MAX_META_LIST:
            out[f"{key}_total"] = len(value)
            value = value[:MAX_META_LIST]
            out[f"{key}_listed"] = MAX_META_LIST
        out[key] = value
    return out


def interleave(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Round-robin across components, so trimming from the end keeps every component."""
    out: list[dict[str, Any]] = []
    longest = max((len(g) for g in groups), default=0)
    for i in range(longest):
        for g in groups:
            if i < len(g):
                out.append(g[i])
    return out


def tag(component: Component, record_type: str, record: Any) -> dict[str, Any]:
    return {"component": component.id, "type": record_type, "record": record}


def overall_truncation(
    components: list[Component], returned: int, cap: int, dropped: int
) -> Truncation | None:
    """One envelope-level truncation: the aggregate cap, else the first component's own."""
    if dropped:
        return Truncation(
            reason="max_records", limit=cap, returned=returned, available=returned + dropped
        )
    for c in components:
        t = c.truncation()
        if t is not None:
            return Truncation(reason=t.reason, limit=t.limit, returned=returned)
    return None


def share(total: int, parts: int, default_cap: int) -> int:
    return max(1, min(default_cap, total // max(1, parts)))
