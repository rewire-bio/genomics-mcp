"""Cache performance suite (#50): upstream requests, bytes and timings, cache off/cold/warm.

    uv run python scripts/benchmark_cache.py --samples 5 --out cache-bench.json
    uv run python scripts/benchmark_cache.py --live --samples 3 --out live.json   # opt-in

Local mode builds small synthetic genomic files (bigWig, BAM, VCF, FASTA, BED; a few MiB in
total), serves them from a counting HTTP range server on 127.0.0.1 and runs each workload
through `GenomicsService.call` (the same path as MCP tools, native readers included):

- disabled: `[cache] enabled = false`; every sample is a full upstream read;
- cold:     a fresh server per sample, cache on;
- warm:     one server, primed with the same query once, then `samples` repeats;
- neighbour: interval B after a warm interval A on the same server, versus B on a cold one.
  This is the query a whole-result cache cannot answer: it measures header/index reuse.

Reference and archive API workloads replay recorded responses (tests/reference/fixtures and a
recorded ENCODE file object) through a counting mock transport; they measure request savings
only. `--live` adds a bounded public ENCODE bigWig region and a few public API lookups (one
request each per mode; nothing is downloaded in full).

Output: JSON (`--out`) with counts, bytes, p50/p95 and sample counts, cache metrics, result
digests and software/fixture versions, plus a short table on stdout. Timings are diagnostic.
Results never contain signed URLs, credentials or local paths.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import platform
import random
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pyBigWig
import pysam

from genomics_mcp import __version__
from genomics_mcp.config import load_settings
from genomics_mcp.service import GenomicsService

ROOT = Path(__file__).resolve().parents[1]
CONTIG, LENGTH = "chrP", 2_000_000
ASSEMBLY = "synthetic-p1"
A = (100_000, 110_000)
B = (1_500_000, 1_510_000)
OVERLAP = (105_000, 115_000)


def _support() -> Any:
    """The shared test range server (strong ETag, 206, request/byte log)."""
    name = "gm_test_support"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "tests/storage/support.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


# --------------------------------------------------------------------------- fixtures


def build_fixtures(root: Path) -> dict[str, Path]:
    """Deterministic synthetic files large enough to span many 64 KiB blocks."""
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(49)  # noqa: S311 - deterministic fixture data
    seq = "".join(rng.choice("ACGT") for _ in range(LENGTH))
    fa = root / "perf.fa"
    with fa.open("w") as f:
        f.write(f">{CONTIG}\n")
        for i in range(0, LENGTH, 60):
            f.write(seq[i : i + 60] + "\n")
    pysam.faidx(str(fa))

    bam = root / "perf.bam"
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": CONTIG, "LN": LENGTH}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as out:
        for i, pos in enumerate(range(0, LENGTH - 200, 100)):
            r = pysam.AlignedSegment(out.header)
            r.query_name, r.reference_id, r.reference_start = f"r{i}", 0, pos
            r.mapping_quality, r.cigartuples, r.flag = 20 + i % 40, [(0, 100)], 16 * (i % 2)
            r.query_sequence = seq[pos : pos + 100]
            r.query_qualities = pysam.qualitystring_to_array(
                "".join(chr(33 + 20 + (i + k) % 20) for k in range(100))
            )
            out.write(r)
    pysam.index(str(bam))

    vcf = root / "perf.vcf"
    lines = [
        "##fileformat=VCFv4.3",
        f"##contig=<ID={CONTIG},length={LENGTH},assembly={ASSEMBLY}>",
        '##INFO=<ID=DP,Number=1,Type=Integer,Description="Depth">',
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\tS3",
    ]
    gts = ("0/0", "0/1", "1/1", "./.")
    for i, pos in enumerate(range(50, LENGTH, 97)):
        ref = seq[pos - 1]
        alt = "ACGT"[("ACGT".index(ref) + 1 + i % 3) % 4]
        g = "\t".join(gts[(i + k) % 4] for k in range(3))
        lines.append(
            f"{CONTIG}\t{pos}\tv{i}\t{ref}\t{alt}\t{30 + i % 50}\tPASS\tDP={i % 90}\tGT\t{g}"
        )
    vcf.write_text("\n".join(lines) + "\n")
    vcf_gz = root / "perf.vcf.gz"
    pysam.tabix_compress(str(vcf), str(vcf_gz), force=True)
    pysam.tabix_index(str(vcf_gz), preset="vcf", force=True)

    bed = root / "perf.bed"
    bed.write_text(
        "".join(
            f"{CONTIG}\t{s}\t{s + 150}\tf{i}\t{i % 1000}\t{'+-'[i % 2]}\n"
            for i, s in enumerate(range(0, LENGTH - 150, 173))
        )
    )
    bed_gz = root / "perf.bed.gz"
    pysam.tabix_compress(str(bed), str(bed_gz), force=True)
    pysam.tabix_index(str(bed_gz), preset="bed", force=True)

    bw = root / "perf.bw"
    w = pyBigWig.open(str(bw), "w")
    w.addHeader([(CONTIG, LENGTH)], maxZooms=6)
    values = [round(rng.uniform(0, 50), 2) for _ in range(LENGTH // 10)]
    w.addEntries(CONTIG, 0, values=values, span=10, step=10)
    w.close()
    return {"bigwig": bw, "bam": bam, "vcf": vcf_gz, "fasta": fa, "bed": bed_gz}


# --------------------------------------------------------------------------- workloads


def _iv(span: tuple[int, int]) -> dict[str, Any]:
    return {"contig": CONTIG, "start": span[0], "end": span[1], "assembly": ASSEMBLY}


def file_workloads(url: Callable[[str], str]) -> list[dict[str, Any]]:
    """(name, family, operation, arguments for an interval)."""

    def f(name: str) -> dict[str, Any]:
        return {"uri": url(name), "visibility": "public"}

    small = lambda s: (s[0], s[0] + 500)  # noqa: E731 - pileup/composed windows
    return [
        {"name": "signal", "family": "signal", "op": "get_signal",
         "args": lambda s: {"file": f("perf.bw"), "interval": _iv(s), "bins": 50}},
        {"name": "reads", "family": "alignments", "op": "get_reads",
         "args": lambda s: {"file": f("perf.bam"), "interval": _iv(s)}},
        {"name": "coverage", "family": "alignments", "op": "get_coverage",
         "args": lambda s: {"file": f("perf.bam"), "interval": _iv(s), "bin_size": 100}},
        {"name": "pileup", "family": "alignments", "op": "get_pileup",
         "args": lambda s: {"file": f("perf.bam"), "interval": _iv(small(s))}},
        {"name": "variants", "family": "variants", "op": "get_variants",
         "args": lambda s: {"file": f("perf.vcf.gz"), "interval": _iv(s)}},
        {"name": "sequence", "family": "sequence", "op": "get_sequence",
         "args": lambda s: {"file": f("perf.fa"), "interval": _iv(s)}},
        {"name": "features", "family": "features", "op": "get_features",
         "args": lambda s: {"file": f("perf.bed.gz"), "interval": _iv(s)}},
        {"name": "inspect_locus", "family": "composed", "op": "inspect_locus",
         "args": lambda s: {"interval": _iv(small(s)),
                            "files": [f("perf.bam"), f("perf.vcf.gz"), f("perf.bw")]}},
    ]  # fmt: skip


def _scientific(value: Any) -> Any:
    """`data` without retrieval times (they differ between calls by design)."""
    if isinstance(value, dict):
        return {k: _scientific(v) for k, v in value.items() if k != "retrieved_at"}
    if isinstance(value, list):
        return [_scientific(v) for v in value]
    return value


def digest(result: Any) -> str:
    """Hash of the scientific payload, independent of timing and cache notes."""
    data = json.loads(json.dumps(result.data, default=str))
    body = json.dumps(_scientific(data), sort_keys=True).encode()
    return hashlib.sha256(body).hexdigest()[:16]


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    xs = sorted(values)
    p95 = xs[min(len(xs) - 1, round(0.95 * (len(xs) - 1)))]
    return {"n": len(xs), "p50_ms": round(statistics.median(xs), 2), "p95_ms": round(p95, 2)}


class Counter:
    """Upstream requests/bytes seen by the counting range server since the last `take`."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._n = self._bytes = 0

    def take(self) -> tuple[int, int]:
        n, b = len(self.server.log), sum(self.server.bytes_sent.values())
        out = (n - self._n, b - self._bytes)
        self._n, self._bytes = n, b
        return out


