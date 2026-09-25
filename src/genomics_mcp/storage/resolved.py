"""`StorageResolvedFile`: a `ResolvedFile` with what the storage layer actually observed.

It is a subclass, so every consumer of the core contract still works; readers use the extra
fields when present (e.g. the `.gzi` companion of a BGZF FASTA). Openable URIs may be signed
and are excluded from repr; report `file.display_uri()` and the `*_display` fields instead.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from genomics_mcp.contracts import ResolvedFile
from genomics_mcp.errors import redact_url
from genomics_mcp.models import Compression

Access = Literal["local", "https", "s3"]
IndexState = Literal["present", "missing", "corrupt", "not_needed", "not_checked"]


class StorageResolvedFile(ResolvedFile):
    access: Access
    size_bytes: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    compression: Compression | None = None
    content_kind: str | None = Field(default=None, description="Sniffed content kind.")
    index_kind: str | None = None
    index_state: IndexState = "not_checked"
    index_display: str | None = None
    index_problem: str | None = None
    index_explicit: bool = False
    companion_open_uris: dict[str, str] = Field(default_factory=dict, repr=False)
    """Extra files readers need, e.g. {"gzi": ...} for BGZF FASTA."""
    companion_display: dict[str, str] = Field(default_factory=dict)
    redirected: bool = False
    endpoint_trusted_host: str | None = Field(
        default=None, description="Explicitly configured host (S3 profile) readers may contact."
    )


def display(uri: str) -> str:
    return redact_url(uri)


def basename(uri: str) -> str:
    path = uri if uri.startswith("/") else urlsplit(uri).path
    return PurePosixPath(path).name
