"""Shared test support for tests/storage, artifacts, readers and signal.

Loaded by each directory's conftest under the module name `gm_test_support`.
Everything here is synthetic; no network except the local fixture server.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import socket
import struct
import threading
import time
from collections.abc import Iterator
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pysam
import pysam.bcftools
import pytest

from genomics_mcp.config import Settings, load_settings
from genomics_mcp.service import GenomicsService

SAMTOOLS = shutil.which("samtools")
BCFTOOLS = shutil.which("bcftools")
TABIX = shutil.which("tabix")
# CI sets GENOMICS_MCP_REQUIRE_ORACLES=1 so the samtools/bcftools/tabix oracle comparisons can
# never be skipped silently there.
if os.environ.get("GENOMICS_MCP_REQUIRE_ORACLES") == "1":
    _missing = [
        n for n, t in (("samtools", SAMTOOLS), ("bcftools", BCFTOOLS), ("tabix", TABIX)) if not t
    ]
    if _missing:
        raise RuntimeError(f"oracle tools required but not installed: {', '.join(_missing)}")
needs_samtools = pytest.mark.skipif(SAMTOOLS is None, reason="samtools not installed")
needs_bcftools = pytest.mark.skipif(BCFTOOLS is None, reason="bcftools not installed")

# Opt-in integration settings, read at import time (the core conftest clears GENOMICS_MCP_*).
MINIO_KEY = os.environ.get("GENOMICS_MCP_TEST_S3_KEY")
MINIO_SECRET = os.environ.get("GENOMICS_MCP_TEST_S3_SECRET")
MINIO_ENDPOINT = os.environ.get("GENOMICS_MCP_TEST_S3_ENDPOINT", "http://127.0.0.1:39000")
MINIO_BUCKET = os.environ.get("GENOMICS_MCP_TEST_S3_BUCKET", "genomics-mcp-test")
REVIEW_DATA = os.environ.get("GENOMICS_MCP_TEST_FIXTURES")
REVIEW_HTTP = os.environ.get("GENOMICS_MCP_TEST_HTTP")
NETWORK = os.environ.get("GENOMICS_MCP_NETWORK_TESTS") == "1"


def envelope(result: Any) -> dict[str, Any]:
    """The JSON envelope exactly as an MCP client would receive it."""
    return json.loads(result.model_dump_json())


def make_settings(tmp_path: Path, roots: list[Path], **extra: Any) -> Settings:
    overrides: dict[str, Any] = {
        "paths": {"allowed_roots": [str(r) for r in roots], "work_dir": str(tmp_path / "work")},
        "storage": {"local_network_hosts": ["127.0.0.1"]},
    }
    env = extra.pop("env", {})
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(overrides.get(k), dict):
            overrides[k] = {**overrides[k], **v}
        else:
            overrides[k] = v
    return load_settings(env=env, overrides=overrides)


# --------------------------------------------------------------------------- HTTP fixture


class FixtureServer:
    """Local byte-range server with fault modes, for storage and transfer tests.

    Paths: /<file>                  206 for Range, strong ETag, Last-Modified
           /ignore-range/<file>     always 200 (records bytes actually sent)
           /forbidden/<file> 403, /expired/<file> 401
           /redirect/<file>         302 to /<file>
           /redirect-meta/<file>    302 to http://169.254.169.254/latest/meta-data/
           /redirect-to/<url>       302 to the percent-decoded URL
           /drop/<n>/<file>         sends at most n body bytes, then closes the connection
           /slow/<file>             4 KiB every 50 ms
           /noetag/<file>           no ETag header
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.log: list[dict[str, Any]] = []
        self.bytes_sent: dict[str, int] = {}
        self.drop_once: dict[str, int] = {}
        """path -> body bytes to send before dropping the connection, once."""
        self.ignore_range_once: set[str] = set()
        self.redirect_after: dict[str, tuple[int, str]] = {}
        """path -> (requests served normally, then 302 Location for every later request)."""
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                server.handle(self)

            def do_HEAD(self) -> None:
                server.handle(self, head=True)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def handle(self, h: BaseHTTPRequestHandler, head: bool = False) -> None:
        raw_path = urlsplit(h.path).path
        path = unquote(raw_path)
        self.log.append({"path": raw_path, "headers": dict(h.headers.items())})
        mode = ""
        for prefix in (
            "/ignore-range/",
            "/forbidden/",
            "/expired/",
            "/redirect-meta/",
            "/redirect-to/",
            "/redirect/",
            "/slow/",
            "/noetag/",
            "/drop/",
        ):
            if path.startswith(prefix):
                mode, path = prefix.strip("/"), "/" + path[len(prefix) :]
                break
        if mode == "forbidden":
            return self._simple(h, 403)
        if mode == "expired":
            return self._simple(h, 401)
        if mode == "redirect":
            return self._redirect(h, path)
        if mode == "redirect-meta":
            return self._redirect(h, "http://169.254.169.254/latest/meta-data/")
        if mode == "redirect-to":
            return self._redirect(h, unquote(raw_path[len("/redirect-to/") :]))
        if raw_path in self.redirect_after:
            n, target_url = self.redirect_after[raw_path]
            if n <= 0:
                return self._redirect(h, target_url)
            self.redirect_after[raw_path] = (n - 1, target_url)
        rng_hdr = h.headers.get("Range") or ""
        # One-shot faults target downloads (no Range) and resumes (open-ended Range) only.
        limit = self.drop_once.pop(raw_path, None) if not rng_hdr else None
        if raw_path in self.ignore_range_once and rng_hdr.endswith("-"):
            self.ignore_range_once.discard(raw_path)
            mode = "ignore-range"
        if mode == "drop":
            n, _, rest = path.lstrip("/").partition("/")
            limit, path = int(n), "/" + rest
        target = (self.root / path.lstrip("/")).resolve()
        if not target.is_file() or not target.is_relative_to(self.root.resolve()):
            return self._simple(h, 404)
        data = target.read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
        start, end, status = 0, len(data) - 1, 200
        rng = h.headers.get("Range")
        if_range = h.headers.get("If-Range")
        if rng and mode != "ignore-range" and (if_range is None or if_range == etag):
            spec = rng.removeprefix("bytes=")
            a, _, b = spec.partition("-")
            start = int(a)
            end = min(int(b), len(data) - 1) if b else len(data) - 1
            if start >= len(data):
                h.send_response(416)
                h.send_header("Content-Range", f"bytes */{len(data)}")
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            status = 206
        body = data[start : end + 1]
        h.send_response(status)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Accept-Ranges", "bytes")
        h.send_header("Last-Modified", formatdate(target.stat().st_mtime, usegmt=True))
        if mode != "noetag":
            h.send_header("ETag", etag)
        if status == 206:
            h.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        h.end_headers()
        if head:
            return
        sent = 0
        chunk = 4096 if mode == "slow" else 65536
        try:
            for i in range(0, len(body), chunk):
                piece = body[i : i + chunk]
                if limit is not None and sent + len(piece) > limit:
                    piece = piece[: limit - sent]
                    h.wfile.write(piece)
                    sent += len(piece)
                    h.wfile.flush()
                    h.close_connection = True
                    with_socket_close(h)
                    return
                h.wfile.write(piece)
                sent += len(piece)
                if mode == "slow":
                    h.wfile.flush()
                    time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.bytes_sent[raw_path] = self.bytes_sent.get(raw_path, 0) + sent

    def _simple(self, h: BaseHTTPRequestHandler, status: int) -> None:
        h.send_response(status)
        h.send_header("Content-Length", "0")
        h.end_headers()

    def _redirect(self, h: BaseHTTPRequestHandler, location: str) -> None:
        h.send_response(302)
        h.send_header("Location", location)
        h.send_header("Content-Length", "0")
        h.end_headers()


