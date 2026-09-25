"""Local file resolver: allowed roots after symlink resolution, observed sidecar indexes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from genomics_mcp.config import Settings
from genomics_mcp.errors import (
    InvalidInputError,
    NotFoundError,
    UnauthorizedError,
)
from genomics_mcp.models import FileRef
from genomics_mcp.security import resolve_local_path
from genomics_mcp.storage.formats import HEAD_BYTES, content_matches, readiness, sniff
from genomics_mcp.storage.indexes import Probe, locate_index
from genomics_mcp.storage.resolved import StorageResolvedFile
from genomics_mcp.storage.uris import local_path_from_uri

if TYPE_CHECKING:
    from genomics_mcp.context import OperationContext


def read_head(path: Path, n: int = HEAD_BYTES) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(n)
    except PermissionError:
        raise UnauthorizedError(
            "permission denied reading local file", source="local", details={"path": str(path)}
        ) from None
    except IsADirectoryError:
        raise InvalidInputError(
            "path is a directory, not a file", details={"path": str(path)}
        ) from None


def resolve_path(settings: Settings, uri: str) -> Path:
    """Validated absolute path under an allowed root (symlinks resolved first)."""
    return resolve_local_path(settings, local_path_from_uri(uri))


def _probe_local(settings: Settings):
    async def probe(candidate: str) -> Probe:
        try:
            path = resolve_path(settings, candidate)
        except NotFoundError:
            return Probe(state="missing", display=candidate)
        except UnauthorizedError:
            # A sidecar symlink pointing outside the roots is not readable to us.
            return Probe(state="denied", display=candidate)
        if not path.is_file():
            return Probe(state="missing", display=candidate)
        try:
            head = read_head(path)
        except UnauthorizedError:
            return Probe(state="denied", display=candidate)
        st = path.stat()
        return Probe(
            state="present",
            open_uri=str(path),
            display=candidate,
            head=head,
            size=st.st_size,
            mtime=st.st_mtime,
        )

    return probe


async def resolve_local(
    settings: Settings, file: FileRef, *, strict: bool = True
) -> StorageResolvedFile:
    """Resolve a local file. `strict` raises on content that does not match the format."""
    given = local_path_from_uri(file.uri)
    path = resolve_path(settings, file.uri)
    if path.is_dir():
        raise InvalidInputError("path is a directory, not a file", details={"path": given})
    if not os.access(path, os.R_OK):
        raise UnauthorizedError("permission denied reading local file", source="local")
    head = read_head(path)
    st = path.stat()
    s = sniff(head)
    fmt = file.effective_format()
    match = content_matches(fmt, s)
    reasons: list[str] = []
    if match is False:
        msg = f"file content looks like {s.kind}, not {fmt.value if fmt else '?'}"
        if strict:
            raise InvalidInputError(
                msg, hint="check the file or set file.format explicitly", details={"path": given}
            )
        reasons.append(msg)
    explicit = None
    if file.index_uri is not None:
        if not (file.index_uri.startswith("/") or file.index_uri.lower().startswith("file:")):
            raise InvalidInputError("a local file needs a local index_uri")
        explicit = file.index_uri
    idx = await locate_index(
        fmt, s.compression, data_name=given, explicit=explicit, probe=_probe_local(settings)
    )
    reasons.extend(idx.reasons)
    if idx.state == "present" and idx.mtime is not None and idx.mtime < st.st_mtime:
        reasons.append("index is older than the data file; it may be stale")
    if idx.state == "corrupt" and idx.problem:
        reasons.append(f"index {idx.display}: {idx.problem}")
    ready = readiness(
        fmt,
        s.compression,
        index_state=idx.readiness_state,
        range_capable=True,
        local=True,
        extra_reasons=reasons,
    )
    enriched = file.model_copy(
        update={
            "size_bytes": st.st_size,
            "compression": s.compression,
            "readiness": ready,
            "index_uri": file.index_uri or (idx.display if idx.state == "present" else None),
            "source": file.source or "local",
        }
    )
    return StorageResolvedFile(
        file=enriched,
        open_uri=str(path),
        index_open_uri=idx.open_uri if idx.state == "present" else None,
        local_path=path,
        range_capable=True,
        readiness=ready,
        access="local",
        size_bytes=st.st_size,
        # Local identity for transfer reuse: nanosecond mtime plus inode, not size alone.
        last_modified=f"mtime_ns={st.st_mtime_ns};ino={st.st_ino}",
        compression=s.compression,
        content_kind=s.kind,
        index_kind=idx.kind,
        index_state=idx.state,
        index_display=idx.display,
        index_problem=idx.problem,
        index_explicit=idx.explicit,
        companion_open_uris=idx.companions,
        companion_display=idx.companion_display,
    )


class LocalResolver:
    """Resolver for absolute paths and file:// URIs."""

    async def resolve(self, file: FileRef, ctx: OperationContext) -> StorageResolvedFile:
        return await resolve_local(ctx.settings, file)

    async def stat(self, file: FileRef, ctx: OperationContext) -> FileRef:
        return (await resolve_local(ctx.settings, file, strict=False)).file
