"""Test doubles for E3-E5 handlers, used only when the real providers are not installed.

They follow the record contracts of the E3-E5 handlers (field names and error types) closely
enough to exercise composition, and read the same real synthetic files with pysam/pyBigWig.
They are not acceptance evidence for the readers; `real_providers()` switches to the real
registered providers whenever `genomics_mcp.readers` is importable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pysam

from genomics_mcp.context import OperationContext
from genomics_mcp.errors import (
    InvalidInputError,
    NotFoundError,
    PreparationRequiredError,
    UnsupportedError,
)
from genomics_mcp.models import Provenance
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import OperationOutput, Truncation

REAL_MODULES = {
    "genomics_mcp.storage": "E2",
    "genomics_mcp.artifacts": "E3",
    "genomics_mcp.readers": "E4",
    "genomics_mcp.signal": "E5",
}


def real_providers() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in REAL_MODULES)


def _local(ctx: OperationContext, file: Any, index_suffixes: tuple[str, ...]) -> tuple[Path, str]:
    if not file.is_local:
        raise UnsupportedError("test double reads local files only")
    path = ctx.resolve_local_path(file.uri)
    if file.index_uri:
        return path, str(ctx.resolve_local_path(file.index_uri))
    for suffix in index_suffixes:
        if Path(f"{path}{suffix}").exists():
            return path, f"{path}{suffix}"
    raise PreparationRequiredError("no index for this file", hint="pass file.index_uri")


def _assembly(req: Any, declared: str | None = None) -> dict[str, Any]:
    status = "file_metadata_asserted" if req.file.assembly else "caller_asserted"
    return {"requested": req.interval.assembly, "file_declared": declared, "status": status}


def _prov(req: Any, method: str) -> list[Provenance]:
    return [Provenance(source="local", url=req.file.display_uri(), method=f"double {method}")]


def _contig(names: list[str], lengths: list[int], contig: str) -> int:
    if contig not in names:
        raise NotFoundError(f"contig {contig!r} is not in the file")
    return lengths[names.index(contig)]


async def reads(req, ctx):
    if not req.file.is_local:
        await ctx.resolve_file(req.file, interval=req.interval)
    path, index = _local(ctx, req.file, (".bai",))
    iv, cap = req.interval, ctx.limits.max_records

    def run():
        with pysam.AlignmentFile(str(path), "rb", index_filename=index) as af:
            length = _contig(list(af.references), list(af.lengths), iv.contig)
            out, trunc = [], False
            for r in af.fetch(iv.contig, iv.start, iv.end):
                if len(out) == cap:
                    trunc = True
                    break
                rec = {
                    "name": r.query_name,
                    "flag": r.flag,
                    "contig": r.reference_name,
                    "start": r.reference_start,
                    "end": r.reference_end,
                    "mapq": r.mapping_quality,
                    "cigar": r.cigarstring,
                }
                if req.include_sequence:
                    rec["sequence"] = r.query_sequence
                out.append(rec)
            return out, trunc, length

    records, trunc, length = await ctx.run_blocking(run)
    return OperationOutput(
        data={
            "records": records,
            "interval": iv,
            "assembly": _assembly(req),
            "contig_length": length,
        },
        truncation=Truncation(reason="max_records", limit=cap, returned=cap) if trunc else None,
        provenance=_prov(req, "reads"),
    )


async def coverage(req, ctx):
    if not req.file.is_local:
        await ctx.resolve_file(req.file, interval=req.interval)
    path, index = _local(ctx, req.file, (".bai",))
    iv, cap = req.interval, ctx.limits.max_records

    def run():
        with pysam.AlignmentFile(str(path), "rb", index_filename=index) as af:
            length = _contig(list(af.references), list(af.lengths), iv.contig)
            depth = [0] * iv.length
            for r in af.fetch(iv.contig, iv.start, iv.end):
                if r.flag & req.exclude_flags or r.mapping_quality < req.min_mapping_quality:
                    continue
                for p in r.get_reference_positions():
                    if iv.start <= p < iv.end:
                        depth[p - iv.start] += 1
            return depth, length

    depth, length = await ctx.run_blocking(run)
    bs = req.bin_size
    records = []
    for s in range(iv.start, iv.end, bs):
        seg = depth[s - iv.start : min(iv.end, s + bs) - iv.start]
        records.append(
            {
                "start": s,
                "end": min(iv.end, s + bs),
                "complete": True,
                "mean": sum(seg) / len(seg),
                "min": min(seg),
                "max": max(seg),
            }
        )
    summary = {
        "positions_computed": len(depth),
        "mean": sum(depth) / len(depth),
        "min": min(depth),
        "max": max(depth),
        "bases_with_coverage": sum(1 for d in depth if d),
        "total_depth": sum(depth),
    }
    return OperationOutput(
        data={
            "records": records[:cap],
            "mode": "bins",
            "bin_size": bs,
            "interval": iv,
            "summary": summary,
            "complete": True,
            "complete_until": iv.end,
            "assembly": _assembly(req),
            "contig_length": length,
            "applied_filters": {
                "exclude_flags": req.exclude_flags,
                "min_mapping_quality": req.min_mapping_quality,
            },
        },
        provenance=_prov(req, "coverage"),
    )


def _gt(rec: Any, name: str) -> dict[str, Any] | None:
    s = rec.samples[name]
    if "GT" not in s:
        return None
    idx = list(s.allele_indices or [])
    sep = "|" if s.phased else "/"
    text = sep.join("." if i is None else str(i) for i in idx) or "."
    return {
        "text": text,
        "alleles": idx,
        "allele_bases": [rec.alleles[i] if i is not None else None for i in idx],
        "ploidy": len(idx),
        "phased": bool(s.phased) and len(idx) > 1,
        "missing": any(i is None for i in idx) or not idx,
    }


async def variants(req, ctx):
    if not req.file.is_local:
        await ctx.resolve_file(req.file, interval=req.interval)
    path, index = _local(ctx, req.file, (".tbi", ".csi"))
    iv, cap = req.interval, ctx.limits.max_records

    def run():
        with pysam.VariantFile(str(path), index_filename=index) as vf:
            header = list(vf.header.samples)
            if req.samples is not None:
                unknown = [s for s in req.samples if s not in header]
                if unknown:
                    raise InvalidInputError("unknown sample IDs", details={"unknown": unknown})
                vf.subset_samples(req.samples)
                names = list(req.samples)
            else:
                names = header
            contigs = vf.header.contigs
            length = _contig(list(contigs), [c.length for c in contigs.values()], iv.contig)
            out, trunc = [], False
            for rec in vf.fetch(iv.contig, iv.start, iv.end):
                if len(out) == cap:
                    trunc = True
                    break
                row = {
                    "contig": rec.contig,
                    "pos": rec.pos,
                    "start": rec.start,
                    "end": rec.stop,
                    "ids": [],
                    "ref": rec.ref,
                    "alts": list(rec.alts or []),
                    "qual": rec.qual,
                    "filters": list(rec.filter.keys()),
                    "filter_status": "PASS",
                    "info": {},
                }
                if req.include_genotypes:
                    row["samples"] = {
                        n: {
                            "format": {k: rec.samples[n][k] for k in rec.samples[n] if k != "GT"},
                            "genotype": _gt(rec, n),
                        }
                        for n in names
                    }
                out.append(row)
            return out, trunc, names, length

    records, trunc, names, length = await ctx.run_blocking(run)
    return OperationOutput(
        data={
            "records": records,
            "interval": iv,
            "samples": names,
            "assembly": _assembly(req),
            "header_reference": None,
            "contig_length": length,
        },
        truncation=Truncation(reason="max_records", limit=cap, returned=cap) if trunc else None,
        provenance=_prov(req, "variants"),
    )


async def sequence(req, ctx):
    path, index = _local(ctx, req.file, (".fai",))
    iv = req.interval

    def run():
        with pysam.FastaFile(str(path), filepath_index=index) as fa:
            length = _contig(list(fa.references), list(fa.lengths), iv.contig)
            return fa.fetch(iv.contig, iv.start, iv.end), length

    seq, length = await ctx.run_blocking(run)
    rec = {
        "contig": iv.contig,
        "start": iv.start,
        "end": iv.end,
        "assembly": iv.assembly,
        "length": len(seq),
        "sequence": seq,
    }
    return OperationOutput(
        data={"records": [rec], "assembly": _assembly(req), "contig_length": length},
        provenance=_prov(req, "sequence"),
    )


async def signal(req, ctx):
    import pyBigWig

    path = ctx.resolve_local_path(req.file.uri)
    iv, n = req.interval, req.bins

    def run():
        bw = pyBigWig.open(str(path))
        try:
            length = _contig(list(bw.chroms()), list(bw.chroms().values()), iv.contig)
            vals = bw.stats(iv.contig, iv.start, iv.end, type="mean", nBins=n, exact=True)
            whole = bw.stats(iv.contig, iv.start, iv.end, type="mean", exact=True)[0]
            return vals, whole, length
        finally:
            bw.close()

    vals, whole, length = await ctx.run_blocking(run)
    L = iv.length
    records = [
        {"start": iv.start + i * L // n, "end": iv.start + (i + 1) * L // n, "value": v}
        for i, v in enumerate(vals)
    ]
    return OperationOutput(
        data={
            "records": records,
            "mode": "bins",
            "interval": iv,
            "summary": {"type": "mean", "value": whole, "exact": True},
            "assembly": _assembly(req),
            "contig_length": length,
        },
        provenance=_prov(req, "signal"),
    )


def register_doubles(registry: Registry) -> None:
    for fmt in ("bam", "cram"):
        registry.register(Operation.GET_READS, fmt, reads, provider="test-double")
        registry.register(Operation.GET_COVERAGE, fmt, coverage, provider="test-double")
    for fmt in ("vcf", "bcf"):
        registry.register(Operation.GET_VARIANTS, fmt, variants, provider="test-double")
    registry.register(Operation.GET_SEQUENCE, "fasta", sequence, provider="test-double")
    registry.register(Operation.GET_SIGNAL, "bigwig", signal, provider="test-double")
