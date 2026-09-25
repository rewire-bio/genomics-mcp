"""VCF/BCF task run inside the isolated reader process.

Records keep VCF POS (1-based, labelled `pos`) and also give `start`/`end` (0-based
half-open over the REF span, END-aware for symbolic alleles). Genotypes keep the exact GT
text (so mixed phasing and missing alleles are not lost), allele indices with null for
missing, ploidy and the allele strings they refer to. Nothing is flattened or normalized.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pysam

from genomics_mcp.errors import InvalidInputError, PreparationRequiredError
from genomics_mcp.storage.native_common import (
    assembly_status,
    check_contig,
    jsonable,
    open_error,
)

pysam.set_verbosity(1)
_SEP = re.compile(r"[|/]")


def _genotype(text: str, rec: Any, sample: Any) -> dict[str, Any]:
    indices = list(sample.allele_indices) if sample.allele_indices is not None else []
    separators = _SEP.findall(text.lstrip("|/"))
    return {
        "text": text,
        "alleles": indices,
        "allele_bases": [rec.alleles[i] if i is not None else None for i in indices],
        "ploidy": len(indices),
        "phased": (bool(separators) and all(s == "|" for s in separators)) or text.startswith("|"),
        "separators": separators,
        "missing": any(i is None for i in indices) or not indices,
    }


def _record(rec: Any, samples: list[str] | None, include_gt: bool) -> dict[str, Any]:
    filters = list(rec.filter.keys())
    out: dict[str, Any] = {
        "contig": rec.contig,
        "pos": rec.pos,
        "start": rec.start,
        "end": rec.stop,
        "ids": rec.id.split(";") if rec.id else [],
        "ref": rec.ref,
        "alts": list(rec.alts or []),
        "qual": jsonable(rec.qual),
        "filters": filters,
        "filter_status": "not_applied"
        if not filters
        else ("PASS" if filters == ["PASS"] else "FAIL"),
        "info": {k: jsonable(v) for k, v in rec.info.items()},
    }
    if include_gt and samples:
        cols = str(rec).rstrip("\n").split("\t")
        keys = cols[8].split(":") if len(cols) > 8 else []
        gt_at = keys.index("GT") if "GT" in keys else None
        # Serialized sample columns follow the active header order, not the caller's order.
        column = {name: 9 + j for j, name in enumerate(rec.samples)}
        if len(cols) != 9 + len(column):
            raise InvalidInputError(
                "record has a different number of sample columns than the header"
            )
        per: dict[str, Any] = {}
        for name in samples:
            s = rec.samples[name]
            fields = {k: jsonable(s[k]) for k in s.keys() if k != "GT"}
            entry: dict[str, Any] = {"format": fields}
            if gt_at is not None:
                raw = cols[column[name]].split(":")
                text = raw[gt_at] if gt_at < len(raw) else "."
                entry["genotype"] = _genotype(text, rec, s)
            per[name] = entry
        out["samples"] = per
    return out


def variants(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    iv = p["interval"]
    contig, start, end = iv["contig"], iv["start"], iv["end"]
    kwargs: dict[str, Any] = {}
    if p.get("index"):
        kwargs["index_filename"] = p["index"]
    try:
        vf = pysam.VariantFile(p["uri"], **kwargs)
    except (OSError, ValueError) as exc:
        raise open_error(exc, what=p["format"].upper(), has_index=bool(p.get("index"))) from None
    try:
        header = vf.header
        lengths = {name: c.length for name, c in header.contigs.items()}
        declared = None
        if contig in header.contigs:
            hr = header.contigs[contig].header_record
            declared = hr.get("assembly") if hr is not None else None
            check_contig(contig, lengths, start, end)
        header_reference = next(
            (r.value for r in header.records if r.key == "reference" and r.value), None
        )
        all_samples = list(header.samples)
        wanted = p.get("samples")
        if wanted is not None:
            unknown = [s for s in wanted if s not in all_samples]
            if unknown:
                raise InvalidInputError(
                    "unknown sample IDs",
                    details={
                        "unknown": unknown[:20],
                        "available_sample_count": len(all_samples),
                        "available_sample_sample": all_samples[:20],
                    },
                )
            vf.subset_samples(wanted)
            samples = list(wanted)
        else:
            samples = all_samples
        try:
            if p.get("index"):
                it = vf.fetch(contig, start, end)
            elif p.get("region_slice"):
                it = (
                    r for r in vf.fetch() if r.contig == contig and r.start < end and r.stop > start
                )
            else:
                raise PreparationRequiredError("no index for this file")
        except ValueError as exc:
            if "invalid contig" in str(exc) or "not found" in str(exc).lower():
                raise InvalidInputError(
                    f"contig {contig!r} is not in the file or its index",
                    hint="contig names must match exactly; no renaming is done",
                ) from None
            raise open_error(
                exc, what=p["format"].upper(), has_index=bool(p.get("index"))
            ) from None
        out: list[dict[str, Any]] = []
        truncated = False
        complete = True
        cap = p["max_records"]
        try:
            for rec in it:
                if time.monotonic() > deadline:
                    complete = False
                    break
                if p.get("pass_only") and list(rec.filter.keys()) != ["PASS"]:
                    continue
                if len(out) == cap:
                    truncated = True
                    break
                out.append(_record(rec, samples, p["include_genotypes"]))
        except OSError as exc:
            raise InvalidInputError(
                f"variant records could not be decoded ({str(exc)[:200]})"
            ) from None
        return {
            "records": out,
            "truncated": truncated,
            "complete": complete,
            "samples": samples if p["include_genotypes"] else [],
            "assembly": assembly_status(
                iv["assembly"], declared, file_asserted=bool(p.get("file_assembly"))
            ),
            "header_reference": header_reference,
            "contig_length": lengths.get(contig),
        }
    finally:
        vf.close()


NATIVE_TASKS = {"variants": variants}