async def _call(svc: GenomicsService, op: str, args: dict[str, Any]) -> tuple[Any, float]:
    t = time.perf_counter()
    res = await svc.call(op, args)
    elapsed = (time.perf_counter() - t) * 1000
    if res.status.value == "error":
        raise RuntimeError(f"{op} failed: {res.error.code.value}: {res.error.message}")
    return res, elapsed


async def run_file_workloads(
    root: Path, work: Path, samples: int, only: set[str] | None = None
) -> list[dict[str, Any]]:
    support = _support()
    server = support.FixtureServer(root)
    server.httpd.handle_error = lambda *a: None  # readers close ranges early; not an error
    counter = Counter(server)

    def service(enabled: bool) -> GenomicsService:
        settings = load_settings(
            env={},
            overrides={
                "paths": {"allowed_roots": [], "work_dir": str(work)},
                "storage": {"local_network_hosts": ["127.0.0.1"]},
                "cache": {"enabled": enabled},
            },
        )
        return GenomicsService(settings)

    results = []
    try:
        for w in file_workloads(server.url):
            if only and w["name"] not in only:
                continue
            row: dict[str, Any] = {"name": w["name"], "family": w["family"], "op": w["op"]}
            digests: dict[str, str] = {}

            async def measure(svc: GenomicsService, span, mode: str, w=w, digests=digests):
                counter.take()
                res, ms = await _call(svc, w["op"], w["args"](span))
                n, b = counter.take()
                digests.setdefault(mode, digest(res))
                if digests[mode] != digest(res):
                    raise RuntimeError(f"{w['name']}: {mode} results differ between samples")
                return {"requests": n, "bytes": b, "ms": ms}

            modes: dict[str, list[dict[str, Any]]] = {"disabled": [], "cold": [], "warm": []}
            off = service(False)
            try:
                for _ in range(samples):
                    modes["disabled"].append(await measure(off, A, "disabled"))
            finally:
                await off.aclose()
            for _ in range(samples):
                cold = service(True)
                try:
                    modes["cold"].append(await measure(cold, A, "cold"))
                finally:
                    await cold.aclose()
            warm = service(True)
            try:
                await measure(warm, A, "warm")
                modes["warm"] = [await measure(warm, A, "warm") for _ in range(samples)]
                neighbour = await measure(warm, B, "neighbour_after_warm")
                overlap = await measure(warm, OVERLAP, "overlap_after_warm")
                concurrent = await asyncio.gather(
                    *[_call(warm, w["op"], w["args"](A)) for _ in range(4)]
                )
                if {digest(r) for r, _ in concurrent} != {digests["warm"]}:
                    raise RuntimeError(f"{w['name']}: concurrent warm results differ")
                metrics = warm.cache.metrics()
            finally:
                await warm.aclose()
            fresh = service(True)
            try:
                neighbour_cold = await measure(fresh, B, "neighbour_cold")
            finally:
                await fresh.aclose()
            if digests["neighbour_after_warm"] != digests["neighbour_cold"]:
                raise RuntimeError(f"{w['name']}: neighbour results differ with a warm cache")
            row["results_equal"] = len({digests[m] for m in ("disabled", "cold", "warm")}) == 1
            if not row["results_equal"]:
                raise RuntimeError(f"{w['name']}: results differ between cache modes")
            for mode, obs in modes.items():
                row[mode] = {
                    "requests": obs[0]["requests"] if len({o["requests"] for o in obs}) == 1
                    else [o["requests"] for o in obs],
                    "bytes": [o["bytes"] for o in obs],
                    **_stats([o["ms"] for o in obs]),
                }  # fmt: skip
            row["neighbour"] = {
                "after_warm": {k: neighbour[k] for k in ("requests", "bytes")},
                "cold": {k: neighbour_cold[k] for k in ("requests", "bytes")},
            }
            row["overlap_after_warm"] = {k: overlap[k] for k in ("requests", "bytes")}
            row["concurrent_warm_calls"] = 4
            row["cache_after_warm"] = {k: metrics[k] for k in ("entries", "bytes")} | {
                "counts": metrics["counts"]
            }
            results.append(row)
    finally:
        server.close()
    return results


