"""Content sniffing, index compatibility and readiness rules.

Everything here is decided from bytes actually read (magic numbers) plus the declared or
suffix-inferred format. Nothing guesses that a remote index exists.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from genomics_mcp.models import Compression, FileFormat, Readiness, ReadinessState, utcnow

HEAD_BYTES = 64 * 1024
"""Bytes read from the start of a file or index for sniffing (one BGZF block at most)."""

_BIGWIG_MAGIC = struct.pack("<I", 0x888FFC26)
_BIGBED_MAGIC = struct.pack("<I", 0x8789F2EB)


@dataclass(frozen=True)
class Sniff:
    compression: Compression
    kind: str
    """bam, cram, bcf, vcf, sam, fasta, fastq, bigwig, bigbed, bai, csi, tbi, crai, gzi, fai,
    text, empty or unknown."""


def is_bgzf(head: bytes) -> bool:
    """True if `head` starts with a BGZF block (gzip member with a BC extra subfield)."""
    if len(head) < 18 or head[:4] != b"\x1f\x8b\x08\x04":
        return False
    xlen = struct.unpack("<H", head[10:12])[0]
    extra = head[12 : 12 + xlen]
    i = 0
    while i + 4 <= len(extra):
        si1, si2, slen = extra[i], extra[i + 1], struct.unpack("<H", extra[i + 2 : i + 4])[0]
        if si1 == 66 and si2 == 67 and slen == 2:
            return True
        i += 4 + slen
    return False


def _inflate(head: bytes, limit: int = 4096) -> bytes:
    try:
        return zlib.decompressobj(31).decompress(head, limit)
    except zlib.error:
        return b""


def _classify_plain(data: bytes) -> str:
    if not data:
        return "empty"
    if data.startswith(b"BAM\x01"):
        return "bam"
    if data.startswith(b"BCF\x02"):
        return "bcf"
    if data.startswith(b"BAI\x01"):
        return "bai"
    if data.startswith(b"CSI\x01"):
        return "csi"
    if data.startswith(b"TBI\x01"):
        return "tbi"
    if data.startswith(b"##fileformat=VCF"):
        return "vcf"
    if data.startswith(b">"):
        return "fasta"
    if data.startswith(b"@HD\t") or data.startswith(b"@SQ\t"):
        return "sam"
    if data.startswith(b"@"):
        return "fastq"
    if _looks_like_crai(data):
        return "crai"
    if _looks_like_fai(data):
        return "fai"
    try:
        data[:1024].decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    return "text"


def _looks_like_crai(text: bytes) -> bool:
    lines = [ln for ln in text.split(b"\n")[:4] if ln]
    if not lines:
        return False
    for ln in lines[:-1] if len(lines) > 1 else lines:
        parts = ln.split(b"\t")
        if len(parts) != 6 or not all(p.lstrip(b"-").isdigit() for p in parts):
            return False
    return True


def _looks_like_fai(text: bytes) -> bool:
    lines = [ln for ln in text.split(b"\n")[:4] if ln]
    if not lines:
        return False
    for ln in lines[:-1] if len(lines) > 1 else lines:
        parts = ln.split(b"\t")
        if len(parts) not in (5, 6) or not all(p.isdigit() for p in parts[1:]):
            return False
    return True


def sniff(head: bytes) -> Sniff:
    """Identify compression and content from the first bytes of a file."""
    if not head:
        return Sniff(Compression.NONE, "empty")
    if head.startswith(b"CRAM"):
        return Sniff(Compression.NONE, "cram")
    if head.startswith(_BIGWIG_MAGIC):
        return Sniff(Compression.NONE, "bigwig")
    if head.startswith(_BIGBED_MAGIC):
        return Sniff(Compression.NONE, "bigbed")
    if head[:2] == b"\x1f\x8b":
        compression = Compression.BGZF if is_bgzf(head) else Compression.GZIP
        inner = _inflate(head)
        return Sniff(compression, _classify_plain(inner) if inner else "unknown")
    if _looks_like_gzi(head):
        return Sniff(Compression.NONE, "gzi")
    return Sniff(Compression.NONE, _classify_plain(head))


def _looks_like_gzi(head: bytes, size: int | None = None) -> bool:
    if len(head) < 8:
        return False
    (n,) = struct.unpack("<Q", head[:8])
    expected = 8 + 16 * n
    if size is not None:
        return expected == size
    return n < (1 << 40) and (expected == len(head) or len(head) == HEAD_BYTES)


# --------------------------------------------------------------------------- expectations

_CONTENT_FOR_FORMAT: dict[FileFormat, set[str]] = {
    FileFormat.BAM: {"bam"},
    FileFormat.CRAM: {"cram"},
    FileFormat.BCF: {"bcf"},
    FileFormat.VCF: {"vcf"},
    FileFormat.SAM: {"sam"},
    FileFormat.FASTA: {"fasta"},
    FileFormat.FASTQ: {"fastq"},
    FileFormat.BIGWIG: {"bigwig"},
    FileFormat.BIGBED: {"bigbed"},
    FileFormat.BED: {"text"},
    FileFormat.GFF3: {"text"},
    FileFormat.GTF: {"text"},
}


def content_matches(fmt: FileFormat | None, s: Sniff) -> bool | None:
    """Whether sniffed content fits the declared format. None when it cannot be judged."""
    if fmt is None or fmt in (FileFormat.OTHER, FileFormat.TSV):
        return None
    if s.kind in ("unknown",):
        return None if s.compression is not Compression.NONE else False
    if s.kind == "empty":
        return False
    expected = _CONTENT_FOR_FORMAT.get(fmt)
    if expected is None:
        return None
    if fmt in (FileFormat.BED, FileFormat.GFF3, FileFormat.GTF):
        # Plain-text track formats have no magic; binary content is the only clear mismatch.
        return s.kind in ("text", "vcf", "fai", "crai")
    return s.kind in expected


def index_kinds(fmt: FileFormat | None) -> tuple[str, ...]:
    """Index file kinds that htslib/pysam accept for a data format, in preference order."""
    return (
        {
            FileFormat.BAM: ("bai", "csi"),
            FileFormat.CRAM: ("crai",),
            FileFormat.VCF: ("tbi", "csi"),
            FileFormat.BCF: ("csi",),
            FileFormat.BED: ("tbi", "csi"),
            FileFormat.GFF3: ("tbi", "csi"),
            FileFormat.GTF: ("tbi", "csi"),
            FileFormat.FASTA: ("fai",),
        }.get(fmt, ())
        if fmt
        else ()
    )


def needs_index(fmt: FileFormat | None) -> bool:
    return bool(index_kinds(fmt))


def sidecar_names(name: str, fmt: FileFormat | None) -> list[tuple[str, str]]:
    """Conventional sidecar names for `name` (a file name or key), as (name, kind).

    These are candidates to *observe*; a caller must check each one actually exists.
    """
    lower = name.lower()
    out: list[tuple[str, str]] = []
    if fmt is FileFormat.BAM:
        out = [(name + ".bai", "bai"), (name + ".csi", "csi")]
        if lower.endswith(".bam"):
            out.append((name[:-4] + ".bai", "bai"))
    elif fmt is FileFormat.CRAM:
        out = [(name + ".crai", "crai")]
        if lower.endswith(".cram"):
            out.append((name[:-5] + ".crai", "crai"))
    elif fmt in (FileFormat.VCF, FileFormat.BED, FileFormat.GFF3, FileFormat.GTF):
        out = [(name + ".tbi", "tbi"), (name + ".csi", "csi")]
    elif fmt is FileFormat.BCF:
        out = [(name + ".csi", "csi")]
    elif fmt is FileFormat.FASTA:
        out = [(name + ".fai", "fai")]
    return out


def gzi_name(name: str) -> str:
    return name + ".gzi"


class IndexCheck:
    """Result of validating an index's first bytes against the data format."""

    def __init__(self, kind: str | None, ok: bool, problem: str | None = None) -> None:
        self.kind = kind
        self.ok = ok
        self.problem = problem


