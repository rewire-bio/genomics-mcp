"""Locate and verify indexes for a data file, independent of storage backend.

An explicit index is fetched and its content checked; an incompatible one is `invalid_input`.
Without an explicit index, conventional sidecars are *observed* one by one through a
backend-specific probe. Nothing is assumed to exist.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from genomics_mcp.errors import InvalidInputError
from genomics_mcp.models import Compression, FileFormat
from genomics_mcp.storage.formats import check_index, gzi_name, needs_index, sidecar_names


@dataclass
class Probe:
    state: Literal["present", "missing", "denied"]
    open_uri: str | None = None
    display: str | None = None
    head: bytes = b""
    size: int | None = None
    mtime: float | None = None


ProbeFn = Callable[[str], Awaitable[Probe]]
"""Probe a candidate location (a name/key/URL in the backend's own terms)."""


@dataclass
class IndexResult:
    state: Literal["present", "missing", "corrupt", "not_needed", "not_checked"]
    kind: str | None = None
    open_uri: str | None = None
    display: str | None = None
    problem: str | None = None
    explicit: bool = False
    mtime: float | None = None
    companions: dict[str, str] = field(default_factory=dict)
    companion_display: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    missing_companion: bool = False

    @property
    def readiness_state(self) -> str:
        return "missing" if self.missing_companion and self.state == "present" else self.state


async def locate_index(
    fmt: FileFormat | None,
    compression: Compression | None,
    *,
    data_name: str,
    explicit: str | None,
    probe: ProbeFn,
    probe_sidecars: bool = True,
    sidecar_note: str | None = None,
) -> IndexResult:
    if not needs_index(fmt):
        return IndexResult(state="not_needed")
    result: IndexResult
    if explicit is not None:
        p = await probe(explicit)
        if p.state == "missing":
            raise InvalidInputError(
                "the explicit index does not exist", details={"index": p.display or "?"}
            )
        if p.state == "denied":
            raise InvalidInputError(
                "the explicit index cannot be read (access denied)",
                details={"index": p.display or "?"},
            )
        check = check_index(fmt, p.head, size=p.size)
        if not check.ok:
            raise InvalidInputError(
                f"explicit index is not usable: {check.problem}",
                hint="pass the matching .bai/.csi/.crai/.tbi/.fai for this file, or omit index_uri",
                details={"index": p.display, "detected": check.kind},
            )
        result = IndexResult(
            state="present",
            kind=check.kind,
            open_uri=p.open_uri,
            display=p.display,
            explicit=True,
            mtime=p.mtime,
        )
    elif not probe_sidecars:
        result = IndexResult(state="not_checked", reasons=[sidecar_note] if sidecar_note else [])
    else:
        corrupt: IndexResult | None = None
        denied = False
        result = IndexResult(state="missing")
        for name, _kind in sidecar_names(data_name, fmt):
            p = await probe(name)
            if p.state == "missing":
                continue
            if p.state == "denied":
                denied = True
                continue
            check = check_index(fmt, p.head, size=p.size)
            if check.ok:
                result = IndexResult(
                    state="present",
                    kind=check.kind,
                    open_uri=p.open_uri,
                    display=p.display,
                    mtime=p.mtime,
                )
                break
            corrupt = corrupt or IndexResult(
                state="corrupt", kind=check.kind, display=p.display, problem=check.problem
            )
        else:
            if corrupt is not None:
                result = corrupt
            if denied:
                result.reasons.append("an index sidecar exists but access was denied")
    if fmt is FileFormat.FASTA and compression is Compression.BGZF:
        g = await probe(gzi_name(data_name))
        if g.state == "present":
            result.companions["gzi"] = g.open_uri or ""
            result.companion_display["gzi"] = g.display or ""
        else:
            result.reasons.append("BGZF FASTA needs a .gzi index next to the file")
            result.missing_companion = True
    return result