def with_socket_close(h: BaseHTTPRequestHandler) -> None:
    try:
        h.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


# --------------------------------------------------------------------------- golden data

GOLDEN_ASSEMBLY = "synthetic-g1"


def _random_seq(n: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice("ACGT") for _ in range(n))


def write_fasta(path: Path, seqs: dict[str, str], width: int = 60) -> None:
    with path.open("w") as f:
        for name, s in seqs.items():
            f.write(f">{name}\n")
            for i in range(0, len(s), width):
                f.write(s[i : i + width] + "\n")


def build_golden(root: Path) -> dict[str, Any]:
    """BAM/CRAM/FASTA/VCF/BCF/BED/GFF3/GTF/bigWig/bigBed golden files. Returns paths/metadata."""
    root.mkdir(parents=True, exist_ok=True)
    lengths = {"chrG": 3000, "chrH": 500}
    seqs = {c: _random_seq(n, 7 + i) for i, (c, n) in enumerate(lengths.items())}
    ref = root / "ref.fa"
    write_fasta(ref, seqs)
    pysam.faidx(str(ref))
    # Same names/lengths, different bases: a wrong reference that only M5 can catch.
    wrong = root / "wrong_ref.fa"
    write_fasta(wrong, {c: _random_seq(n, 99 + i) for i, (c, n) in enumerate(lengths.items())})
    pysam.faidx(str(wrong))
    md5 = {c: hashlib.md5(s.encode()).hexdigest() for c, s in seqs.items()}
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": c, "LN": n, "AS": GOLDEN_ASSEMBLY, "M5": md5[c]} for c, n in lengths.items()],
    }
    bam = root / "golden.bam"
    _write_bam(bam, header, seqs, lengths)
    pysam.index(str(bam))
    out: dict[str, Any] = {
        "root": root,
        "ref": ref,
        "wrong_ref": wrong,
        "bam": bam,
        "seqs": seqs,
        "lengths": lengths,
        "md5": md5,
    }
    # BAM without @SQ AS (assembly only caller-asserted).
    noas = root / "noas.bam"
    h2 = {**header, "SQ": [{k: v for k, v in s.items() if k != "AS"} for s in header["SQ"]]}
    _write_bam(noas, h2, seqs, lengths)
    pysam.index(str(noas))
    out["bam_noas"] = noas
    # CRAMs are written with pysam's HTSlib, so fixtures never depend on a samtools binary.
    # Like `samtools view -C -T`, the writer records @SQ UR pointing at the reference.
    crams = {
        "cram": (str(ref), []),
        "cram_embedded": (str(ref), ["embed_ref=1"]),
        "cram_noref": (None, ["no_ref=1"]),
    }
    for key, (reference, opts) in crams.items():
        path = root / f"{key}.cram"
        with pysam.AlignmentFile(str(bam)) as src:
            kwargs = {"reference_filename": reference} if reference else {}
            with pysam.AlignmentFile(
                str(path), "wc", template=src, format_options=[o.encode() for o in opts], **kwargs
            ) as dst:
                for rec in src.fetch(until_eof=True):
                    dst.write(rec)
        pysam.index(str(path))
        out[key] = path
    out.update(_write_variants(root, lengths))
    out.update(_write_features(root))
    out.update(_write_signal(root))
    return out


