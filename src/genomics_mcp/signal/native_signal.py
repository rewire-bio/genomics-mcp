"""bigWig/bigBed tasks run inside the isolated reader process (pyBigWig/libBigWig).

- Summaries use `exact=True` (computed from the full-resolution data, not zoom levels).
- Bins follow libBigWig's integer edges: bin i covers
  [start + i*L//n, start + (i+1)*L//n) with L = end - start.
- Missing data is JSON null, never NaN.
- bigBed rows keep their source-native columns, named from the file's autoSql schema.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import pyBigWig

from genomics_mcp.errors import InvalidInputError, UnsupportedError
from genomics_mcp.storage.native_common import check_contig

_AUTOSQL_FIELD = re.compile(r"^\s*([A-Za-z_][\w\[\]]*(?:\s+[A-Za-z_]\w*)?)\s+([A-Za-z_]\w*)\s*;")


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _open(p: dict[str, Any], kind: str) -> Any:
    if not p.get("local") and not pyBigWig.remote:
        raise UnsupportedError("this pyBigWig build cannot read remote files")
    try:
        f = pyBigWig.open(p["uri"])
    except (RuntimeError, OSError) as exc:
        raise InvalidInputError(f"{kind} could not be opened ({str(exc)[:200]})") from None
    if f is None:
        raise InvalidInputError(f"{kind} could not be opened")
    ok = f.isBigWig() if kind == "bigWig" else f.isBigBed()
    if not ok:
        f.close()
        raise InvalidInputError(f"file is not a {kind}")
    return f


def bin_edges(start: int, end: int, n: int) -> list[tuple[int, int]]:
    length = end - start
    return [(start + i * length // n, start + (i + 1) * length // n) for i in range(n)]


def bigwig(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    iv = p["interval"]
    contig, start, end = iv["contig"], iv["start"], iv["end"]
    f = _open(p, "bigWig")
    try:
        chroms = f.chroms()
        check_contig(contig, chroms, start, end)
        stat = p["summary"]
        header = f.header()
        out: dict[str, Any] = {
            "file_header": {
                "version": header.get("version"),
                "zoom_levels": header.get("nLevels"),
                "bases_covered": header.get("nBasesCovered"),
            },
            "contig_length": chroms[contig],
        }
        whole = f.stats(contig, start, end, type=stat, exact=True)
        out["summary"] = {"type": stat, "value": _clean(whole[0]), "exact": True}
        cap = p["max_records"]
        if p.get("bins"):
            n = p["bins"]
            values = f.stats(contig, start, end, type=stat, nBins=n, exact=True)
            out["records"] = [
                {"start": s, "end": e, "value": _clean(v)}
                for (s, e), v in zip(bin_edges(start, end, n), values, strict=True)
            ]
            out["mode"] = "bins"
            out["available"] = n
        else:
            intervals = f.intervals(contig, start, end) or ()
            out["mode"] = "intervals"
            out["available"] = len(intervals)
            # Stored intervals can extend past the query; the value is constant over an
            # interval, so clipping to the query is exact.
            out["records"] = [
                {"start": max(s, start), "end": min(e, end), "value": _clean(v)}
                for s, e, v in intervals[:cap]
            ]
        out["complete"] = time.monotonic() <= deadline
        return out
    finally:
        f.close()


def parse_autosql(text: str | None) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    if not text:
        return fields
    body = text.split("(", 1)[1] if "(" in text else text
    for line in body.splitlines():
        m = _AUTOSQL_FIELD.match(line)
        if m:
            desc = line.split('"')[1] if line.count('"') >= 2 else ""
            fields.append({"type": m.group(1), "name": m.group(2), "description": desc})
    return fields


def bigbed(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    iv = p["interval"]
    contig, start, end = iv["contig"], iv["start"], iv["end"]
    f = _open(p, "bigBed")
    try:
        chroms = f.chroms()
        check_contig(contig, chroms, start, end)
        schema = parse_autosql(f.SQL().decode() if isinstance(f.SQL(), bytes) else f.SQL())
        extra_names = [fld["name"] for fld in schema[3:]]
        entries = f.entries(contig, start, end) or []
        cap = p["max_records"]
        records = []
        for s, e, rest in entries[:cap]:
            cols = rest.split("\t") if rest else []
            named = {
                (extra_names[i] if i < len(extra_names) else f"column_{i + 4}"): v
                for i, v in enumerate(cols)
            }
            records.append({"contig": contig, "start": s, "end": e, "fields": named, "raw": rest})
        return {
            "records": records,
            "available": len(entries),
            "schema": schema,
            "contig_length": chroms[contig],
            "complete": time.monotonic() <= deadline,
        }
    finally:
        f.close()


NATIVE_TASKS = {"bigwig": bigwig, "bigbed": bigbed}
