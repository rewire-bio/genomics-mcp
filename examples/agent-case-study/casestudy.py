"""Protocol arithmetic for the BCL11A enhancer case study: windows, metrics and contrasts.

Pure functions, no I/O. Used by replay.py and the tests. Coordinates are GRCh38, 0-based
half-open; see PROTOCOL.md.
"""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Any

ELEMENTS = ("E55", "E58", "E62")
WINDOWS = (*ELEMENTS, "BG_up", "BG_down", "P", "G")
ANCHORS = {"E55": ("rs7606173",), "E58": ("rs6706648", "rs6738440"), "E62": ("rs1427407",)}
HALF = 500
ACCESSIBLE = 3.0
LATE_DAY = 11
NON_ERYTHROID = ("GM12878", "HepG2", "CD14-positive monocyte")


def zero_based(vcf_pos: int) -> int:
    """VCF POS (1-based) -> 0-based position of the same base."""
    if vcf_pos < 1:
        raise ValueError("VCF POS is 1-based")
    return vcf_pos - 1


def tss_zero_based(start0: int, end0: int, strand: int) -> int:
    """TSS base of a transcript given as a 0-based half-open span [start0, end0)."""
    if strand not in (1, -1) or end0 <= start0:
        raise ValueError("need strand +1/-1 and a non-empty span")
    return start0 if strand == 1 else end0 - 1


def windows(vcf_pos: dict[str, int], bcl11a_tss0: int, gapdh_tss0: int) -> dict[str, dict]:
    """Protocol windows from anchor VCF positions and 0-based TSSs (bigWig contig names)."""
    a = {rs: zero_based(p) for rs, p in vcf_pos.items()}
    c55, c62 = a["rs7606173"], a["rs1427407"]
    c58 = (a["rs6706648"] + a["rs6738440"]) // 2

    def w(contig: str, start: int, end: int) -> dict:
        return {"contig": contig, "start": start, "end": end}

    return {
        "E55": w("chr2", c55 - HALF, c55 + HALF),
        "E58": w("chr2", c58 - HALF, c58 + HALF),
        "E62": w("chr2", c62 - HALF, c62 + HALF),
        "BG_up": w("chr2", c55 + 10_000, c55 + 20_000),
        "BG_down": w("chr2", c62 - 20_000, c62 - 10_000),
        "P": w("chr2", bcl11a_tss0 - HALF, bcl11a_tss0 + HALF),
        "G": w("chr12", gapdh_tss0 - HALF, gapdh_tss0 + HALF),
    }


def ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b <= 0:
        return None
    return a / b


def metrics(m: dict[str, float | None]) -> dict[str, float | None]:
    """Derived per-file metrics from window means (None = no data, never zero)."""
    bg = None if m["BG_up"] is None or m["BG_down"] is None else (m["BG_up"] + m["BG_down"]) / 2
    out: dict[str, float | None] = {"B": bg}
    for e in ELEMENTS:
        out[f"{e}_over_B"] = ratio(m[e], bg)
        out[f"{e}_over_G"] = ratio(m[e], m["G"])
    out["P_over_B"] = ratio(m["P"], bg)
    over_g = [out[f"{e}_over_G"] for e in ELEMENTS]
    out["max_over_G"] = None if None in over_g else max(over_g)  # type: ignore[type-var]
    over_b = [out[f"{e}_over_B"] for e in ELEMENTS]
    out["max_over_B"] = None if None in over_b else max(over_b)  # type: ignore[type-var]
    return out


def separated(high: list[float | None], low: list[float | None]) -> bool | None:
    """Every value of `high` above every value of `low`; None if any value is missing."""
    if not high or not low or None in high or None in low:
        return None
    return min(high) > max(low)  # type: ignore[type-var]


def summary(values: list[float | None]) -> dict[str, Any]:
    vals = [v for v in values if v is not None]
    return {
        "n": len(values),
        "n_missing": len(values) - len(vals),
        "mean": mean(vals) if vals else None,
        "min": min(vals) if vals else None,
        "max": max(vals) if vals else None,
    }


def day(label: str) -> int | None:
    return int(label.rsplit(" ", 1)[1]) if label.startswith("EPO culture day ") else None


def value(r: dict, key: str) -> float | None:
    """A derived metric, or a raw window mean for keys `raw_<window>`."""
    return r["means"][key[4:]] if key.startswith("raw_") else r["metrics"][key]