def _segment(
    header: pysam.AlignmentHeader,
    seqs,
    lengths,
    name,
    contig,
    pos,
    cigar,
    *,
    flag=0,
    mapq=60,
    low_q=(),
    snv=None,
    ins="",
    mate=None,
) -> pysam.AlignedSegment:
    s = seqs[contig]
    r = pysam.AlignedSegment(header)
    r.query_name, r.flag = name, flag
    r.reference_id = list(lengths).index(contig)
    r.reference_start, r.mapping_quality = pos, mapq
    parts, rp = [], pos
    for op, n in cigar:
        if op in (0, 7, 8):
            parts.append(s[rp : rp + n])
            rp += n
        elif op == 1:
            parts.append((ins or "T" * n)[:n])
        elif op == 4:
            parts.append("N" * n)
        elif op in (2, 3):
            rp += n
    qs = "".join(parts)
    for i, b in (snv or {}).items():
        qs = qs[:i] + b + qs[i + 1 :]
    r.cigartuples = cigar
    r.query_sequence = qs
    r.query_qualities = pysam.qualitystring_to_array(
        "".join("&" if i in low_q else "?" for i in range(len(qs)))
    )
    if mate is not None:
        r.next_reference_id = r.reference_id
        r.next_reference_start, r.template_length = mate
    return r