# --------------------------------------------------------------------------- API workloads

ENCODE_FILE = {
    "@id": "/files/ENCFF792QDS/",
    "accession": "ENCFF792QDS",
    "file_format": "bigWig",
    "assembly": "GRCh38",
    "file_size": 1413106336,
    "md5sum": "6b5e27fcb966d26cca1398e65b590dfd",
    "status": "released",
    "href": "/files/ENCFF792QDS/@@download/ENCFF792QDS.bigWig",
    "dataset": "/annotations/ENCSR901HTN/",
}


def _recorded(request: httpx.Request) -> httpx.Response:
    fixtures = ROOT / "tests/reference/fixtures"
    url = str(request.url.copy_with(query=None))
    table = {
        "https://rest.genenames.org/info": "hgnc_info.json",
        "https://rest.genenames.org/fetch/symbol/FANCD1": "hgnc_empty.json",
        "https://rest.genenames.org/fetch/alias_symbol/FANCD1": "hgnc_empty.json",
        "https://rest.genenames.org/fetch/prev_symbol/FANCD1": "hgnc_prev_symbol_FANCD1.json",
        "https://rest.uniprot.org/uniprotkb/P51587.json": "uniprot_P51587.json",
    }
    if url == "https://www.encodeproject.org/files/ENCFF792QDS/":
        return httpx.Response(200, json=ENCODE_FILE)
    if url in table:
        body = (fixtures / table[url]).read_bytes()
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})
    return httpx.Response(404, json={"error": "not recorded"})


