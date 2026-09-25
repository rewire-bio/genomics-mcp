"""BAM/CRAM tasks run inside the isolated reader process.

Semantics (all coordinates 0-based half-open):

- reads: records overlapping the interval with samtools view -f/-F/-q semantics.
- coverage: samtools depth semantics. Counts bases aligned by M/=/X only (never deletions or
  reference skips), excludes reads with any `exclude_flags` bit, MAPQ < min, and bases with
  quality < min. Overlapping mates are both counted (samtools depth default, no -s).
  Every read overlapping the interval is processed; if the processing cap or the soft
  deadline stops early, positions from the first unprocessed read onward are reported as
  not computed, never as zero.
- pileup: samtools mpileup -B -A semantics (no BAQ, orphans counted, overlap detection on
  unless disabled). An entry is kept when quality[qpos] >= min_base_quality, where qpos for
  a deletion or skip is the next query base, exactly as samtools does.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pysam

from genomics_mcp.errors import (
    InvalidInputError,
    PreparationRequiredError,
)
from genomics_mcp.storage.native_common import (
    assembly_status,
    cached_md5,
    check_contig,
    jsonable,
    open_error,
)

MAX_COVERAGE_INPUT_READS = 5_000_000
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
MAX_TAG_ITEMS = 64
FLAG_NAMES = [
    (0x1, "PAIRED"),
    (0x2, "PROPER_PAIR"),
    (0x4, "UNMAP"),
    (0x8, "MUNMAP"),
    (0x10, "REVERSE"),
    (0x20, "MREVERSE"),
    (0x40, "READ1"),
    (0x80, "READ2"),
    (0x100, "SECONDARY"),
    (0x200, "QCFAIL"),
    (0x400, "DUP"),
    (0x800, "SUPPLEMENTARY"),
]

pysam.set_verbosity(1)


class Opened:
    def __init__(
        self, af: pysam.AlignmentFile, info: dict[str, Any], fasta: Any, seal: str | None
    ) -> None:
        self.af = af
        self.info = info
        self.fasta = fasta
        self.seal = seal

    def close(self) -> None:
        self.af.close()
        if self.fasta is not None:
            self.fasta.close()
        if self.seal is not None:
            shutil.rmtree(self.seal, ignore_errors=True)


def _ur_path(value: str) -> str | None:
    if "://" in value and not value.startswith("file:"):
        return None  # HTSlib 1.24 refuses remote UR tags itself
    return value[5:] if value.startswith("file:") else value


def _open(p: dict[str, Any]) -> Opened:
    fmt = p["format"]
    iv = p["interval"]
    contig, start, end = iv["contig"], iv["start"], iv["end"]
    ref = p.get("reference")
    ref_path = ref["path"] if ref else None
    fasta = None
    if ref:
        try:
            fasta = pysam.FastaFile(
                ref["uri"],
                filepath_index=ref.get("index"),
                filepath_index_compressed=(ref.get("companions") or {}).get("gzi"),
            )
        except (OSError, ValueError) as exc:
            raise InvalidInputError(
                f"reference FASTA could not be opened ({str(exc)[:200]})"
            ) from None
    mode = "rc" if fmt == "cram" else "rb"
    kwargs: dict[str, Any] = {}
    if p.get("index"):
        kwargs["index_filename"] = p["index"]
    if fmt == "cram" and ref_path:
        kwargs["reference_filename"] = ref_path
    try:
        af = pysam.AlignmentFile(p["uri"], mode, **kwargs)
    except (OSError, ValueError) as exc:
        raise open_error(exc, what=fmt.upper(), has_index=bool(p.get("index"))) from None
    header = af.header.to_dict()
    sq = {s["SN"]: s for s in header.get("SQ", [])}
    lengths = {name: int(s.get("LN", 0)) for name, s in sq.items()}
    check_contig(contig, lengths, start, end)
    entry = sq[contig]
    info: dict[str, Any] = {
        "assembly": assembly_status(
            iv["assembly"], entry.get("AS"), file_asserted=bool(p.get("file_assembly"))
        ),
        "contig_length": lengths[contig],
    }
    ref_names = set(fasta.references) if fasta is not None else set()
    try:
        if fasta is not None:
            info["reference"] = _verify_reference(fasta, ref, entry, contig, lengths[contig], p)
        elif fmt == "cram":
            info["reference"] = {
                "status": "none_supplied",
                "note": "decoded without an external reference: the CRAM embeds its reference "
                "or stores bases reference-free",
            }
        if fmt == "cram":
            seal = _seal_reference_lookup(sq, ref_names, ref_path, p.get("seal_dir"))
        else:
            seal = None
    except BaseException:
        af.close()
        if fasta is not None:
            fasta.close()
        raise
    return Opened(af, info, fasta, seal)


def _verify_reference(
    fasta: Any, ref: dict[str, Any], entry: dict[str, Any], contig: str, length: int, p: dict
) -> dict[str, Any]:
    names = set(fasta.references)
    if contig not in names:
        raise InvalidInputError(
            f"reference FASTA has no contig {contig!r}; wrong reference",
            details={"reference_contigs_sample": sorted(names)[:20]},
        )
    ref_len = fasta.get_reference_length(contig)
    if ref_len != length:
        raise InvalidInputError(
            f"reference {contig} length {ref_len} differs from the header length {length}; "
            "wrong reference",
        )
    header_m5 = entry.get("M5")
    info: dict[str, Any] = {"uri": ref.get("display"), "header_m5": header_m5}
    if header_m5:
        md5 = cached_md5(fasta, ref["path"], contig, p.get("md5_cache_dir"))
        if md5.lower() != str(header_m5).lower():
            raise InvalidInputError(
                f"reference MD5 for {contig} does not match the file header (M5); wrong reference",
                hint="supply the exact reference the file was aligned/compressed against",
                details={"header_m5": header_m5, "reference_md5": md5},
            )
        info.update(status="md5_verified", reference_md5=md5)
    elif p["format"] == "cram":
        raise InvalidInputError(
            f"CRAM header has no M5 for {contig}; the reference cannot be verified"
        )
    else:
        info.update(status="unverified_no_header_m5")
    return info


def _seal_reference_lookup(
    sq: dict[str, dict[str, Any]], approved: set[str], ref_path: str | None, seal_base: str | None
) -> str | None:
    """Stop HTSlib from finding any reference the caller did not approve.

    HTSlib looks up a missing reference by header M5 in REF_CACHE, then REF_PATH, then opens
    the @SQ UR path. M5 values are used verbatim in those paths, so they must be plain hex.
    For every contig without an approved reference, an empty file named by its M5 is placed
    in a private REF_CACHE: a slice that needs that reference then fails to decode instead of
    reaching the UR fallback. Embedded-reference and reference-free slices never look it up.
    A contig with a UR tag naming an existing local file but no M5 would go straight to UR,
    so it is refused.
    """
    risky: list[str] = []
    to_seal: list[str] = []
    for name, s in sq.items():
        m5 = s.get("M5")
        if m5 is not None and not _HEX32.match(str(m5)):
            raise InvalidInputError(
                f"malformed @SQ M5 for {name}; refusing to decode",
                details={"contig": name},
            )
        if name in approved:
            continue
        if m5 is not None:
            to_seal.append(str(m5).lower())
        elif "UR" in s:
            path = _ur_path(str(s["UR"]))
            if path and os.path.isabs(path) and os.path.exists(path):
                if not (ref_path and os.path.realpath(path) == os.path.realpath(ref_path)):
                    risky.append(name)
    if risky:
        raise PreparationRequiredError(
            "the CRAM header names local reference files (@SQ UR) without an M5 checksum; "
            "HTSlib would open them automatically and header-named references are not used "
            "without approval",
            hint="pass reference= an explicit FASTA containing these contigs",
            details={"contigs": risky[:20]},
        )
    if not seal_base:
        raise PreparationRequiredError("reference isolation directory is not configured")
    Path(seal_base).mkdir(parents=True, exist_ok=True)
    seal = tempfile.mkdtemp(prefix="seal-", dir=seal_base)
    for m5 in to_seal:
        Path(seal, m5).touch()
    os.environ["REF_CACHE"] = f"{seal}/%s"
    return seal


def _records(o: Opened, p: dict[str, Any]):
    iv = p["interval"]
    contig, start, end = iv["contig"], iv["start"], iv["end"]
    try:
        if p.get("index"):
            it = o.af.fetch(contig, start, end)
        elif p.get("region_slice"):
            it = (
                r
                for r in o.af.fetch(until_eof=True)
                if r.reference_name == contig
                and r.reference_start < end
                and (r.reference_end or r.reference_start + 1) > start
            )
        else:
            raise PreparationRequiredError("no index for this file")
        yield from it
    except OSError as exc:
        raise _decode_error(p, exc) from None


def _decode_error(p: dict[str, Any], exc: Exception) -> Exception:
    if p["format"] == "cram" and not p.get("reference"):
        return PreparationRequiredError(
            "CRAM records could not be decoded without a reference (or the file is damaged)",
            hint="pass reference= a local FASTA whose MD5 matches the header @SQ M5",
            details={"cram_reference": "missing"},
        )
    return InvalidInputError(f"alignment records could not be decoded ({str(exc)[:200]})")


def _flags(flag: int) -> list[str]:
    return [name for bit, name in FLAG_NAMES if flag & bit]


def _tags(r: pysam.AlignedSegment) -> tuple[dict[str, Any], list[str]]:
    tags: dict[str, Any] = {}
    omitted: list[str] = []
    for key, value in r.get_tags():
        v = jsonable(value)
        if (isinstance(v, list) and len(v) > MAX_TAG_ITEMS) or (
            isinstance(v, str) and len(v) > 256
        ):
            omitted.append(key)
            continue
        tags[key] = v
    return tags, omitted


def _read_record(r: pysam.AlignedSegment, include_sequence: bool) -> dict[str, Any]:
    tags, omitted = _tags(r)
    rec: dict[str, Any] = {
        "name": r.query_name,
        "flag": r.flag,
        "flags": _flags(r.flag),
        "contig": r.reference_name,
        "start": r.reference_start,
        "end": r.reference_end,
        "mapq": r.mapping_quality,
        "cigar": r.cigarstring,
        "strand": "-" if r.is_reverse else "+",
        "query_length": r.query_length,
        "mate": None,
        "template_length": r.template_length,
        "tags": tags,
    }
    if omitted:
        rec["omitted_tags"] = omitted
    if r.is_paired:
        rec["mate"] = {
            "contig": r.next_reference_name,
            "start": r.next_reference_start if r.next_reference_start >= 0 else None,
            "unmapped": r.mate_is_unmapped,
            "reverse": r.mate_is_reverse,
        }
    if include_sequence:
        rec["sequence"] = r.query_sequence
        q = r.query_qualities
        rec["base_qualities"] = list(q) if q is not None else None
    return rec


def reads(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    o = _open(p)
    try:
        req, exc, mq = p["require_flags"], p["exclude_flags"], p["min_mapping_quality"]
        cap = p["max_records"]
        out: list[dict[str, Any]] = []
        truncated = False
        complete = True
        for r in _records(o, p):
            if time.monotonic() > deadline:
                complete = False
                break
            if (r.flag & req) != req or (r.flag & exc) or r.mapping_quality < mq:
                continue
            if len(out) == cap:
                truncated = True
                break
            out.append(_read_record(r, p["include_sequence"]))
        return {"records": out, "truncated": truncated, "complete": complete, **o.info}
    finally:
        o.close()


def _pass(r: pysam.AlignedSegment, exclude: int, min_mq: int) -> bool:
    return not (r.flag & exclude) and not r.is_unmapped and r.mapping_quality >= min_mq


def coverage(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    o = _open(p)
    try:
        iv = p["interval"]
        start, end = iv["start"], iv["end"]
        exclude, min_mq, min_bq = (
            p["exclude_flags"],
            p["min_mapping_quality"],
            p["min_base_quality"],
        )
        depth = np.zeros(end - start, dtype=np.int64)
        processed = 0
        stop_at: int | None = None
        stop_reason = None
        for r in _records(o, p):
            if processed >= p.get("max_input_reads", MAX_COVERAGE_INPUT_READS):
                stop_at, stop_reason = max(start, r.reference_start), "max_input_reads"
                break
            if processed % 1024 == 0 and time.monotonic() > deadline:
                stop_at, stop_reason = max(start, r.reference_start), "deadline"
                break
            processed += 1
            if not _pass(r, exclude, min_mq) or r.cigartuples is None:
                continue
            quals = r.query_qualities if min_bq > 0 else None
            qarr = np.asarray(quals, dtype=np.int16) if quals is not None else None
            rpos, qpos = r.reference_start, 0
            for op, n in r.cigartuples:
                if op in (0, 7, 8):
                    lo, hi = max(rpos, start), min(rpos + n, end)
                    if lo < hi:
                        if qarr is not None:
                            q = qarr[qpos + (lo - rpos) : qpos + (hi - rpos)]
                            depth[lo - start : hi - start] += q >= min_bq
                        else:
                            depth[lo - start : hi - start] += 1
                    rpos += n
                    qpos += n
                elif op in (1, 4):
                    qpos += n
                elif op in (2, 3):
                    rpos += n
                if rpos >= end:
                    break
        complete_end = end if stop_at is None else min(end, stop_at)
        done = depth[: complete_end - start]
        summary = {
            "positions_computed": int(done.size),
            "mean": float(done.mean()) if done.size else None,
            "min": int(done.min()) if done.size else None,
            "max": int(done.max()) if done.size else None,
            "bases_with_coverage": int((done > 0).sum()),
            "total_depth": int(done.sum()),
        }
        bin_size = p.get("bin_size")
        cap = p["max_records"]
        records: list[dict[str, Any]] = []
        if bin_size:
            total = -(-(end - start) // bin_size)
            for b in range(min(total, cap)):
                s = start + b * bin_size
                e = min(end, s + bin_size)
                full = e <= complete_end
                seg = depth[s - start : e - start]
                records.append(
                    {
                        "start": s,
                        "end": e,
                        "complete": full,
                        "mean": float(seg.mean()) if full else None,
                        "min": int(seg.min()) if full else None,
                        "max": int(seg.max()) if full else None,
                    }
                )
        else:
            total = complete_end - start
            n = min(total, cap)
            records = [{"pos": start + i, "depth": int(depth[i])} for i in range(n)]
        return {
            "records": records,
            "available": total,
            "summary": summary,
            "complete": stop_at is None,
            "complete_until": complete_end,
            "stop_reason": stop_reason,
            "reads_processed": processed,
            **o.info,
        }
    finally:
        o.close()


def pileup(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    o = _open(p)
    try:
        iv = p["interval"]
        contig, start, end = iv["contig"], iv["start"], iv["end"]
        min_bq, max_depth = p["min_base_quality"], p["max_depth"]
        ref_seq = o.fasta.fetch(contig, start, end).upper() if o.fasta is not None else None
        cap = p["max_records"]
        out: list[dict[str, Any]] = []
        truncated = False
        complete = True
        limited_positions = 0
        if not p.get("index"):
            raise PreparationRequiredError("pileup needs an indexed file")
        try:
            columns = o.af.pileup(
                contig,
                start,
                end,
                truncate=True,
                stepper="samtools",
                flag_filter=p["exclude_flags"],
                min_base_quality=0,
                min_mapping_quality=p["min_mapping_quality"],
                ignore_overlaps=p["overlap_detection"],
                ignore_orphans=False,
                compute_baq=False,
                max_depth=max_depth,
                fastafile=o.fasta,
            )
            for col in columns:
                if time.monotonic() > deadline:
                    complete = False
                    break
                if len(out) == cap:
                    truncated = True
                    break
                rec = _column(col, min_bq, max_depth)
                pos = col.reference_pos
                rec["ref_base"] = ref_seq[pos - start] if ref_seq is not None else None
                if rec["depth_limit_reached"]:
                    limited_positions += 1
                out.append(rec)
        except OSError as exc:
            raise _decode_error(p, exc) from None
        return {
            "records": out,
            "truncated": truncated,
            "complete": complete,
            "positions_at_depth_limit": limited_positions,
            **o.info,
        }
    finally:
        o.close()


def _column(col: Any, min_bq: int, max_depth: int) -> dict[str, Any]:
    bases: Counter[str] = Counter()
    fwd: Counter[str] = Counter()
    rev: Counter[str] = Counter()
    ins: Counter[str] = Counter()
    dels: Counter[int] = Counter()
    names: Counter[str] = Counter()
    kept = deletions = skips = low_q = 0
    reads_in_column = 0
    for pr in col.pileups:
        reads_in_column += 1
        b = pr.alignment
        names[b.query_name] += 1
        qpos = pr.query_position_or_next
        quals = b.query_qualities
        q = quals[qpos] if qpos is not None and quals is not None and qpos < b.query_length else 0
        if q < min_bq:
            low_q += 1
            continue
        kept += 1
        if pr.is_refskip:
            skips += 1
        elif pr.is_del:
            deletions += 1
        else:
            base = b.query_sequence[pr.query_position].upper()
            bases[base] += 1
            (rev if b.is_reverse else fwd)[base] += 1
        if pr.indel > 0 and pr.query_position is not None:
            qp = pr.query_position
            ins[b.query_sequence[qp + 1 : qp + 1 + pr.indel].upper()] += 1
        elif pr.indel < 0:
            dels[-pr.indel] += 1
    return {
        "pos": col.reference_pos,
        "depth": kept,
        "reads_in_column": reads_in_column,
        "bases": dict(bases),
        "forward": dict(fwd),
        "reverse": dict(rev),
        "deletions": deletions,
        "ref_skips": skips,
        "insertions_after": [{"sequence": s, "count": c} for s, c in sorted(ins.items())],
        "deletions_starting_after": [{"length": n, "count": c} for n, c in sorted(dels.items())],
        "low_base_quality_excluded": low_q,
        "reads_with_mate_in_column": sum(c for c in names.values() if c > 1),
        "depth_limit_reached": reads_in_column >= max_depth,
    }


NATIVE_TASKS = {"reads": reads, "coverage": coverage, "pileup": pileup}