def timing(rows: list[dict], key: str) -> dict[str, Any]:
    """Per day: do all replicates exceed the largest day-0 replicate on `key`?"""
    day0 = [value(r, key) for r in rows if day(r["label"]) == 0]
    ceiling = None if not day0 or None in day0 else max(day0)  # type: ignore[type-var]
    by_day = []
    for d in sorted({day(r["label"]) for r in rows if day(r["label"]) is not None}):
        vals = [value(r, key) for r in rows if day(r["label"]) == d]
        above = None if ceiling is None or None in vals else all(v > ceiling for v in vals)
        by_day.append({"day": d, key: vals, "all_above_day0_max": above})
    first = next((t["day"] for t in by_day if t["day"] > 0 and t["all_above_day0_max"]), None)
    return {f"day0_max_{key}": ceiling, "by_day": by_day, "first_day": first}


def _cv(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2 or mean(vals) == 0:
        return None
    return pstdev(vals) / mean(vals)


def contrasts(rows: list[dict]) -> dict[str, Any]:
    """rows: one per file with keys label, file, means (window -> mean) and metrics."""

    def pick(pred, key):
        return [value(r, key) for r in rows if pred(r)]

    def late(r):
        return (day(r["label"]) or -1) >= LATE_DAY

    def non_ery(r):
        return r["label"] in NON_ERYTHROID

    def lineage(keys):
        return {
            key: {
                "late_erythroid": summary(pick(late, key)),
                "non_erythroid": summary(pick(non_ery, key)),
                "separated": separated(pick(late, key), pick(non_ery, key)),
            }
            for key in keys
        }

    out: dict[str, Any] = {
        "lineage": lineage(("E58_over_G", "max_over_G", "E58_over_B", "max_over_B"))
    }
    out["gm12878"] = [
        {
            "file": r["file"],
            "P_over_B": r["metrics"]["P_over_B"],
            "E58_over_B": r["metrics"]["E58_over_B"],
            "promoter_accessible": _acc(r["metrics"]["P_over_B"]),
            "E58_accessible": _acc(r["metrics"]["E58_over_B"]),
        }
        for r in rows
        if r["label"] == "GM12878"
    ]
    out["timing"] = timing(rows, "E58_over_G")
    out["k562"] = [
        {
            "file": r["file"],
            **{k: r["metrics"][k] for k in ("E58_over_B", "max_over_B", "E58_over_G", "P_over_B")},
            "E58_accessible": _acc(r["metrics"]["E58_over_B"]),
        }
        for r in rows
        if r["label"] == "K562"
    ]
    counts = dict.fromkeys(ELEMENTS, 0)
    for r in rows:
        if late(r):
            vals = {e: r["means"][e] for e in ELEMENTS}
            if None not in vals.values():
                counts[max(vals, key=lambda e: vals[e])] += 1
    out["sub_elements_late_erythroid"] = {
        "files_where_highest": counts,
        "mean_over_G": {e: summary(pick(late, f"{e}_over_G"))["mean"] for e in ELEMENTS},
    }
    days = sorted({day(r["label"]) for r in rows if day(r["label"]) is not None})
    groups = sorted({r["label"] for r in rows}, key=lambda g: (day(g) is None, day(g) or 0, g))
    out["denominator_check"] = {
        "note": "added after review; reported alongside, not instead of, the contrasts above",
        "lineage_raw_E58": lineage(("raw_E58",))["raw_E58"],
        "timing_raw_E58": timing(rows, "raw_E58"),
        "timing_E58_over_B": timing(rows, "E58_over_B"),
        "G_all_files": {
            **summary(pick(lambda r: True, "raw_G")),
            "cv": _cv(pick(lambda r: True, "raw_G")),
        },
        "B_all_files": {**summary(pick(lambda r: True, "B")), "cv": _cv(pick(lambda r: True, "B"))},
        "by_label": {
            g: {
                k: summary(pick(lambda r, g=g: r["label"] == g, k))["mean"]
                for k in ("raw_E58", "raw_G", "B", "E58_over_G", "E58_over_B")
            }
            for g in groups
        },
        "time_course_days": days,
    }
    return out


def _acc(v: float | None) -> bool | None:
    return None if v is None else v >= ACCESSIBLE