API_WORKLOADS = [
    ("encode_list_files", "archive", "list_files", {"source": "encode", "accession": "ENCFF792QDS"}),
    ("hgnc_resolve", "reference", "resolve_identifier", {"identifier": "FANCD1", "sources": ["hgnc"]}),
    ("uniprot_protein", "reference", "lookup_protein", {"protein": "P51587", "sources": ["uniprot"]}),
]  # fmt: skip


async def run_api_workloads(work: Path, samples: int) -> list[dict[str, Any]]:
    from genomics_mcp import catalogs, evidence
    from genomics_mcp.registry import Registry

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _recorded(request)

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)

    def service(enabled: bool) -> GenomicsService:
        reg = Registry()
        catalogs.register(reg, http_factory=client)
        evidence.register(reg, runtime=evidence.ReferenceRuntime(client=client()))
        settings = load_settings(
            env={}, overrides={"paths": {"work_dir": str(work)}, "cache": {"enabled": enabled}}
        )
        return GenomicsService(settings, reg, load_providers=False)

    rows = []
    for name, family, op, args in API_WORKLOADS:
        row: dict[str, Any] = {"name": name, "family": family, "op": op, "fixture": "recorded"}
        seen: set[str] = set()
        for mode in ("disabled", "cold", "warm"):
            svc = service(mode != "disabled")
            if mode == "warm":
                await _call(svc, op, args)
            counts, times = [], []
            for _ in range(samples):
                if mode == "cold":
                    await svc.aclose()
                    svc = service(True)
                before = len(requests)
                res, ms = await _call(svc, op, args)
                counts.append(len(requests) - before)
                times.append(ms)
                seen.add(digest(res))
            await svc.aclose()
            row[mode] = {"requests": counts, **_stats(times)}
        row["results_equal"] = len(seen) == 1
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- live (opt-in)

LIVE_BIGWIG = "ENCFF792QDS"  # ENCODE GRCh38 bigWig used by the published demos
LIVE_REGIONS = [(1_000_000, 1_001_000), (1_001_000, 1_002_000), (5_000_000, 5_001_000)]


