#!/usr/bin/env python3
"""Curated discovery for the BCL11A enhancer case study (run by the case-study author, not the agent).

Applies the selection rules in PROTOCOL.md to the public ENCODE REST API and writes:

- manifest.json: the panel of experiments and one bigWig per biological replicate;
- evidence/encode_metadata.json: every query URL, UTC time, HTTP status, SHA-256 of the raw
  response body and the (field-restricted, small) response itself.

Anonymous HTTPS only; standard library only. Run from the repository root:

    python3 examples/agent-case-study/discovery/select_files.py
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://www.encodeproject.org"
HERE = Path(__file__).resolve().parent.parent
UW = "John Stamatoyannopoulos, UW"
SIGNAL = "read-depth normalized signal"

# Group 1: every released GRCh38 DNase-seq experiment of one adult donor's CD34+ progenitors
# cultured with EPO/SCF/IL-3/hydrocortisone (erythroid differentiation), day 0..20.
TIME_COURSE_DONOR = "ENCDO937OUY"
# Group 2: reference cell types, chosen by rule (see PROTOCOL.md), plus all fetal erythroblast data.
CONTROL_TERMS = ["K562", "GM12878", "HepG2", "CD14-positive monocyte"]
FETAL_TERM = "erythroblast"

EXPERIMENT_FIELDS = [
    "accession",
    "biosample_summary",
    "biosample_ontology.term_name",
    "biosample_ontology.classification",
    "lab.title",
    "date_released",
    "default_analysis",
    "assembly",
    "replicates.biological_replicate_number",
    "replicates.technical_replicate_number",
    "replicates.library.biosample.accession",
    "replicates.library.biosample.donor.accession",
    "replicates.library.biosample.age_display",
    "replicates.library.biosample.treatments.treatment_term_name",
    "replicates.library.biosample.treatments.duration",
    "audit.ERROR.category",
    "audit.NOT_COMPLIANT.category",
]
FILE_FIELDS = [
    "accession",
    "file_format",
    "output_type",
    "assembly",
    "status",
    "biological_replicates",
    "technical_replicates",
    "file_size",
    "md5sum",
    "href",
    "cloud_metadata.url",
    "date_created",
]

log: list[dict] = []


def get(path: str, params: list[tuple[str, str]]) -> dict:
    url = f"{BASE}{path}?" + urllib.parse.urlencode([*params, ("format", "json")])
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310 - fixed https host
                body = r.read()
                status = r.status
            break
        except (TimeoutError, OSError) as exc:  # transient network errors: retry, and record
            log.append(
                {
                    "url": url,
                    "retrieved_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "attempt": attempt,
                    "error": type(exc).__name__,
                }
            )
            if attempt == 3:
                raise
    data = json.loads(body)
    log.append(
        {
            "url": url,
            "retrieved_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "http_status": status,
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "response": data,
        }
    )
    return data


def search(params: dict[str, str | list[str]], fields: list[str]) -> list[dict]:
    q: list[tuple[str, str]] = [("limit", "all")]
    for k, v in params.items():
        q += [(k, x) for x in (v if isinstance(v, list) else [v])]
    q += [("field", f) for f in fields]
    return get("/search/", q).get("@graph", [])


def dnase(extra: dict[str, str]) -> list[dict]:
    return search(
        {
            "type": "Experiment",
            "assay_title": "DNase-seq",
            "status": "released",
            "assembly": "GRCh38",
            **extra,
        },
        EXPERIMENT_FIELDS,
    )


def reps(exp: dict) -> list[int]:
    return sorted({r["biological_replicate_number"] for r in exp.get("replicates", [])})


def flagged(exp: dict) -> list[str]:
    audit = exp.get("audit") or {}
    return sorted({a["category"] for k in ("ERROR", "NOT_COMPLIANT") for a in audit.get(k, [])})


def treated(exp: dict) -> bool:
    return any(r["library"]["biosample"].get("treatments") for r in exp.get("replicates", []))


ANALYSIS_FIELDS = ["accession", "title", "assembly", "pipeline_version", "status"]
_analyses: dict[str, dict] = {}


def analyses(paths: list[str], files: bool = False) -> dict[str, dict]:
    """Analysis objects by @id, fetched in batches through field-restricted searches."""
    want = [p for p in dict.fromkeys(paths) if files or p not in _analyses]
    for i in range(0, len(want), 50):
        batch = want[i : i + 50]
        rows = search(
            {"type": "Analysis", "accession": [p.strip("/").split("/")[-1] for p in batch]},
            ANALYSIS_FIELDS + (["files.accession"] if files else []),
        )
        for r in rows:
            _analyses[f"/analyses/{r['accession']}/"] = r
    return {p: _analyses[p] for p in paths}


def uniform(analysis: dict) -> bool:
    return (
        str(analysis.get("title", "")).startswith("ENCODE4")
        and analysis.get("assembly") == "GRCh38"
    )


def pick_control(term: str) -> tuple[dict | None, list[dict]]:
    """Filters: untreated, no ERROR/NOT_COMPLIANT audit, default analysis is the ENCODE4 GRCh38
    pipeline. Preference order: UW lab (as the time course), exactly two biological replicates,
    earliest release date, smallest accession."""
    exps = dnase({"biosample_ontology.term_name": term})
    by_id = analyses([e["default_analysis"] for e in exps])
    ok = [
        e
        for e in exps
        if not treated(e) and not flagged(e) and uniform(by_id[e["default_analysis"]])
    ]
    ok.sort(
        key=lambda e: (
            e["lab"]["title"] != UW,
            reps(e) != [1, 2],
            e.get("date_released", ""),
            e["accession"],
        )
    )
    considered = [
        {
            "accession": e["accession"],
            "lab": e["lab"]["title"],
            "biological_replicates": reps(e),
            "treated": treated(e),
            "audit_flags": flagged(e),
            "date_released": e.get("date_released"),
            "default_analysis": by_id[e["default_analysis"]].get("title"),
            "eligible": e in ok,
        }
        for e in sorted(exps, key=lambda e: e["accession"])
    ]
    return (ok[0] if ok else None), considered


def day(exp: dict) -> int:
    ds = {
        t.get("duration")
        for r in exp.get("replicates", [])
        for t in r["library"]["biosample"].get("treatments") or []
    }
    return int(next(iter(ds))) if ds else 0


def files_for(exp: dict) -> tuple[dict, list[dict]]:
    """One ENCODE4 default-analysis GRCh38 read-depth normalized signal bigWig per replicate."""
    analysis = analyses([exp["default_analysis"]], files=True)[exp["default_analysis"]]
    if not uniform(analysis):
        raise SystemExit(
            f"{exp['accession']}: default analysis {analysis.get('title')} is not ENCODE4 GRCh38"
        )
    rows = search(
        {"type": "File", "dataset": f"/experiments/{exp['accession']}/", "file_format": "bigWig"},
        FILE_FIELDS,
    )
    chosen = [
        f
        for f in rows
        if f.get("output_type") == SIGNAL
        and f.get("assembly") == "GRCh38"
        and f.get("status") == "released"
        and f["accession"] in {x.get("accession") for x in analysis.get("files", [])}
    ]
    by_rep: dict[int, list[dict]] = {}
    for f in chosen:
        if len(f["biological_replicates"]) != 1:
            raise SystemExit(f"{f['accession']} pools replicates {f['biological_replicates']}")
        by_rep.setdefault(f["biological_replicates"][0], []).append(f)
    if sorted(by_rep) != reps(exp) or any(len(v) != 1 for v in by_rep.values()):
        raise SystemExit(f"{exp['accession']}: expected one file per replicate, got {by_rep}")
    out = []
    for rep in sorted(by_rep):
        f = by_rep[rep][0]
        r = next(r for r in exp["replicates"] if r["biological_replicate_number"] == rep)
        bio = r["library"]["biosample"]
        out.append(
            {
                "file": f["accession"],
                "biological_replicate": rep,
                "technical_replicates": f.get("technical_replicates"),
                "biosample": bio["accession"],
                "donor": (bio.get("donor") or {}).get("accession"),
                "url": (f.get("cloud_metadata") or {}).get("url"),
                "encode_href": f"{BASE}{f['href']}",
                "md5": f.get("md5sum"),
                "size_bytes": f.get("file_size"),
                "date_created": f.get("date_created"),
            }
        )
    meta = {
        "accession": analysis["accession"],
        "title": analysis.get("title"),
        "pipeline_version": analysis.get("pipeline_version"),
    }
    return meta, out


def entry(exp: dict, group: str, label: str, rule: str) -> dict:
    analysis, files = files_for(exp)
    return {
        "group": group,
        "label": label,
        "experiment": exp["accession"],
        "biosample_term": exp["biosample_ontology"]["term_name"],
        "biosample_classification": exp["biosample_ontology"].get("classification"),
        "biosample_summary": exp.get("biosample_summary"),
        "lab": exp["lab"]["title"],
        "date_released": exp.get("date_released"),
        "audit_flags": flagged(exp),
        "analysis": analysis,
        "selection_rule": rule,
        "files": files,
    }


def main() -> None:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    panel: list[dict] = []
    course = dnase({"replicates.library.biosample.donor.accession": TIME_COURSE_DONOR})
    course = [
        e
        for e in course
        if e["biosample_ontology"]["term_name"] == "hematopoietic multipotent progenitor cell"
    ]
    for e in sorted(course, key=day):
        panel.append(
            entry(
                e,
                "erythroid_time_course",
                f"EPO culture day {day(e)}",
                f"all released GRCh38 DNase-seq of donor {TIME_COURSE_DONOR} progenitors",
            )
        )
    considered = {}
    for term in CONTROL_TERMS:
        exp, considered[term] = pick_control(term)
        if exp is None:
            raise SystemExit(f"no experiment satisfies the control rule for {term}")
        panel.append(
            entry(
                exp,
                "reference_cell_types",
                term,
                "untreated, no ERROR/NOT_COMPLIANT audit, ENCODE4 GRCh38 default analysis; "
                "prefer UW lab, then exactly 2 biological replicates, then earliest release, "
                "then smallest accession",
            )
        )
    # Fetal erythroblasts would be informative, but only data from the same uniform pipeline is
    # included; anything else is recorded as considered and excluded.
    fetal = dnase({"biosample_ontology.term_name": FETAL_TERM})
    fetal_analyses = analyses([e["default_analysis"] for e in fetal])
    considered[FETAL_TERM] = []
    for e in sorted(fetal, key=lambda e: e["accession"]):
        eligible = uniform(fetal_analyses[e["default_analysis"]]) and not flagged(e)
        considered[FETAL_TERM].append(
            {
                "accession": e["accession"],
                "biological_replicates": reps(e),
                "default_analysis": fetal_analyses[e["default_analysis"]].get("title"),
                "eligible": eligible,
            }
        )
        if eligible:
            panel.append(
                entry(
                    e,
                    "reference_cell_types",
                    "fetal erythroblast",
                    "released GRCh38 erythroblast DNase-seq with ENCODE4 default analysis",
                )
            )
    n_files = sum(len(p["files"]) for p in panel)
    manifest = {
        "schema": "genomics-mcp/agent-case-study/manifest@1",
        "curated_by": "case-study author (Claude) with discovery/select_files.py; NOT chosen by the agent",
        "generated_utc": started,
        "assembly": "GRCh38",
        "assay": "DNase-seq",
        "output_type": SIGNAL,
        "coordinates": "0-based half-open",
        "experiments": len(panel),
        "files": n_files,
        "panel": panel,
        "control_candidates_considered": considered,
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    (HERE / "evidence").mkdir(exist_ok=True)
    (HERE / "evidence" / "encode_metadata.json").write_text(
        json.dumps({"generated_utc": started, "requests": log}, indent=None, separators=(",", ":"))
        + "\n"
    )
    print(json.dumps({"experiments": len(panel), "files": n_files, "requests": len(log)}))


if __name__ == "__main__":
    main()
