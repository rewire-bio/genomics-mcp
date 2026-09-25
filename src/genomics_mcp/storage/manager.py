"""`StorageManager`: the long-lived storage component other providers use.

Registered as component ``"storage"``. Readers (E3-E5) get it with
``ctx.require_component("storage")`` and use:

- `require_ready(resolved, needs_index=...)` - raise a typed error unless a region query can run;
- `async with native_call(ctx) as call:` then `await call.reader_params(resolved)` (remote
  files become loopback proxy routes owned by this call, see proxy.py) and
  `await call.run(task, params, what=...)` (isolated child process);
- `provenance(resolved, method=..., transformations=...)`.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from genomics_mcp.config import Settings
from genomics_mcp.contracts import ResolvedFile
from genomics_mcp.errors import (
    BudgetExceededError,
    GenomicsError,
    PreparationRequiredError,
    UnauthorizedError,
    UnsupportedError,
)
from genomics_mcp.models import Compression, FileFormat, FileRef, Provenance, ReadinessState
from genomics_mcp.security import is_metadata_destination
from genomics_mcp.storage.http import HttpAccess
from genomics_mcp.storage.local import resolve_local
from genomics_mcp.storage.native import NativeRunner
from genomics_mcp.storage.proxy import RangeProxy
from genomics_mcp.storage.s3 import S3Clients

if TYPE_CHECKING:
    from genomics_mcp.context import OperationContext


MAX_STAGED_INDEX_BYTES = 64 * 1024 * 1024


def _trusted(resolved: ResolvedFile) -> tuple[str, ...]:
    return tuple(h for h in [getattr(resolved, "endpoint_trusted_host", None)] if h)


def source_name(file: FileRef) -> str:
    if file.source:
        return file.source
    return {"file": "local", "http": "https", "https": "https"}.get(file.scheme, file.scheme)


class StorageManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = HttpAccess(settings)
        self.native = NativeRunner(settings)
        self.s3 = S3Clients(settings, settings.paths.work_dir / ".isolation" / "botocore")
        self.proxy = RangeProxy(self.http)

    async def aclose(self) -> None:
        await self.proxy.aclose()
        await self.http.aclose()

    # ------------------------------------------------------------------ readiness
    def require_ready(self, resolved: ResolvedFile, *, needs_index: bool = True) -> None:
        """Raise `preparation_required`/`unsupported` unless a region query can run now."""
        name = resolved.file.display_uri()
        for uri in (resolved.open_uri, resolved.index_open_uri):
            if uri and not uri.startswith("/") and is_metadata_destination(urlsplit(uri).hostname):
                raise UnauthorizedError("refusing a cloud metadata destination")
        state = resolved.readiness.state
        reasons = "; ".join(resolved.readiness.reasons)
        if resolved.local_path is None and resolved.range_capable is False:
            raise PreparationRequiredError(
                "the server does not honour byte-range requests, so the file cannot be "
                "queried in place",
                hint="download it with fetch_file (within the transfer budget) and query the "
                "local artifact",
                details={"file": name, "readiness": "download_required"},
            )
        if state is ReadinessState.UNSUPPORTED:
            raise UnsupportedError(
                f"file is not queryable by locus: {reasons}", details={"file": name}
            )
        if state is ReadinessState.NOT_LOCUS_READY:
            raise PreparationRequiredError(
                f"file is not ready for region queries: {reasons}",
                hint="fetch_file with prepare=true builds an indexed copy in the work dir",
                details={"file": name, "readiness": state.value},
            )
        if needs_index and resolved.index_open_uri is None and resolved.region is None:
            index_state = getattr(resolved, "index_state", "missing")
            problem = getattr(resolved, "index_problem", None)
            shown = getattr(resolved, "index_display", None)
            if index_state == "corrupt":
                raise PreparationRequiredError(
                    f"the index next to this file is not valid: {problem}",
                    hint="rebuild the index (samtools/tabix/bcftools index) or pass index_uri; "
                    "fetch_file with prepare=true builds a fresh index on a local copy",
                    details={"file": name, "index": shown},
                )
            raise PreparationRequiredError(
                "no index was found for this file; region queries need one",
                hint="pass file.index_uri, or fetch_file with prepare=true to build one on a "
                "local copy",
                details={"file": name, "reasons": resolved.readiness.reasons},
            )
        if (
            needs_index
            and resolved.file.effective_format() is FileFormat.FASTA
            and getattr(resolved, "compression", None) is Compression.BGZF
            and "gzi" not in getattr(resolved, "companion_open_uris", {})
        ):
            raise PreparationRequiredError(
                "BGZF FASTA needs a .gzi index next to the file",
                hint="run samtools faidx on it, or fetch_file with prepare=true",
                details={"file": name},
            )

    async def describe_local(self, file: FileRef) -> ResolvedFile:
        """Resolve a local file (e.g. a new artifact) without failing on content mismatch."""
        return await resolve_local(self.settings, file, strict=False)

    @contextlib.asynccontextmanager
    async def native_call(self, ctx: OperationContext) -> AsyncIterator[NativeCall]:
        """One native reader invocation. Its proxy routes belong to a private lease, so
        concurrent readers of the same request (fan-out) never release each other's routes.
        Routes are released when the block exits (success, error or cancellation)."""
        call = NativeCall(self, ctx, uuid.uuid4().hex)
        try:
            yield call
        finally:
            await self.proxy.release(call.lease)

    @contextlib.asynccontextmanager
    async def staged_indexes(
        self, ctx: OperationContext, resolved: ResolvedFile, params: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Copy remote index files (bounded) to a private temp dir for readers that need local
        index paths (pysam FastaFile). Removed afterwards. Yields updated params."""
        companions = dict(getattr(resolved, "companion_open_uris", {}) or {})
        remote = [
            (key, uri)
            for key, uri in [("index", resolved.index_open_uri), *companions.items()]
            if uri and not uri.startswith("/")
        ]
        if not remote:
            yield params
            return
        tmp = Path(tempfile.mkdtemp(prefix="idx-", dir=self._staging_root()))
        try:
            out = {**params, "companions": dict(params["companions"])}
            trusted = _trusted(resolved)
            for key, uri in remote:
                _status, body, total, _ = await self.http.read_head(
                    uri,
                    MAX_STAGED_INDEX_BYTES + 1,
                    source=source_name(resolved.file),
                    timeout_s=min(30.0, ctx.deadline.ensure("index download")),
                    extra_trusted=trusted,
                )
                if len(body) > MAX_STAGED_INDEX_BYTES or (total or 0) > MAX_STAGED_INDEX_BYTES:
                    raise BudgetExceededError(
                        f"remote {key} index is larger than {MAX_STAGED_INDEX_BYTES} bytes",
                        hint="fetch the file and its index with fetch_file",
                    )
                path = tmp / f"staged.{key}"
                path.write_bytes(body)
                if key == "index":
                    out["index"] = str(path)
                else:
                    out["companions"][key] = str(path)
            yield out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _staging_root(self) -> Path:
        root = self.settings.paths.work_dir / ".isolation" / "staged"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def provenance(
        self,
        resolved: ResolvedFile,
        *,
        method: str,
        transformations: list[str] | None = None,
    ) -> Provenance:
        return Provenance(
            source=source_name(resolved.file),
            source_record_id=resolved.file.accession,
            url=resolved.file.display_uri(),
            method=method,
            transformations=transformations or [],
        )


