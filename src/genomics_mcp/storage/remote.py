"""Resolution of remote files (public HTTPS, and S3 after presigning).

Data: range preflight (`bytes=0-0`), then a bounded head read for content sniffing when the
server honours ranges. Indexes: an explicit index is read (bounded) and checked; otherwise
conventional sidecars are observed with one bounded request each. URLs with a query string
(e.g. signed URLs) are not probed for sidecars, because a sidecar needs its own signature.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from genomics_mcp.errors import InvalidInputError, NotFoundError, UnauthorizedError
from genomics_mcp.models import FileRef
from genomics_mcp.storage.formats import HEAD_BYTES, content_matches, readiness, sniff
from genomics_mcp.storage.http import HttpAccess
from genomics_mcp.storage.indexes import Probe, locate_index
from genomics_mcp.storage.resolved import Access, StorageResolvedFile, display

if TYPE_CHECKING:
    from genomics_mcp.context import OperationContext

STEP_TIMEOUT_S = 15.0

# name (URL or key) -> (openable URL, display string)
Signer = Callable[[str], Awaitable[tuple[str, str]]]


def _step(ctx: OperationContext) -> float:
    return min(STEP_TIMEOUT_S, ctx.deadline.ensure("storage request"))


async def resolve_remote(
    ctx: OperationContext,
    http: HttpAccess,
    file: FileRef,
    *,
    access: Access,
    source: str,
    data_name: str,
    explicit_index: str | None,
    sign: Signer,
    probe_sidecars: bool,
    sidecar_note: str | None = None,
    extra_trusted: tuple[str, ...] = (),
    strict: bool = True,
) -> StorageResolvedFile:
    open_url, _ = await sign(data_name)
    pre = await http.preflight(
        open_url, source=source, timeout_s=_step(ctx), extra_trusted=extra_trusted
    )
    fmt = file.effective_format()
    reasons: list[str] = []
    compression = None
    kind = None
    if pre.range_capable:
        n = HEAD_BYTES if pre.size is None else max(1, min(HEAD_BYTES, pre.size))
        _, head, _, _ = await http.read_head(
            pre.final_url, n, source=source, timeout_s=_step(ctx), extra_trusted=extra_trusted
        )
        s = sniff(head)
        compression, kind = s.compression, s.kind
        if content_matches(fmt, s) is False:
            msg = f"file content looks like {s.kind}, not {fmt.value if fmt else '?'}"
            if strict:
                raise InvalidInputError(msg, hint="check the file or set file.format explicitly")
            reasons.append(msg)
    else:
        reasons.append("content not sniffed because the server ignored byte ranges")

    async def probe(name: str) -> Probe:
        url, shown = await sign(name)
        try:
            status, head, total, final = await http.read_head(
                url, HEAD_BYTES, source=source, timeout_s=_step(ctx), extra_trusted=extra_trusted
            )
        except NotFoundError:
            return Probe(state="missing", display=shown)
        except UnauthorizedError:
            return Probe(state="denied", display=shown)
        if status == 416:
            return Probe(state="present", open_uri=final, display=shown, head=b"", size=0)
        return Probe(state="present", open_uri=final, display=shown, head=head, size=total)

    idx = await locate_index(
        fmt,
        compression,
        data_name=data_name,
        explicit=explicit_index,
        probe=probe,
        probe_sidecars=probe_sidecars,
        sidecar_note=sidecar_note,
    )
    reasons.extend(idx.reasons)
    if idx.state == "corrupt" and idx.problem:
        reasons.append(f"index {idx.display}: {idx.problem}")
    ready = readiness(
        fmt,
        compression,
        index_state=idx.readiness_state,
        range_capable=pre.range_capable,
        local=False,
        extra_reasons=reasons,
    )
    enriched = file.model_copy(
        update={
            "size_bytes": pre.size if pre.size is not None else file.size_bytes,
            "compression": compression or file.compression,
            "readiness": ready,
            "index_uri": file.index_uri or (idx.display if idx.state == "present" else None),
            "source": file.source or access,
        }
    )
    expiries = [e for e in (pre.expires_at,) if e is not None]
    return StorageResolvedFile(
        file=enriched,
        open_uri=pre.final_url,
        index_open_uri=idx.open_uri if idx.state == "present" else None,
        range_capable=pre.range_capable,
        readiness=ready,
        expires_at=min(expiries) if expiries else None,
        access=access,
        size_bytes=pre.size,
        etag=pre.etag,
        last_modified=pre.last_modified,
        compression=compression,
        content_kind=kind,
        index_kind=idx.kind,
        index_state=idx.state,
        index_display=idx.display,
        index_problem=idx.problem,
        index_explicit=idx.explicit,
        companion_open_uris=idx.companions,
        companion_display=idx.companion_display,
        redirected=pre.redirected,
        endpoint_trusted_host=extra_trusted[0] if extra_trusted else None,
    )


class HttpResolver:
    """Public HTTP(S) files. Plain http only for hosts in storage.local_network_hosts."""

    def __init__(self, http_for: Callable[[OperationContext], HttpAccess]) -> None:
        self.http_for = http_for

    async def _resolve(self, file: FileRef, ctx: OperationContext, strict: bool):
        explicit = file.index_uri
        if explicit is not None and urlsplit(explicit).scheme.lower() not in ("http", "https"):
            raise InvalidInputError("an http(s) file needs an http(s) index_uri")
        has_query = bool(urlsplit(file.uri).query)

        async def sign(name: str) -> tuple[str, str]:
            return name, display(name)

        return await resolve_remote(
            ctx,
            self.http_for(ctx),
            file,
            access="https",
            source="https",
            data_name=file.uri,
            explicit_index=explicit,
            sign=sign,
            probe_sidecars=not has_query,
            sidecar_note="sidecar indexes are not probed for URLs with a query string; "
            "pass index_uri explicitly"
            if has_query
            else None,
            strict=strict,
        )

    async def resolve(self, file: FileRef, ctx: OperationContext) -> StorageResolvedFile:
        return await self._resolve(file, ctx, True)

    async def stat(self, file: FileRef, ctx: OperationContext) -> FileRef:
        return (await self._resolve(file, ctx, False)).file