def check_index(fmt: FileFormat | None, head: bytes, *, size: int | None = None) -> IndexCheck:
    """Check that index bytes are a kind htslib accepts for `fmt`."""
    accepted = index_kinds(fmt)
    s = sniff(head)
    kind = s.kind
    if kind == "gzi" or (kind not in accepted and _looks_like_gzi(head, size)):
        kind = "gzi"
    if kind in accepted:
        if kind in ("tbi", "csi") and s.compression is not Compression.BGZF:
            return IndexCheck(kind, False, f"{kind} index is not BGZF-compressed")
        if kind == "tbi":
            problem = _tbi_preset_problem(fmt, _inflate(head, 64))
            if problem:
                return IndexCheck(kind, False, problem)
        if kind in ("fai", "bai", "gzi") and s.compression is not Compression.NONE:
            return IndexCheck(kind, False, f"{kind} index must not be compressed")
        if kind == "crai" and s.compression is Compression.NONE:
            return IndexCheck(kind, False, "crai index is not gzip-compressed")
        return IndexCheck(kind, True)
    wanted = "/".join(accepted) or "no index"
    return IndexCheck(
        kind,
        False,
        f"index content is {kind}, but {fmt.value if fmt else 'this file'} needs {wanted}",
    )


def _tbi_preset_problem(fmt: FileFormat | None, header: bytes) -> str | None:
    """A TBI records the column layout it was built for; it must fit the data format."""
    if len(header) < 36 or fmt is None:
        return None
    _n_ref, preset, col_seq, col_beg, col_end = struct.unpack("<iiiii", header[4:24])
    kind, zero_based = preset & 0xFFFF, bool(preset & 0x10000)
    if fmt is FileFormat.VCF and kind != 2:
        return "tabix index was not built with the VCF preset"
    if fmt is FileFormat.BED and (kind != 0 or not zero_based or (col_seq, col_beg) != (1, 2)):
        return "tabix index was not built with the BED preset"
    if fmt in (FileFormat.GFF3, FileFormat.GTF) and (
        kind != 0 or zero_based or (col_seq, col_beg, col_end) != (1, 4, 5)
    ):
        return "tabix index was not built with the GFF preset"
    return None