class NativeCall:
    """Registered locations and the native task of one reader invocation."""

    def __init__(self, manager: StorageManager, ctx: OperationContext, lease: str) -> None:
        self.manager = manager
        self.ctx = ctx
        self.lease = lease

    async def reader_params(self, resolved: ResolvedFile) -> dict[str, Any]:
        """Locations for the native child.

        Remote URLs are never handed to native code: each is registered with the loopback
        range proxy under this call's lease, and the child gets an opaque 127.0.0.1 route.
        Every upstream hop is then made by the guarded Python transport.
        """
        ctx, proxy = self.ctx, self.manager.proxy
        name = resolved.file.display_uri().split("?", 1)[0].rsplit("/", 1)[-1]

        async def route(uri: str | None, suffix: str) -> str | None:
            if uri is None or uri.startswith("/"):
                return uri
            scheme = urlsplit(uri).scheme.lower()
            if scheme not in ("http", "https"):
                raise UnsupportedError(f"native readers cannot open {scheme!r} locations")
            return await proxy.register(
                self.lease,
                uri,
                source=source_name(resolved.file),
                name=name + suffix,
                expires=time.monotonic() + ctx.deadline.remaining(),
                size=getattr(resolved, "size_bytes", None) if suffix == "" else None,
                extra_trusted=_trusted(resolved),
            )

        companions = dict(getattr(resolved, "companion_open_uris", {}) or {})
        return {
            "uri": await route(resolved.open_uri, ""),
            "index": await route(resolved.index_open_uri, ".index"),
            "companions": {k: await route(v, "." + k) for k, v in companions.items()},
            "local": resolved.local_path is not None,
            "region_slice": resolved.region is not None,
        }

    async def run(self, task: str, params: dict[str, Any], *, what: str) -> Any:
        """Run the native task. If it failed while the proxy refused or failed one of this
        call's upstream requests, that typed error (e.g. `unauthorized`) is raised."""
        try:
            return await self.manager.native.run(
                task, params, timeout_s=self.ctx.deadline.ensure(what), what=what
            )
        except GenomicsError:
            upstream = self.manager.proxy.errors(self.lease)
            if upstream:
                raise upstream[0] from None
            raise
