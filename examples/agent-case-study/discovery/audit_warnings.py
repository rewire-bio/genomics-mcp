#!/usr/bin/env python3
"""Record every ENCODE audit category (including WARNING) for the 14 panel experiments.

The selection rules in select_files.py used only ERROR and NOT_COMPLIANT audits, and the manifest
records only those. This supplementary record, made after the run, discloses the WARNING-level
audits too (for example GM12878's low read depth and low SPOT score). It does not change the panel.

    python3 examples/agent-case-study/discovery/audit_warnings.py

One anonymous HTTPS request. Writes evidence/encode_audits.json with the URL, UTC time, SHA-256
of the raw response body and the per-experiment audit categories and details.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LEVELS = ("ERROR", "NOT_COMPLIANT", "WARNING")


def main() -> None:
    manifest = json.loads((HERE / "manifest.json").read_text())
    accessions = [p["experiment"] for p in manifest["panel"]]
    q = [("type", "Experiment"), ("format", "json"), ("limit", "all"), ("field", "accession")]
    q += [("accession", a) for a in accessions]
    q += [("field", f"audit.{lvl}.{k}") for lvl in LEVELS for k in ("category", "detail")]
    url = "https://www.encodeproject.org/search/?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310 - fixed public https host
        body = r.read()
    graph = {g["accession"]: g for g in json.loads(body)["@graph"]}
    if sorted(graph) != sorted(accessions):
        raise SystemExit(f"ENCODE returned {sorted(graph)}, expected {sorted(accessions)}")
    experiments = {}
    for p in manifest["panel"]:
        audit = graph[p["experiment"]].get("audit") or {}
        experiments[p["experiment"]] = {
            "label": p["label"],
            **{
                lvl: [
                    {"category": a.get("category"), "detail": a.get("detail")}
                    for a in audit.get(lvl, [])
                ]
                for lvl in LEVELS
            },
        }
    out = {
        "url": url,
        "retrieved_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(body).hexdigest(),
        "note": "supplementary disclosure after the run; not used for selection",
        "experiments": experiments,
    }
    (HERE / "evidence" / "encode_audits.json").write_text(json.dumps(out, indent=1) + "\n")
    for acc, e in experiments.items():
        print(acc, e["label"], {lvl: sorted({a["category"] for a in e[lvl]}) for lvl in LEVELS})


if __name__ == "__main__":
    main()