# --------------------------------------------------------------------------- readiness


def readiness(
    fmt: FileFormat | None,
    compression: Compression | None,
    *,
    index_state: str,
    range_capable: bool | None,
    local: bool,
    extra_reasons: list[str] | None = None,
) -> Readiness:
    """Readiness for region queries.

    index_state: present, missing, corrupt, not_needed, not_checked.
    """
    reasons = list(extra_reasons or [])
    state = ReadinessState.READY
    if fmt is None or fmt in (FileFormat.OTHER, FileFormat.TSV):
        return Readiness(
            state=ReadinessState.UNSUPPORTED,
            reasons=[*reasons, "format is not queryable by locus"],
            checked_at=utcnow(),
        )
    if fmt is FileFormat.FASTQ:
        return Readiness(
            state=ReadinessState.NOT_LOCUS_READY,
            reasons=[*reasons, "FASTQ is downloadable but not queryable by locus"],
            checked_at=utcnow(),
        )
    if fmt is FileFormat.SAM:
        return Readiness(
            state=ReadinessState.NOT_LOCUS_READY,
            reasons=[*reasons, "uncompressed SAM has no region index; convert to BAM"],
            checked_at=utcnow(),
        )
    tabix_like = fmt in (FileFormat.VCF, FileFormat.BED, FileFormat.GFF3, FileFormat.GTF)
    if tabix_like and compression in (Compression.NONE, Compression.GZIP):
        what = "plain text" if compression is Compression.NONE else "ordinary gzip (not BGZF)"
        return Readiness(
            state=ReadinessState.NOT_LOCUS_READY,
            reasons=[
                *reasons,
                f"{what} cannot be indexed in place; fetch_file with prepare=true builds a "
                "BGZF copy with a tabix index in the work dir",
            ],
            checked_at=utcnow(),
        )
    if fmt is FileFormat.FASTA and compression is Compression.GZIP:
        return Readiness(
            state=ReadinessState.NOT_LOCUS_READY,
            reasons=[
                *reasons,
                "ordinary gzip FASTA cannot be indexed; fetch_file with prepare=true "
                "recompresses a copy to BGZF",
            ],
            checked_at=utcnow(),
        )
    if not local and range_capable is False:
        state = ReadinessState.DOWNLOAD_REQUIRED
        reasons.append("server ignored the byte-range request; download with fetch_file")
    elif not local and range_capable is None:
        state = ReadinessState.UNKNOWN
        reasons.append("byte-range support not yet verified")
    if needs_index(fmt):
        if index_state == "missing":
            if state is ReadinessState.READY:
                state = ReadinessState.INDEX_REQUIRED
            reasons.append("no index found; fetch_file with prepare=true can build one")
        elif index_state == "corrupt":
            if state is ReadinessState.READY:
                state = ReadinessState.INDEX_REQUIRED
            reasons.append("index present but not valid for this file")
        elif index_state == "not_checked" and state is ReadinessState.READY:
            state = ReadinessState.UNKNOWN
            reasons.append("index presence not checked")
    if fmt is FileFormat.CRAM and state is ReadinessState.READY:
        reasons.append(
            "CRAM decoding needs an explicit reference whose MD5 matches the header, "
            "unless the CRAM embeds its reference or stores bases reference-free"
        )
    return Readiness(state=state, reasons=reasons, checked_at=utcnow())