def _write_bam(path: Path, header: dict, seqs, lengths) -> None:
    with pysam.AlignmentFile(str(path), "wb", header=header) as o:
        h = o.header
        S = lambda *a, **k: _segment(h, seqs, lengths, *a, **k)  # noqa: E731
        recs = [
            S("m1", "chrG", 100, [(0, 50)]),
            S("dup", "chrG", 100, [(0, 50)], flag=1024),
            S("sec", "chrG", 102, [(0, 40)], flag=256),
            S("qcf", "chrG", 104, [(0, 40)], flag=512),
            S("lowbq", "chrG", 105, [(0, 40)], low_q=range(0, 40, 3)),
            S("lowmq", "chrG", 110, [(0, 50)], mapq=5),
            S("snv", "chrG", 112, [(0, 30)], flag=16, snv={5: "A", 6: "C"}),
            S("del", "chrG", 130, [(0, 20), (2, 3), (0, 20)]),
            S("skip", "chrG", 140, [(0, 10), (3, 100), (0, 10)]),
            S("ins", "chrG", 150, [(0, 15), (1, 2), (0, 15)], ins="GG"),
            S("soft", "chrG", 160, [(4, 5), (0, 30)]),
            S("p1", "chrG", 200, [(0, 50)], flag=99, mate=(220, 70)),
            S("orphan", "chrG", 205, [(0, 30)], flag=1 | 8),
            S("p1", "chrG", 220, [(0, 50)], flag=147, mate=(200, -70), low_q=range(0, 10)),
            S("endH", "chrH", 470, [(0, 30)]),
        ]
        # Many reads over chrG:1000-1400 for truncation/depth-cap tests.
        for i in range(300):
            recs.append(S(f"bulk{i}", "chrG", 1000 + i, [(0, 60)], mapq=30 + i % 30))
        for r in sorted(recs, key=lambda r: (r.reference_id, r.reference_start)):
            o.write(r)


VCF_BODY = """##fileformat=VCFv4.3
##reference=synthetic-g1
##contig=<ID=chrG,length=3000,assembly=synthetic-g1>
##contig=<ID=chrLong,length=600000000,assembly=synthetic-g1>
##INFO=<ID=DP,Number=1,Type=Integer,Description="Depth">
##INFO=<ID=AF,Number=A,Type=Float,Description="Allele frequency">
##INFO=<ID=SOMATIC,Number=0,Type=Flag,Description="Flag">
##INFO=<ID=END,Number=1,Type=Integer,Description="End">
##INFO=<ID=SVTYPE,Number=1,Type=String,Description="SV type">
##FILTER=<ID=LowQual,Description="Low quality">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">
##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Allele depths">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\tS3
chrG\t21\trs1;rsAlt\tA\tC,G\t50\tPASS\tDP=30;AF=0.25,0.5;SOMATIC\tGT:DP:AD\t1|2:12:0,6,6\t0/.:7:7,0,.\t0/1/2:9:3,3,3
chrG\t31\t.\tGTA\tG\t60\tPASS\tDP=20\tGT:DP\t1:9\t0/1:16\t./.:.
chrG\t41\t.\tC\tT,*\t.\tLowQual\t.\tGT\t2|1\t0|0\t1/1
chrG\t100\tsv1\tN\t<DEL>\t99\tPASS\tSVTYPE=DEL;END=250\tGT\t0/1\t0/0\t.
chrG\t3000\tlast\tT\tA\t10\t.\t.\tGT\t0/1\t0/1\t0/1
chrLong\t550000001\tfar\tA\tG\t40\tPASS\t.\tGT\t0/1\t1/1\t0/0
"""