async def run_live(work: Path, samples: int) -> dict[str, Any]:
    """Bounded public queries: ENCODE file lookup + bigWig regions, HGNC and UniProt lookups."""

    def service(enabled: bool) -> GenomicsService:
        settings = load_settings(
            env={}, overrides={"paths": {"work_dir": str(work)}, "cache": {"enabled": enabled}}
        )
        return GenomicsService(settings)

    out: dict[str, Any] = {"regions": [], "api": []}
    probe = service(True)
    try:
        listing, _ = await _call(
            probe, "list_files", {"source": "encode", "accession": LIVE_BIGWIG}
        )
        file = listing.data["records"][0]
    finally:
        await probe.aclose()
    file = {"uri": file["uri"], "format": "bigwig", "visibility": "public",
            "source": "encode", "accession": LIVE_BIGWIG}  # fmt: skip
    for mode in ("disabled", "cold_then_warm"):
        svc = service(mode != "disabled")
        try:
            for i, (start, end) in enumerate(LIVE_REGIONS):
                for rep in range(samples if i == 0 else 1):
                    args = {"file": file, "interval": {"contig": "chr1", "start": start,
                            "end": end, "assembly": "GRCh38"}, "bins": 10}  # fmt: skip
                    res, ms = await _call(svc, "get_signal", args)
                    out["regions"].append({
                        "mode": mode, "region": f"chr1:{start}-{end}", "repeat": rep,
                        "ms": round(ms, 1), "digest": digest(res),
                        "mean": res.data.get("summary", {}).get("value"),
                    })  # fmt: skip
            if mode != "disabled":
                out["bigwig_cache"] = svc.cache.metrics()
        finally:
            await svc.aclose()
    lookups = [
        ("resolve_identifier", {"identifier": "BRCA2", "sources": ["hgnc"]}),
        ("lookup_protein", {"protein": "P51587", "sources": ["uniprot"]}),
    ]
    svc = service(True)
    try:
        for op, args in lookups:
            for rep in range(2):
                res, ms = await _call(svc, op, args)
                prov = res.provenance[0].retrieved_at.isoformat() if res.provenance else None
                out["api"].append({"op": op, "repeat": rep, "ms": round(ms, 1),
                                   "digest": digest(res), "retrieved_at": prov,
                                   "cache_note": any(w.startswith("cache:") for w in res.warnings)})  # fmt: skip
        out["api_cache"] = svc.cache.metrics()
    finally:
        await svc.aclose()
    return out


# --------------------------------------------------------------------------- report


def environment(fixtures: dict[str, Path] | None) -> dict[str, Any]:
    env: dict[str, Any] = {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "genomics_mcp": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "pysam": pysam.__version__,
        "samtools_bundled": pysam.__samtools_version__,
        "pyBigWig": pyBigWig.__version__,
        "pyBigWig_remote": bool(pyBigWig.remote),
    }
    if fixtures:
        env["fixtures"] = {
            p.name: {"bytes": p.stat().st_size,
                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:16]}
            for p in fixtures.values()
        }  # fmt: skip
    return env


def table(report: dict[str, Any]) -> str:
    def first(v: Any) -> Any:
        return v[0] if isinstance(v, list) else v

    lines = [
        f"{'workload':<16} {'mode':<9} {'req':>5} {'bytes':>10} {'p50 ms':>8} {'p95 ms':>8} {'n':>3}"
    ]
    for row in report.get("local", []) + report.get("api", []):
        for mode in ("disabled", "cold", "warm"):
            m = row[mode]
            lines.append(
                f"{row['name']:<16} {mode:<9} {first(m['requests']):>5} "
                f"{first(m.get('bytes', ['-'])):>10} {m.get('p50_ms', '-'):>8} "
                f"{m.get('p95_ms', '-'):>8} {m['n']:>3}"
            )
        if "neighbour" in row:
            nb = row["neighbour"]
            lines.append(
                f"{row['name']:<16} {'B cold':<9} {nb['cold']['requests']:>5} "
                f"{nb['cold']['bytes']:>10}"
            )
            lines.append(
                f"{row['name']:<16} {'B warm':<9} {nb['after_warm']['requests']:>5} "
                f"{nb['after_warm']['bytes']:>10}"
            )
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="cache-bench-") as tmp:
        base = Path(tmp)
        report: dict[str, Any] = {"schema": "genomics-mcp-cache-benchmark/1"}
        fixtures = None
        if not args.live_only:
            fixtures = build_fixtures(base / "data")
            only = set(args.only.split(",")) if args.only else None
            report["local"] = await run_file_workloads(
                base / "data", base / "work", args.samples, only
            )
            report["api"] = await run_api_workloads(base / "work-api", args.samples)
        if args.live or args.live_only:
            try:
                report["live"] = await run_live(base / "work-live", args.samples)
            except Exception as exc:  # noqa: BLE001 - a live outage is recorded, not hidden
                report["live"] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        report["environment"] = environment(fixtures)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--out", type=Path, help="write the JSON report here")
    ap.add_argument("--only", help="comma-separated local workload names")
    ap.add_argument("--live", action="store_true", help="also run bounded public queries")
    ap.add_argument("--live-only", action="store_true")
    args = ap.parse_args()
    report = asyncio.run(main_async(args))
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.out:
        args.out.write_text(text + "\n")
    print(table(report))
    if "live" in report:
        print(json.dumps(report["live"], indent=2, default=str)[:4000])


if __name__ == "__main__":
    main()
