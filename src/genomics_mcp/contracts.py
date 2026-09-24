"""Interfaces between epics that are not MCP operations.

`FileResolver` turns a `FileRef` into something a reader library can open. E2
registers resolvers for file/http(s)/s3/ftp; E6 may register ega/htsget. Readers
(E3-E5) call `ctx.resolve_file(file)` and never build storage clients themselves.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from genomics_mcp.models import FileRef, Readiness

if TYPE_CHECKING:
    from genomics_mcp.context import OperationContext


class ResolvedFile(BaseModel):
    """An openable location for a file and its index.

    `open_uri` / `index_open_uri` may carry signed query strings. They must not be
    logged or returned to callers; report `file.display_uri()` instead.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    file: FileRef
    open_uri: str = Field(repr=False, description="Path or URL passed to pysam/pyBigWig.")
    index_open_uri: str | None = Field(default=None, repr=False)
    reference_open_uri: str | None = Field(default=None, repr=False)
    local_path: Path | None = None
    range_capable: bool | None = Field(
        default=None, description="True only when byte-range support was verified."
    )
    readiness: Readiness = Field(default_factory=Readiness)
    expires_at: datetime | None = Field(default=None, description="When signed URLs expire.")


@runtime_checkable
class FileResolver(Protocol):
    async def resolve(self, file: FileRef, ctx: OperationContext) -> ResolvedFile:
        """Return an openable location or raise a GenomicsError (never guess indexes/references)."""
        ...

    async def stat(self, file: FileRef, ctx: OperationContext) -> FileRef:
        """Return `file` enriched with size, checksums and readiness observed from storage."""
        ...