def _write_variants(root: Path, lengths) -> dict[str, Path]:
    plain = root / "golden.vcf"
    plain.write_text(VCF_BODY.replace("\\t", "\t"))
    gz = root / "golden.vcf.gz"
    pysam.tabix_compress(str(plain), str(gz), force=True)
    pysam.tabix_index(str(gz), preset="vcf", force=True, csi=True)
    out = {"vcf_plain": plain, "vcf": gz}
    # A separate chrG-only VCF with a TBI index (TBI cannot hold positions > 2^29).
    short = root / "short.vcf"
    short.write_text(
        "".join(ln + "\n" for ln in plain.read_text().splitlines() if "chrLong" not in ln)
    )
    sgz = root / "short.vcf.gz"
    pysam.tabix_compress(str(short), str(sgz), force=True)
    pysam.tabix_index(str(sgz), preset="vcf", force=True)
    out["vcf_tbi"] = sgz
    bcf = root / "golden.bcf"
    with (
        pysam.VariantFile(str(gz)) as src,
        pysam.VariantFile(str(bcf), "wb", header=src.header) as dst,
    ):
        for rec in src:
            dst.write(rec)
    pysam.bcftools.index(str(bcf))
    out["bcf"] = bcf
    # Ordinary gzip (not BGZF).
    import gzip

    og = root / "ordinary.vcf.gz"
    og.write_bytes(gzip.compress(plain.read_bytes()))
    out["vcf_gzip"] = og
    return out


BED_BODY = "chrG\t10\t20\tfeatA\t0\t+\nchrG\t15\t60\tfeatB\t5\t-\nchrG\t100\t101\tone_base\nchrH\t0\t10\tstartH\n"
GFF_BODY = (
    "##gff-version 3\n"
    "chrG\tsrc\tgene\t11\t20\t.\t+\t.\tID=gene1;Name=G%3B1;Alias=a,b\n"
    "chrG\tsrc\texon\t16\t60\t0.5\t-\t0\tID=exon1;Parent=gene1\n"
    "chrG\tsrc\tgene\t101\t101\t.\t.\t.\tID=g2\n"
)
GTF_BODY = (
    'chrG\tsrc\tgene\t11\t20\t.\t+\t.\tgene_id "g1"; gene_name "G1";\n'
    'chrG\tsrc\texon\t16\t60\t.\t-\t.\tgene_id "g1"; transcript_id "t1"; tag "a"; tag "b";\n'
)


def _write_features(root: Path) -> dict[str, Path]:
    import gzip

    out: dict[str, Path] = {}
    for name, body, preset in (
        ("bed", BED_BODY, "bed"),
        ("gff3", GFF_BODY, "gff"),
        ("gtf", GTF_BODY, "gff"),
    ):
        plain = root / f"features.{name}"
        plain.write_text(body)
        gz = root / f"features.{name}.gz"
        pysam.tabix_compress(str(plain), str(gz), force=True)
        pysam.tabix_index(str(gz), preset=preset, force=True)
        out[name] = gz
        out[f"{name}_plain"] = plain
    og = root / "ordinary.bed.gz"
    og.write_bytes(gzip.compress(BED_BODY.encode()))
    out["bed_gzip"] = og
    # FASTA variants: soft-masked plain, BGZF with .gzi, ordinary gzip.
    fa = root / "masked.fa"
    fa.write_text(">m1\nACGTacgtNNNNACGT\nGGGG\n>m2\nTTTT\n")
    pysam.faidx(str(fa))
    out["fasta_masked"] = fa
    bgz = root / "masked.fa.gz"
    pysam.tabix_compress(str(fa), str(bgz), force=True)
    pysam.faidx(str(bgz))
    out["fasta_bgzf"] = bgz
    gzf = root / "ordinary.fa.gz"
    gzf.write_bytes(gzip.compress(fa.read_bytes()))
    out["fasta_gzip"] = gzf
    unindexed = root / "unindexed.fa"
    unindexed.write_text(fa.read_text())
    out["fasta_unindexed"] = unindexed
    return out


