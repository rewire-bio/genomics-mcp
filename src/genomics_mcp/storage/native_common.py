"""Helpers used inside the isolated reader process (see storage/native.py)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from genomics_mcp.errors import InvalidInputError, NotFoundError, PreparationRequiredError

_M5_STRIP = re.compile(rb"[^\x21-\x7e]")
_CHUNK = 4_000_000


def check_contig(
    name: str, lengths: dict[str, int], start: int, end: int, *, what: str = "file"
) -> int:
    """Require the exact contig and an interval inside it. No renaming (chr1 vs 1)."""
    if name not in lengths:
        sample = sorted(lengths)[:20]
        raise NotFoundError(
            f"contig {name!r} is not in the {what}",
            hint="contig names must match exactly; no chr-prefix renaming or liftover is done",
            details={"available_contigs_sample": sample, "contig_count": len(lengths)},
        )
    length = lengths[name]
    if length is not None and end > length:
        raise InvalidInputError(
            f"interval end {end} is beyond the end of {name} (length {length})",
            hint="0-based half-open: end may be at most the contig length",
            details={"contig": name, "contig_length": length, "start": start, "end": end},
        )
    return length


def assembly_status(requested: str, declared: str | None, *, file_asserted: bool) -> dict:
    """Distinguish caller-asserted from file-declared assemblies. Mismatch is an error."""
    if declared is not None and declared != requested:
        raise InvalidInputError(
            f"the file header declares assembly {declared!r}, not {requested!r}",
            hint="no liftover is performed; query with the file's assembly",
            details={"file_declared": declared, "requested": requested},
        )
    if declared is not None:
        status = "file_header_declared"
    elif file_asserted:
        status = "file_metadata_asserted"
    else:
        status = "caller_asserted"
    return {"requested": requested, "file_declared": declared, "status": status}


def sequence_md5(fasta: Any, contig: str) -> str:
    """SAM-spec M5: MD5 of the uppercase sequence with bytes outside 33..126 removed."""
    h = hashlib.md5(usedforsecurity=False)
    length = fasta.get_reference_length(contig)
    for pos in range(0, length, _CHUNK):
        chunk = fasta.fetch(contig, pos, min(length, pos + _CHUNK)).encode().upper()
        h.update(_M5_STRIP.sub(b"", chunk))
    return h.hexdigest()


def cached_md5(fasta: Any, ref_path: str | None, contig: str, cache_dir: str | None) -> str:
    """M5 of a local reference contig, cached by (path, size, mtime) in the work dir."""
    if not ref_path or not cache_dir:
        return sequence_md5(fasta, contig)
    st = os.stat(ref_path)
    key = hashlib.sha256(
        f"{os.path.realpath(ref_path)}|{st.st_size}|{st.st_mtime_ns}|{contig}".encode()
    ).hexdigest()
    path = Path(cache_dir) / f"{key}.json"
    try:
        return json.loads(path.read_text())["md5"]
    except (OSError, ValueError, KeyError):
        pass
    md5 = sequence_md5(fasta, contig)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"md5": md5}))
        os.replace(tmp, path)
    except OSError:
        pass
    return md5


def jsonable(value: Any) -> Any:
    """Convert pysam values to JSON: tuples to lists, NaN/inf to None, bytes to str."""
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "tolist"):
        return jsonable(value.tolist())
    return value


def open_error(exc: Exception, *, what: str, has_index: bool) -> Exception:
    """Map an HTSlib open/index failure to a typed error."""
    text = str(exc)
    low = text.lower()
    if "index" in low and has_index:
        return InvalidInputError(
            f"{what}: the index could not be loaded ({text[:200]})",
            hint="the index may be corrupt or belong to another file; rebuild it or pass the "
            "matching index_uri",
        )
    if "index" in low:
        return PreparationRequiredError(
            f"{what}: no usable index ({text[:200]})",
            hint="pass file.index_uri or fetch_file with prepare=true",
        )
    return InvalidInputError(f"{what}: file could not be opened ({text[:200]})")
