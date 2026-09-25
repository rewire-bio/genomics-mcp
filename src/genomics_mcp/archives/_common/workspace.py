"""Private artifact paths inside a caller-configured workspace.

Source-provided names (file names, contigs, accessions) are data, never path components:
`safe_name` reduces them to a conservative basename, and `artifact_path` proves the final
path stays directly inside the resolved workspace. Writes go to an exclusive, randomly named
temporary file (O_EXCL|O_NOFOLLOW, mode 0600) that is atomically renamed into place.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

from genomics_mcp.archives._common.errors import InvalidInputError

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_NAME = 120


def safe_name(name: str, *, fallback: str = "artifact") -> str:
    """A single path component derived from untrusted text; adds a hash when anything changed."""
    raw = str(name)
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    clean = _SAFE.sub("_", base).lstrip(".-")
    if not clean or clean in (".", ".."):
        clean = fallback
    if clean != raw or len(clean) > MAX_NAME:
        digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
        stem, dot, ext = clean[:MAX_NAME].rpartition(".")
        clean = (
            f"{stem}-{digest}.{ext}"
            if dot and stem and len(ext) <= 10
            else f"{clean[:MAX_NAME]}-{digest}"
        )
    return clean


def private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path.resolve()


def artifact_path(workspace: Path, name: str) -> Path:
    """Path for `name` directly inside `workspace`; refuses anything that escapes or is a symlink."""
    root = private_dir(workspace)
    component = safe_name(name)
    dest = root / component
    if dest.parent != root or component in (".", ".."):
        raise InvalidInputError("artifact path escapes the workspace", details={"name": component})
    if dest.is_symlink():
        raise InvalidInputError(
            "refusing to write through a symlink in the workspace", details={"name": component}
        )
    return dest


def open_temp(dest: Path) -> tuple[int, Path]:
    """Exclusive private temporary file next to `dest`."""
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(8)}.part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.open(tmp, flags, 0o600), tmp


def write_atomic(dest: Path, data: bytes) -> None:
    fd, tmp = open_temp(dest)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def remove_quietly(*paths: str | Path) -> None:
    """Best-effort cleanup of partial artifacts (sync; safe to call from failure paths)."""
    for p in paths:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