def _write_signal(root: Path) -> dict[str, Path]:
    import pyBigWig

    bw_path = root / "signal.bw"
    bw = pyBigWig.open(str(bw_path), "w")
    bw.addHeader([("chrG", 3000), ("chrH", 500)])
    bw.addEntries(
        ["chrG", "chrG", "chrG"], [0, 100, 150], ends=[100, 150, 160], values=[1.0, 3.0, 0.5]
    )
    bw.addEntries("chrG", 1000, values=[2.0, 4.0, 6.0, 8.0], span=1, step=1)
    bw.close()
    bb_path = root / "features.bb"
    write_bigbed(
        bb_path,
        {"chrG": 3000, "chrH": 500},
        [
            ("chrG", 10, 20, "featA\t0\t+"),
            ("chrG", 15, 60, "featB\t5\t-"),
            ("chrG", 2990, 3000, "tail\t9\t."),
            ("chrH", 0, 10, "startH\t1\t+"),
        ],
    )
    return {"bigwig": bw_path, "bigbed": bb_path}


AUTOSQL = b"""table bed6
"BED6 test schema"
    (
    string chrom;       "Reference sequence chromosome or scaffold"
    uint   chromStart;  "Start position in chromosome"
    uint   chromEnd;    "End position in chromosome"
    string name;        "Name of item"
    uint   score;       "Score from 0-1000"
    char[1] strand;     "+ or -"
    )
"""


def write_bigbed(path: Path, chroms: dict[str, int], rows: list[tuple[str, int, int, str]]) -> None:
    """Minimal uncompressed bigBed (one data block, one R-tree leaf, no zoom levels).

    Layout per the UCSC bigBed/bbiFile format; validated by reading it back with pyBigWig.
    """
    names = list(chroms)
    key_size = max(len(n) for n in names)
    rows = sorted(rows, key=lambda r: (names.index(r[0]), r[1]))
    header_size, zoom_size = 64, 0
    autosql_off = header_size + zoom_size
    autosql = AUTOSQL + b"\0"
    summary_off = autosql_off + len(autosql)
    summary = struct.pack("<Qdddd", sum(r[2] - r[1] for r in rows), 0, 0, 0, 0)
    tree_off = summary_off + len(summary)
    tree = struct.pack("<IIIIQQ", 0x78CA8C91, len(names), key_size, 8, len(names), 0)
    tree += struct.pack("<BBH", 1, 0, len(names))
    for i, n in enumerate(names):
        tree += n.encode().ljust(key_size, b"\0") + struct.pack("<II", i, chroms[n])
    data_off = tree_off + len(tree)
    block = b"".join(
        struct.pack("<III", names.index(c), s, e) + rest.encode() + b"\0" for c, s, e, rest in rows
    )
    data = struct.pack("<Q", len(rows)) + block
    block_off = data_off + 8
    index_off = data_off + len(data)
    first, last = rows[0], rows[-1]
    fci, lci = names.index(first[0]), names.index(last[0])
    max_end = max(e for c, s, e, _ in rows if names.index(c) == lci)
    rtree = struct.pack(
        "<IIQIIIIQII", 0x2468ACE0, 256, len(rows), fci, first[1], lci, max_end, index_off, 1, 0
    )
    rtree += struct.pack("<BBH", 1, 0, 1)
    rtree += struct.pack("<IIIIQQ", fci, first[1], lci, max_end, block_off, len(block))
    header = struct.pack(
        "<IHHQQQHHQQIQ",
        0x8789F2EB,
        4,
        0,
        tree_off,
        data_off,
        index_off,
        6,
        6,
        autosql_off,
        summary_off,
        0,
        0,
    )
    assert len(header) == header_size
    path.write_bytes(header + autosql + summary + tree + data + rtree)


@pytest.fixture(scope="session")
def golden(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return build_golden(tmp_path_factory.mktemp("golden"))


@pytest.fixture
def fixture_server(golden: dict[str, Any]) -> Iterator[FixtureServer]:
    srv = FixtureServer(golden["root"])
    yield srv
    srv.close()


@pytest.fixture
def gsettings(tmp_path: Path, golden: dict[str, Any]) -> Settings:
    return make_settings(tmp_path, [golden["root"]])


@pytest.fixture
async def service(gsettings: Settings) -> Any:
    svc = GenomicsService(gsettings)
    yield svc
    await svc.aclose()


def iv(contig: str, start: int, end: int, assembly: str = GOLDEN_ASSEMBLY) -> dict[str, Any]:
    return {"contig": contig, "start": start, "end": end, "assembly": assembly}
