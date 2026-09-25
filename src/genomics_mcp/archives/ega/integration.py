"""EGA wiring into core: the `ega://` resolver (region-only) and a whole-file transfer backend.

* `EgaResolver.resolve_region(file, interval, ctx)` asks EGA htsget for exactly the caller's
  0-based half-open interval, writes the bounded BAM/VCF slice to the private work directory
  and returns a `ResolvedFile` whose `region` is that interval. Readers post-filter overlap and
  may index the artifact. The slice carries its own SHA-256/MD5, never the whole-file MD5.
* `EgaResolver.resolve` never downloads: EGA whole files need bearer auth and a byte budget,
  which a plain open URI cannot carry. It raises `preparation_required`.
* `EgaTransferBackend` (registry component `transfer_backend:ega`) is the documented minimal
  stream interface for the core transfer manager (E3): `describe` for size/MD5 (no download),
  then `open(start, end)` for authorised plain bytes, so budgets, quotas, resume, cancellation
  and MD5 checks stay in the manager.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from genomics_mcp.archives._common.core import SourceRuntime, share_limiters, to_core, to_core_error
from genomics_mcp.archives._common.errors import SourceError
from genomics_mcp.archives._common.workspace import safe_name
from genomics_mcp.archives.ega import htsget as hg
from genomics_mcp.archives.ega.access import EgaAccess, EgaAccessConfig
from genomics_mcp.archives.ega.client import EgaClient, accession_kind
from genomics_mcp.context import OperationContext
from genomics_mcp.contracts import ResolvedFile
from genomics_mcp.errors import (
    BudgetExceededError,
    InvalidInputError,
    PreparationRequiredError,
    UnauthorizedError,
)
from genomics_mcp.models import (
    Checksum,
    Compression,
    FileFormat,
    FileRef,
    Interval,
    Readiness,
    ReadinessState,
    utcnow,
)

REGION_BUDGET_BYTES = hg.DEFAULT_REGION_BUDGET
TRANSFER_BACKEND_COMPONENT = "transfer_backend:ega"


def ega_accession(file: FileRef) -> str:
    """Accession from `ega://EGAF...` (or `file.accession` for the same scheme). Validated."""
    raw = file.uri.split("://", 1)[1].strip("/") if file.uri.startswith("ega://") else ""
    acc = raw or (file.accession or "")
    if file.accession and raw and file.accession != raw:
        raise InvalidInputError("file.accession does not match the ega:// URI", source="ega")
    try:
        if accession_kind(acc) != "file":
            raise InvalidInputError(f"{acc} is not an EGA file (EGAF) accession", source="ega")
    except SourceError as exc:
        raise to_core_error(exc) from None
    return acc


def _workspace_usage(root: Path, limit_entries: int = 200_000) -> int:
    total = 0
    count = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            count += 1
            if count > limit_entries:
                return total
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


class _EgaBase:
    def __init__(self, runtime: SourceRuntime, access: EgaAccess) -> None:
        self.runtime = runtime
        self.access = access

    @asynccontextmanager
    async def _client(
        self, ctx: OperationContext, *, require_auth: bool
    ) -> AsyncIterator[EgaClient]:
        cfg = EgaAccessConfig.from_settings(ctx.settings)
        if require_auth and cfg.mode == "anonymous":
            raise UnauthorizedError(
                "EGA file access needs explicitly configured credentials",
                source="ega",
                hint="Set [sources.ega].api_key_env to a personal EGA token, or GENOMICS_MCP_EGA_USERNAME/"
                "GENOMICS_MCP_EGA_PASSWORD, or GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1 for EGA's public "
                "test data.",
            )
        async with self.runtime.client() as http:
            try:
                auth = await self.access.auth(cfg, http)
            except SourceError as exc:
                raise to_core_error(exc) from None
            client = EgaClient(http, auth=auth)
            share_limiters(self.runtime, ctx, client.meta, client.data)
            yield client


class EgaResolver(_EgaBase):
    """Resolver for `ega://EGAF...` URIs (region-only; see module docstring)."""

    async def stat(self, file: FileRef, ctx: OperationContext) -> FileRef:
        """Source metadata; with credentials, a header-only htsget ticket proves region access."""
        acc = ega_accession(file)
        cfg = EgaAccessConfig.from_settings(ctx.settings)
        async with self._client(ctx, require_auth=False) as c:
            try:
                if cfg.mode == "anonymous":
                    ref = await c.get_file(acc, timeout_s=self.runtime.timeout(ctx))
                else:
                    endpoint = (
                        "variants" if file.format in (FileFormat.VCF, FileFormat.BCF) else "reads"
                    )
                    ref = await c.check_file(
                        acc, endpoint=endpoint, timeout_s=self.runtime.timeout(ctx)
                    )
            except SourceError as exc:
                raise to_core_error(exc) from None
        out = to_core(ref)
        assert isinstance(out, FileRef)
        return out.model_copy(
            update={"visibility": file.visibility, "assembly": file.assembly or out.assembly}
        )

    async def resolve(self, file: FileRef, ctx: OperationContext) -> ResolvedFile:
        raise PreparationRequiredError(
            "EGA files are served as bounded htsget regions (pass an interval) or downloaded "
            "explicitly through the transfer manager; they are never opened as whole remote files",
            source="ega",
            hint=f"use fetch_file with an explicit budget (backend {TRANSFER_BACKEND_COMPONENT!r})",
        )

    async def resolve_region(
        self, file: FileRef, interval: Interval, ctx: OperationContext
    ) -> ResolvedFile:
        acc = ega_accession(file)
        fmt = file.format
        if fmt in (FileFormat.BAM, FileFormat.CRAM):
            endpoint = "reads"
        elif fmt in (FileFormat.VCF, FileFormat.BCF):
            endpoint = "variants"
        else:
            raise InvalidInputError(
                "set file.format (bam, cram, vcf or bcf) for EGA region access",
                source="ega",
                details={"format": fmt},
            )
        if file.assembly and file.assembly != interval.assembly:
            raise InvalidInputError(
                "interval assembly does not match file.assembly; no liftover", source="ega"
            )
        ctx.check_region(interval)
        budget = min(REGION_BUDGET_BYTES, ctx.settings.limits.max_transfer_bytes)
        work = ctx.settings.paths.work_dir / "archives" / "ega-regions"
        used = (
            _workspace_usage(ctx.settings.paths.work_dir)
            if ctx.settings.paths.work_dir.exists()
            else 0
        )
        if used + budget > ctx.settings.limits.workspace_max_bytes:
            raise BudgetExceededError(
                "workspace quota would be exceeded by this region",
                source="ega",
                details={"workspace_max_bytes": ctx.settings.limits.workspace_max_bytes},
            )
        async with self._client(ctx, require_auth=True) as c:
            try:
                res = await c.get_region(
                    acc,
                    _client_interval(interval),
                    workspace=work,
                    endpoint=endpoint,
                    budget_bytes=budget,
                    max_records=0,
                    max_region_bp=ctx.limits.max_region_bp,
                    assembly_policy="reject",
                    timeout_s=self.runtime.timeout(ctx),
                )
            except SourceError as exc:
                raise to_core_error(exc) from None
        art = res.artifact
        prov = to_core(res.provenance[0]).model_dump(mode="json")
        region_meta = {
            "provider": "EGA htsget (provider-generated region)",
            "request": interval.model_dump(),
            "endpoint": endpoint,
            "output_format": res.format,
            "bytes": art.size_bytes,
            "sha256": art.checksums[0].value,
            "md5": art.checksums[1].value,
            "checksum_scope": "region artifact only; the source whole-file MD5 does not apply",
            "ticket_md5_verified": art.checksum_verified,
            "blocks": [b.model_dump() for b in res.blocks],
            "records_received": res.records_in_blocks,
            "records_overlapping": res.records_overlapping,
            "records_unplaced": res.records_skipped_unplaced,
            "header": {
                **res.header.model_dump(),
                "assembly_note": "@SQ AS tags are names, not assembly accessions/patch versions",
            },
            "provenance": prov,
        }
        native = {
            "source_uri": file.uri,
            "source_checksums": [c.model_dump() for c in file.checksums],
            "region_artifact": region_meta,
        }
        out_fmt = FileFormat.BAM if endpoint == "reads" else FileFormat.VCF
        resolved_file = file.model_copy(
            update={
                "format": out_fmt,
                "compression": Compression.BGZF,
                "index_uri": None,
                "size_bytes": art.size_bytes,
                "checksums": [
                    Checksum(algorithm="sha256", value=art.checksums[0].value),
                    Checksum(algorithm="md5", value=art.checksums[1].value),
                ],
                "native": {**file.native, **native},
            }
        )
        path = Path(art.path)
        return ResolvedFile(
            file=resolved_file,
            open_uri=str(path),
            local_path=path,
            range_capable=None,
            region=interval,
            readiness=Readiness(
                state=ReadinessState.READY,
                checked_at=utcnow(),
                reasons=[
                    f"bounded {res.format} slice from EGA htsget for the requested interval",
                    f"{res.records_in_blocks} records received, {res.records_overlapping} overlap the interval; "
                    "readers must post-filter",
                ],
            ),
        )


def _client_interval(interval: Interval):
    from genomics_mcp.archives._common.models import Interval as ClientInterval

    return ClientInterval(**interval.model_dump())


# --------------------------------------------------------------------------- transfers


class TransferDescription(BaseModel):
    """What a transfer backend reports before any bytes move."""

    model_config = ConfigDict(extra="forbid")

    file: FileRef
    size_bytes: int = Field(
        ge=0, description="Exact bytes `open()` will stream for the whole file."
    )
    checksums: list[Checksum] = Field(
        default_factory=list, description="Apply to the streamed bytes."
    )
    resumable: bool = True
    suggested_name: str
    notes: list[str] = Field(default_factory=list)


@runtime_checkable
class TransferBackend(Protocol):
    """Minimal stream interface for the core transfer manager (E3) for schemes it cannot open."""

    scheme: str

    async def describe(self, file: FileRef, ctx: OperationContext) -> TransferDescription: ...

    def open(
        self, file: FileRef, ctx: OperationContext, *, start: int = 0, end: int | None = None
    ) -> Any:
        """Async context manager yielding an async iterator of bytes [start, end)."""
        ...


class EgaTransferBackend(_EgaBase):
    scheme = "ega"

    async def describe(self, file: FileRef, ctx: OperationContext) -> TransferDescription:
        acc = ega_accession(file)
        async with self._client(ctx, require_auth=True) as c:
            try:
                info = await c.transfer_source(acc, timeout_s=self.runtime.timeout(ctx))
            except SourceError as exc:
                raise to_core_error(exc) from None
        ref = to_core(info["file"])
        assert isinstance(ref, FileRef)
        ref = ref.model_copy(update={"visibility": file.visibility})
        return TransferDescription(
            file=ref,
            size_bytes=info["plain_size_bytes"],
            checksums=[Checksum(algorithm="md5", value=info["md5"])] if info["md5"] else [],
            suggested_name=safe_name(info["name"] or f"{acc}.bin"),
            notes=[
                f"EGA stored size {info['stored_size_bytes']} bytes; {info['size_derivation']}",
                "stream is decrypted plain data (destinationFormat=plain); MD5 is the source's plain MD5",
            ],
        )

    @asynccontextmanager
    async def open(
        self, file: FileRef, ctx: OperationContext, *, start: int = 0, end: int | None = None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        acc = ega_accession(file)
        if end is None:
            end = (await self.describe(file, ctx)).size_bytes
        async with self._client(ctx, require_auth=True) as c:
            try:
                async with c.plain_stream(acc, start=start, end=end) as body:
                    yield _mapped(body)
            except SourceError as exc:
                raise to_core_error(exc) from None


async def _mapped(body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    try:
        async for chunk in body:
            yield chunk
    except SourceError as exc:
        raise to_core_error(exc) from None
