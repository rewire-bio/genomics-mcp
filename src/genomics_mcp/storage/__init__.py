"""E2 storage provider: resolvers for local files, public HTTP(S) and S3; file listings.

Registers resolvers `file`, `http`, `https`, `s3`; `list_files` for sources `local` and `s3`;
the `storage` component (`StorageManager`) used by the transfer and reader providers.
`ftp` is not registered: HTSlib FTP access cannot be range-checked the same way; use the
HTTPS form of archive URLs (ENA and NCBI serve the same files over HTTPS).
"""

from __future__ import annotations

from genomics_mcp.context import OperationContext
from genomics_mcp.registry import Operation, Registry, SourceInfo
from genomics_mcp.requests import ListFilesRequest
from genomics_mcp.result import OperationOutput
from genomics_mcp.storage.listing import list_local, list_s3
from genomics_mcp.storage.local import LocalResolver
from genomics_mcp.storage.manager import StorageManager
from genomics_mcp.storage.remote import HttpResolver
from genomics_mcp.storage.resolved import StorageResolvedFile
from genomics_mcp.storage.s3 import S3Resolver

__all__ = ["StorageManager", "StorageResolvedFile", "register"]

COMPONENT = "storage"


class _LazyManager:
    """Creates the StorageManager on first use with the running service's settings."""

    def __init__(self) -> None:
        self.manager: StorageManager | None = None

    def get(self, ctx: OperationContext) -> StorageManager:
        if self.manager is None:
            self.manager = StorageManager(ctx.settings)
        return self.manager


def register(registry: Registry) -> None:
    lazy = _LazyManager()

    registry.register_resolver("file", LocalResolver(), provider=__name__)
    http = HttpResolver(lambda ctx: lazy.get(ctx).http)
    registry.register_resolver("http", http, provider=__name__)
    registry.register_resolver("https", http, provider=__name__)
    registry.register_resolver(
        "s3", S3Resolver(lambda ctx: (lazy.get(ctx).s3, lazy.get(ctx).http)), provider=__name__
    )
    registry.provide(COMPONENT, lazy)

    async def list_files_local(req: ListFilesRequest, ctx: OperationContext) -> OperationOutput:
        return await list_local(req, ctx)

    async def list_files_s3(req: ListFilesRequest, ctx: OperationContext) -> OperationOutput:
        return await list_s3(req, ctx, lazy.get(ctx).s3)

    registry.register(
        Operation.LIST_FILES,
        "local",
        list_files_local,
        provider=__name__,
        description="accession: absolute directory path under an allowed root",
    )
    registry.register(
        Operation.LIST_FILES,
        "s3",
        list_files_s3,
        provider=__name__,
        description="accession: s3://bucket/prefix; storage_profile for private buckets",
    )

    async def close() -> None:
        if lazy.manager is not None:
            await lazy.manager.aclose()

    registry.on_shutdown(close)

    registry.register_source(
        SourceInfo(
            "local",
            "Local files under allowed roots",
            "local",
            "E2",
            auth="none",
            notes="Paths must resolve (after symlinks) under paths.allowed_roots or the work "
            "dir. Sidecar indexes are observed, never assumed.",
        )
    )
    registry.register_source(
        SourceInfo(
            "https",
            "Public HTTP(S) byte-range files",
            "storage",
            "E2",
            auth="none",
            notes="Range support is proven with GET bytes=0-0; a 200 reply means "
            "download_required. Loopback/private hosts and plain http only when listed in "
            "storage.local_network_hosts. Cloud metadata addresses are always refused.",
        )
    )
    registry.register_source(
        SourceInfo(
            "s3",
            "Anonymous public S3 or explicitly configured S3-compatible profiles",
            "storage",
            "E2",
            auth="explicit_profile",
            notes="No ambient AWS credentials or config; requester-pays off unless a profile "
            "enables it. Objects are presigned (15 min) for readers, never passed as s3://.",
        )
    )


def manager_for(ctx: OperationContext) -> StorageManager:
    """The StorageManager for this service (other providers call this via the component)."""
    return ctx.require_component(COMPONENT).get(ctx)
