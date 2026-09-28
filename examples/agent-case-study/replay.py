#!/usr/bin/env python3
"""Deterministic, model-free replay and verification of an agent run.

    .venv/bin/python examples/agent-case-study/replay.py RUN_DIR            # live replay
    python3 examples/agent-case-study/replay.py RUN_DIR --offline           # saved evidence only

Live mode:
1. Resolves the anchors and canonical TSSs independently from the Ensembl REST API (GRCh38) and
   computes the protocol windows with casestudy.windows().
2. Reads all 26 files x 7 windows directly with pyBigWig over HTTPS range requests (not through
   the MCP server), plus any other interval the agent measured.
3. Writes RUN_DIR/replay/ensembl.json and RUN_DIR/results.json.

Both modes then verify, cell by cell, for every (file accession, window):
- the agent measured it over MCP: at least one tool result covers exactly that file and interval;
  repeated measurements must agree; a null or failed component is a failure unless pyBigWig also
  finds no data;
- the MCP value equals the pyBigWig value (relative tolerance 1e-9);
- the agent's reported value for that cell equals its own MCP tool result for that cell;
- the agent's windows equal the independently computed windows.
Any failure is listed in results.json and the command exits with status 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import casestudy as cs  # noqa: E402

ENSEMBL = "https://rest.ensembl.org"
REL_TOL = 1e-9
log: list[dict] = []


def ensembl(path: str) -> dict:
    url = f"{ENSEMBL}{path}"
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"})  # noqa: S310
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https host
                body = r.read()
            break
        except OSError as exc:
            log.append({"url": url, "attempt": attempt, "error": type(exc).__name__})
            if attempt == 3:
                raise
            time.sleep(2 * attempt)
    data = json.loads(body)
    log.append(
        {
            "url": url,
            "retrieved_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "sha256": hashlib.sha256(body).hexdigest(),
            "response": data,
        }
    )
    return data


def reference_coordinates() -> dict[str, Any]:
    info = ensembl("/info/assembly/homo_sapiens")
    vcf_pos = {}
    for rs in ("rs7606173", "rs6706648", "rs6738440", "rs1427407"):
        v = ensembl(f"/variation/human/{rs}")
        (m,) = [x for x in v["mappings"] if x["assembly_name"] == "GRCh38"]
        if m["seq_region_name"] != "2" or m["start"] != m["end"]:
            raise SystemExit(f"{rs}: unexpected mapping {m}")
        vcf_pos[rs] = m["start"]  # Ensembl variation coordinates are 1-based
    tss = {}
    for gene in ("BCL11A", "GAPDH"):
        g = ensembl(f"/lookup/symbol/homo_sapiens/{gene}")
        tx = ensembl(f"/lookup/id/{g['canonical_transcript'].split('.')[0]}")
        start0, end0 = tx["start"] - 1, tx["end"]  # 1-based closed -> 0-based half-open
        tss[gene] = {
            "transcript": f"{tx['id']}.{tx['version']}",
            "strand": tx["strand"],
            "contig": tx["seq_region_name"],
            "tss_0based": cs.tss_zero_based(start0, end0, tx["strand"]),
        }
    return {
        "source": "Ensembl REST (rest.ensembl.org), independent of the MCP server",
        "assembly": info.get("assembly_name"),
        "assembly_accession": info.get("assembly_accession"),
        "vcf_pos": vcf_pos,
        "tss": tss,
    }


def read_means(url: str, intervals: list[tuple[str, int, int]]) -> dict[tuple, float | None]:
    import pyBigWig

    for attempt in range(1, 4):
        try:
            bw = pyBigWig.open(url)
            if bw is None:
                raise OSError("pyBigWig.open returned None")
            try:
                return {
                    iv: bw.stats(iv[0], iv[1], iv[2], type="mean", exact=True)[0]
                    for iv in intervals
                }
            finally:
                bw.close()
        except (OSError, RuntimeError) as exc:
            log.append({"url": url, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"})
            if attempt == 3:
                raise
            time.sleep(3 * attempt)
    raise AssertionError("unreachable")


def close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1e-12)


def live(run: Path, manifest: dict, calls: list[dict]) -> dict:
    import pyBigWig

    if pyBigWig.remote != 1:
        raise SystemExit("pyBigWig was built without libcurl (pyBigWig.remote != 1)")
    started = datetime.now(UTC).isoformat(timespec="seconds")
    ref = reference_coordinates()
    win = cs.windows(
        ref["vcf_pos"], ref["tss"]["BCL11A"]["tss_0based"], ref["tss"]["GAPDH"]["tss_0based"]
    )
    protocol = [(win[k]["contig"], win[k]["start"], win[k]["end"]) for k in cs.WINDOWS]
    measured = {
        (m["file"], m["contig"], m["start"], m["end"])
        for c in calls
        for m in c.get("measurements") or []
    }
    rows, other = [], []
    t0 = time.monotonic()
    for p in manifest["panel"]:
        for f in p["files"]:
            extra = sorted({k[1:] for k in measured if k[0] == f["file"]} - set(protocol))
            means = read_means(f["url"], protocol + extra)
            by_window = {k: means[iv] for k, iv in zip(cs.WINDOWS, protocol, strict=True)}
            other += [{"file": f["file"], "interval": list(iv), "mean": means[iv]} for iv in extra]
            rows.append(
                {
                    "file": f["file"],
                    "experiment": p["experiment"],
                    "label": p["label"],
                    "group": p["group"],
                    "biological_replicate": f["biological_replicate"],
                    "donor": f["donor"],
                    "lab": p["lab"],
                    "pipeline": p["analysis"]["title"],
                    "audit_flags": p["audit_flags"],
                    "means": by_window,
                    "metrics": cs.metrics(by_window),
                }
            )
    (run / "replay").mkdir(exist_ok=True)
    (run / "replay" / "ensembl.json").write_text(
        json.dumps(
            {"generated_utc": started, "requests": [x for x in log if "response" in x]}, indent=1
        )
        + "\n"
    )
    return {
        "generated_utc": started,
        "method": f"pyBigWig {getattr(pyBigWig, '__version__', '?')} "
        "stats(type='mean', exact=True) over HTTPS range requests; independent of the MCP server",
        "coordinates": {"reference": ref, "windows": win},
        "files": rows,
        "other_agent_intervals": other,
        "replay_read_seconds": round(time.monotonic() - t0, 1),
        "replay_errors": [x for x in log if "error" in x],
    }


def verify(results: dict, manifest: dict, calls: list[dict], agent: dict) -> dict:
    failures: list[dict] = []
    win = results["coordinates"]["windows"]
    by_interval = {(w["contig"], w["start"], w["end"]): k for k, w in win.items()}

    # MCP measurements per (file, window), with the call that produced each.
    mcp: dict[tuple[str, str], list[dict]] = {}
    for c in calls:
        for m in c.get("measurements") or []:
            k = by_interval.get((m["contig"], m["start"], m["end"]))
            if k is not None:
                mcp.setdefault((m["file"], k), []).append({"call": c["n"], **m})

    replay = {r["file"]: r["means"] for r in results["files"]}
    expected = [(f["file"], k) for p in manifest["panel"] for f in p["files"] for k in cs.WINDOWS]
    cells = []
    for key in expected:
        got = mcp.get(key, [])
        ok_vals = [g for g in got if g.get("status") == "ok"]
        cell = {
            "file": key[0],
            "window": key[1],
            "calls": [g["call"] for g in got],
            "mcp": ok_vals[-1]["mean"] if ok_vals else None,
            "pybigwig": replay[key[0]][key[1]],
        }
        if not got:
            failures.append({"check": "coverage", **cell, "detail": "never measured over MCP"})
        elif not ok_vals:
            failures.append({"check": "coverage", **cell, "detail": "only failed results",
                             "errors": [g.get("error") for g in got]})  # fmt: skip
        elif len({json.dumps(g["mean"]) for g in ok_vals}) > 1:
            failures.append({"check": "repeat", **cell, "detail": "repeated values differ"})
        elif not close(cell["mcp"], cell["pybigwig"]):
            failures.append({"check": "mcp_vs_pybigwig", **cell})
        cells.append(cell)

    # The agent's JSON: exactly one value per cell, equal to its own MCP result for that cell.
    reported: dict[tuple[str, str], Any] = {}
    for fr in agent.get("files") or []:
        for k, v in (fr.get("mean") or {}).items():
            key = (fr.get("file"), k)
            if key in reported:
                failures.append({"check": "agent_duplicate", "file": key[0], "window": k})
            reported[key] = v
    cell_map = {(c["file"], c["window"]): c for c in cells}
    for key in expected:
        if key not in reported:
            failures.append({"check": "agent_missing", "file": key[0], "window": key[1]})
        elif not close(reported[key], cell_map[key]["mcp"]):
            failures.append({"check": "agent_vs_mcp", "file": key[0], "window": key[1],
                             "agent": reported[key], "mcp": cell_map[key]["mcp"]})  # fmt: skip
    for key in set(reported) - set(expected):
        failures.append({"check": "agent_unexpected", "file": key[0], "window": key[1]})

    agent_win = agent.get("windows") or {}
    for k in cs.WINDOWS:
        a = {x: (agent_win.get(k) or {}).get(x) for x in ("contig", "start", "end")}
        if a != win[k]:
            failures.append({"check": "window", "window": k, "agent": a, "replay": win[k]})

    return {
        "passed": not failures,
        "cells_expected": len(expected),
        "cells_measured_over_mcp": sum(1 for c in cells if c["mcp"] is not None),
        "cells_reported_by_agent": len(reported),
        "rel_tolerance": REL_TOL,
        "failures": failures,
        "cells": cells,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("run", type=Path)
    ap.add_argument("--offline", action="store_true", help="verify saved evidence; no network")
    a = ap.parse_args()
    manifest = json.loads((HERE / "manifest.json").read_text())
    calls = json.loads((a.run / "tool_calls.json").read_text())
    agent = json.loads((a.run / "agent_report.json").read_text()) or {}
    run_info = json.loads((a.run / "run.json").read_text())
    if run_info.get("completion") != "complete":
        print(f"run is not complete: {run_info.get('completion')}", file=sys.stderr)
        return 1
    if a.offline:
        results = json.loads((a.run / "results.json").read_text())
        rows = [{**r, "metrics": cs.metrics(r["means"])} for r in results["files"]]
        if json.loads(json.dumps(cs.contrasts(rows))) != results["contrasts"]:
            print("stored contrasts do not follow from stored means", file=sys.stderr)
            return 1
    else:
        results = {
            "schema": "genomics-mcp/agent-case-study/results@1",
            **live(a.run, manifest, calls),
        }
        results["contrasts"] = cs.contrasts(results["files"])
    results["agent_contrast_calls"] = agent.get("contrast_calls")
    results["verification"] = verify(results, manifest, calls, agent)
    if not a.offline:
        (a.run / "results.json").write_text(json.dumps(results, indent=1) + "\n")
    v = results["verification"]
    print(json.dumps({k: v[k] for k in ("passed", "cells_expected", "cells_measured_over_mcp",
                                        "cells_reported_by_agent")} | {"failures": len(v["failures"])}))  # fmt: skip
    for f in v["failures"][:20]:
        print(json.dumps(f), file=sys.stderr)
    return 0 if v["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
