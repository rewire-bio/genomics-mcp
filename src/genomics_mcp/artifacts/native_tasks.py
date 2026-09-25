"""FASTA, tabix and preparation tasks run inside the isolated reader process.

Coordinates returned are 0-based half-open. BED is already 0-based half-open; GFF3/GTF are
1-based closed and are converted (start - 1, end). The file's own columns and attributes
are kept verbatim next to the parsed values.
"""

from __future__ import annotations

import errno
import gzip
import resource
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pysam
import pysam.bcftools

from genomics_mcp.errors import (
    BudgetExceededError,
    InvalidInputError,
    NotFoundError,
    PreparationRequiredError,
)
from genomics_mcp.storage.native_common import check_contig

pysam.set_verbosity(1)

BED_FIELDS = [
    "chrom",
    "chromStart",
    "chromEnd",
    "name",
    "score",
    "strand",
    "thickStart",
    "thickEnd",
    "itemRgb",
    "blockCount",
    "blockSizes",
    "blockStarts",
]


def sequence(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    iv = p["interval"]
    contig, start, end = iv["contig"], iv["start"], iv["end"]
    try:
        fa = pysam.FastaFile(
            p["uri"],
            filepath_index=p.get("index"),
            filepath_index_compressed=(p.get("companions") or {}).get("gzi"),
        )
    except (OSError, ValueError) as exc:
        raise InvalidInputError(f"FASTA could not be opened ({str(exc)[:200]})") from None
    try:
        lengths = dict(zip(fa.references, fa.lengths, strict=True))
        length = check_contig(contig, lengths, start, end)
        seq = fa.fetch(contig, start, end)
        if len(seq) != end - start:
            raise InvalidInputError(
                "FASTA returned fewer bases than requested; the index may not match the file"
            )
        return {"sequence": seq, "contig_length": length}
    finally:
        fa.close()


def _gff_attributes(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in text.strip().strip(";").split(";"):
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        out.setdefault(unquote(key.strip()), []).extend(unquote(v) for v in value.split(","))
    return out


def _gtf_attributes(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in text.strip().split(";"):
        part = part.strip()
        if not part:
            continue
        key, _, value = part.partition(" ")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        out.setdefault(key, []).append(value)
    return out


def _parse(fmt: str, line: str) -> dict[str, Any]:
    cols = line.rstrip("\n").split("\t")
    if fmt == "bed":
        if len(cols) < 3:
            raise InvalidInputError("BED line has fewer than 3 columns")
        named = {
            (BED_FIELDS[i] if i < len(BED_FIELDS) else f"column_{i + 1}"): v
            for i, v in enumerate(cols)
        }
        return {
            "contig": cols[0],
            "start": int(cols[1]),
            "end": int(cols[2]),
            "name": cols[3] if len(cols) > 3 else None,
            "strand": cols[5] if len(cols) > 5 else None,
            "type": None,
            "fields": named,
            "native_line": line.rstrip("\n"),
        }
    if len(cols) != 9:
        raise InvalidInputError(f"{fmt.upper()} line does not have 9 columns")
    attrs = _gff_attributes(cols[8]) if fmt == "gff3" else _gtf_attributes(cols[8])
    return {
        "contig": cols[0],
        "start": int(cols[3]) - 1,
        "end": int(cols[4]),
        "native_start": int(cols[3]),
        "native_end": int(cols[4]),
        "source": cols[1],
        "type": cols[2],
        "score": None if cols[5] == "." else cols[5],
        "strand": None if cols[6] == "." else cols[6],
        "phase": None if cols[7] == "." else cols[7],
        "attributes": attrs,
        "native_attributes": cols[8],
        "native_line": line.rstrip("\n"),
    }


def features(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    iv = p["interval"]
    contig, start, end = iv["contig"], iv["start"], iv["end"]
    fmt = p["format"]
    types = set(p.get("feature_types") or [])
    try:
        tbx = pysam.TabixFile(p["uri"], index=p.get("index"))
    except (OSError, ValueError) as exc:
        raise InvalidInputError(f"indexed file could not be opened ({str(exc)[:200]})") from None
    try:
        contigs = list(tbx.contigs)
        if contig not in contigs:
            raise NotFoundError(
                f"contig {contig!r} has no records in this indexed file",
                hint="contig names must match exactly (no chr renaming); a tabix index lists "
                "only contigs that have records",
                details={
                    "indexed_contigs_sample": contigs[:20],
                    "indexed_contig_count": len(contigs),
                },
            )
        cap = p["max_records"]
        out: list[dict[str, Any]] = []
        truncated = False
        complete = True
        try:
            for line in tbx.fetch(contig, start, end):
                if time.monotonic() > deadline:
                    complete = False
                    break
                rec = _parse(fmt, line)
                if types and rec.get("type") not in types:
                    continue
                if len(out) == cap:
                    truncated = True
                    break
                out.append(rec)
        except (OSError, ValueError) as exc:
            raise InvalidInputError(f"records could not be read ({str(exc)[:200]})") from None
        return {"records": out, "truncated": truncated, "complete": complete}
    finally:
        tbx.close()


# --------------------------------------------------------------------------- preparation

TABIX_PRESET = {"vcf": "vcf", "bed": "bed", "gff3": "gff", "gtf": "gff"}


def _bgzip(src: Path, *, source_gzip: bool) -> Path:
    """Write a BGZF copy named `<base>.gz` next to `src`, then remove `src`."""
    base = src.name.removesuffix(".gz").removesuffix(".bgz")
    tmp = src.with_name(base + ".gz.tmp")
    opener = gzip.open if source_gzip else open
    with opener(src, "rb") as fin, pysam.BGZFile(str(tmp), "wb") as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
    src.unlink()
    final = src.with_name(base + ".gz")
    tmp.rename(final)
    return final


def prepare(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    """Build indexes for a local copy in the work dir. Never touches source files.

    params: path (the copy), format, compression (none|gzip|bgzf), work_dir (guard).
    Returns the final path, index path(s) and the steps performed.
    """
    path = Path(p["path"])
    work = Path(p["work_dir"]).resolve()
    if not path.resolve().is_relative_to(work):
        raise InvalidInputError("preparation only runs on files inside the work dir")
    fmt, comp = p["format"], p["compression"]
    _limit_writes(p.get("max_file_bytes"))
    baseline = _dir_bytes(path.parent)
    max_growth = p.get("max_growth_bytes")

    def check_growth(step: str) -> None:
        if max_growth is not None and _dir_bytes(path.parent) - baseline > max_growth:
            raise BudgetExceededError(
                f"preparation outputs exceeded the allowed {max_growth} bytes ({step})",
                hint="pass a larger budget_bytes or free space in the work dir",
            )

    steps: list[str] = []
    index: str | None = None
    companions: dict[str, str] = {}
    try:
        if fmt == "fasta":
            if comp == "gzip":
                path = _bgzip(path, source_gzip=True)
                steps.append("recompressed ordinary gzip to BGZF")
                check_growth("BGZF recompression")
            pysam.faidx(str(path))
            check_growth("FASTA index")
            index = str(path) + ".fai"
            steps.append("built .fai with samtools faidx")
            if Path(str(path) + ".gzi").exists():
                companions["gzi"] = str(path) + ".gzi"
                steps.append("built .gzi")
        elif fmt in TABIX_PRESET:
            if comp in ("none", "gzip"):
                path = _bgzip(path, source_gzip=comp == "gzip")
                steps.append(
                    "compressed plain text to BGZF"
                    if comp == "none"
                    else "recompressed ordinary gzip to BGZF"
                )
                check_growth("BGZF compression")
            try:
                pysam.tabix_index(
                    str(path), preset=TABIX_PRESET[fmt], force=True, keep_original=True
                )
                index = str(path) + ".tbi"
                steps.append("built tabix .tbi index")
            except (OSError, ValueError) as exc:
                if "sort" in str(exc).lower() or "unsorted" in str(exc).lower():
                    raise PreparationRequiredError(
                        "the file is not sorted by position, so it cannot be indexed",
                        hint="sort it (e.g. sort -k1,1 -k2,2n for BED) and fetch again; "
                        "this server does not reorder records",
                    ) from None
                pysam.tabix_index(
                    str(path), preset=TABIX_PRESET[fmt], force=True, keep_original=True, csi=True
                )
                index = str(path) + ".csi"
                steps.append("built tabix .csi index (long contigs)")
        elif fmt == "bam":
            pysam.index(str(path))
            index = str(path) + ".bai"
            steps.append("built .bai with samtools index")
        elif fmt == "bcf":
            pysam.bcftools.index(str(path))
            index = str(path) + ".csi"
            steps.append("built .csi with bcftools index")
        else:
            return {
                "path": str(path),
                "index": None,
                "steps": [],
                "prepared": False,
                "reason": f"no preparation is defined for {fmt}",
            }
    except OSError as exc:
        if exc.errno == errno.EFBIG:
            raise BudgetExceededError(
                "preparation output reached the per-file write limit (work dir quota)"
            ) from None
        raise PreparationRequiredError(f"preparation failed: {str(exc)[:300]}") from None
    except pysam.utils.SamtoolsError as exc:
        cap = p.get("max_file_bytes")
        if "File too large" in str(exc) or (
            cap and any(f.stat().st_size >= cap for f in path.parent.iterdir() if f.is_file())
        ):
            raise BudgetExceededError(
                "preparation output reached the per-file write limit (work dir quota)"
            ) from None
        raise PreparationRequiredError(
            f"indexing failed: {str(exc)[:300]}",
            hint="BAM must be coordinate-sorted; FASTA lines must have consistent lengths",
        ) from None
    check_growth("indexing")
    return {
        "path": str(path),
        "index": index,
        "companions": companions,
        "steps": steps,
        "prepared": True,
    }


def _limit_writes(max_file_bytes: int | None) -> None:
    """Cap the size of any file this worker writes (EFBIG instead of overrunning the quota)."""
    if not max_file_bytes or threading.current_thread() is not threading.main_thread():
        return  # only in the isolated worker process; growth checks still apply otherwise
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.iterdir() if f.is_file())


NATIVE_TASKS = {"sequence": sequence, "features": features, "prepare": prepare}
