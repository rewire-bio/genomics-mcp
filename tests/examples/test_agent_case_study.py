"""Offline checks for examples/agent-case-study: coordinates, replicate handling, and the
committed agent-run evidence (no fabricated values, replay agreement, redaction)."""

from __future__ import annotations

import json
import re
import sys
from itertools import pairwise
from pathlib import Path

import pytest

CASE = Path(__file__).resolve().parents[2] / "examples" / "agent-case-study"
RUN = CASE / "runs" / "2026-09-28-claude-opus-5"
INTERRUPTED = CASE / "runs" / "2026-09-28-interrupted"
sys.path.insert(0, str(CASE))

import casestudy as cs  # noqa: E402
import replay  # noqa: E402

# GRCh38 VCF positions of the anchors (dbSNP/Ensembl), used as literal fixtures.
ANCHORS = {
    "rs7606173": 60498316,
    "rs6706648": 60494905,
    "rs6738440": 60495106,
    "rs1427407": 60490908,
}


# ------------------------------------------------------------------ coordinates


def test_vcf_pos_to_zero_based_matches_spdi():
    # NCBI SPDI for rs1427407 is NC_000002.12:60490907:T:G (0-based position).
    assert cs.zero_based(60490908) == 60490907
    with pytest.raises(ValueError):
        cs.zero_based(0)


def test_tss_is_first_transcribed_base_on_either_strand():
    # BCL11A canonical ENST00000642384.2, minus strand, 0-based half-open [60457192, 60553654).
    assert cs.tss_zero_based(60457192, 60553654, -1) == 60553653
    assert cs.tss_zero_based(6534511, 6538374, 1) == 6534511
    with pytest.raises(ValueError):
        cs.tss_zero_based(10, 10, 1)


def test_protocol_windows_are_half_open_disjoint_and_ordered():
    w = cs.windows(ANCHORS, bcl11a_tss0=60553653, gapdh_tss0=6534511)
    assert w["E62"] == {"contig": "chr2", "start": 60490407, "end": 60491407}
    # +58 centre: floor((60494904 + 60495105) / 2) = 60495004
    assert w["E58"] == {"contig": "chr2", "start": 60494504, "end": 60495504}
    assert w["E55"] == {"contig": "chr2", "start": 60497815, "end": 60498815}
    for k in (*cs.ELEMENTS, "P", "G"):
        assert w[k]["end"] - w[k]["start"] == 1000
    for k in ("BG_up", "BG_down"):
        assert w[k]["end"] - w[k]["start"] == 10_000
    chr2 = sorted((w[k]["start"], w[k]["end"], k) for k in w if w[k]["contig"] == "chr2")
    for (_, end, a), (start, _, b) in pairwise(chr2):
        assert end <= start, f"{a} overlaps {b}"
    # BCL11A is on the minus strand: +55 is nearer the TSS than +62.
    assert w["E62"]["start"] < w["E58"]["start"] < w["E55"]["start"] < w["P"]["start"]
    assert w["G"]["contig"] == "chr12"


# ------------------------------------------------------------------ metrics


def test_metrics_keep_missing_data_missing():
    m = cs.metrics(
        {"E55": 4.0, "E58": 9.0, "E62": 2.0, "BG_up": None, "BG_down": 1.0, "P": 5.0, "G": 3.0}
    )
    assert m["B"] is None and m["E58_over_B"] is None and m["max_over_B"] is None
    assert m["E58_over_G"] == 3.0 and m["max_over_G"] == 3.0
    zero_g = cs.metrics(
        {"E55": 1.0, "E58": 1.0, "E62": 1.0, "BG_up": 1.0, "BG_down": 3.0, "P": 8.0, "G": 0.0}
    )
    assert zero_g["E58_over_G"] is None and zero_g["B"] == 2.0 and zero_g["P_over_B"] == 4.0


def test_separation_is_strict_and_undefined_with_missing_values():
    assert cs.separated([3.0, 4.0], [1.0, 2.9]) is True
    assert cs.separated([3.0, 4.0], [3.0]) is False
    assert cs.separated([3.0, None], [1.0]) is None


def _row(label, file, e58, g=1.0, p=1.0):
    means = {"E55": 1.0, "E58": e58, "E62": 1.0, "BG_up": 1.0, "BG_down": 1.0, "P": p, "G": g}
    return {"label": label, "file": file, "group": "", "means": means, "metrics": cs.metrics(means)}


def test_timing_needs_every_replicate_above_the_day0_maximum():
    rows = [
        _row("EPO culture day 0", "a", 1.0),
        _row("EPO culture day 0", "b", 2.0),
        _row("EPO culture day 4", "c", 3.0),
        _row("EPO culture day 4", "d", 1.5),  # one replicate below: day 4 does not qualify
        _row("EPO culture day 8", "e", 2.5),
        _row("EPO culture day 8", "f", 4.0),
        _row("GM12878", "g", 0.5, p=6.0),
    ]
    c = cs.contrasts(rows)
    assert c["timing"]["day0_max_E58_over_G"] == 2.0
    assert [t["all_above_day0_max"] for t in c["timing"]["by_day"]] == [False, False, True]
    assert c["timing"]["first_day"] == 8
    assert c["gm12878"][0]["promoter_accessible"] is True
    assert c["gm12878"][0]["E58_accessible"] is False


# ------------------------------------------------------------------ manifest


def _manifest():
    return json.loads((CASE / "manifest.json").read_text())


def test_manifest_has_one_file_per_biological_replicate():
    m = _manifest()
    seen = set()
    for p in m["panel"]:
        reps = [f["biological_replicate"] for f in p["files"]]
        assert len(reps) == len(set(reps)), p["experiment"]
        assert p["analysis"]["title"].startswith("ENCODE4") and "GRCh38" in p["analysis"]["title"]
        for f in p["files"]:
            assert f["file"] not in seen
            seen.add(f["file"])
            assert f["url"].startswith("https://encode-public.s3.amazonaws.com/")
            assert f["url"].endswith(f"/{f['file']}.bigWig")
            assert "?" not in f["url"]  # unsigned public object URL
    assert len(seen) == m["files"] == 26


def test_time_course_is_one_donor_and_labelled_so():
    m = _manifest()
    course = [p for p in m["panel"] if p["group"] == "erythroid_time_course"]
    assert {f["donor"] for p in course for f in p["files"]} == {"ENCDO937OUY"}
    assert [cs.day(p["label"]) for p in course] == [0, 4, 6, 8, 11, 13, 15, 17, 18, 20]
    late = [f for p in course if cs.day(p["label"]) >= cs.LATE_DAY for f in p["files"]]
    non = [f for p in m["panel"] if p["label"] in cs.NON_ERYTHROID for f in p["files"]]
    assert (len(late), len(non)) == (11, 5)  # the counts stated in PROTOCOL.md


def test_manifest_files_trace_to_saved_encode_responses():
    raw = json.loads((CASE / "evidence" / "encode_metadata.json").read_text())
    rows = {}
    for r in raw["requests"]:
        for g in (r.get("response") or {}).get("@graph", []):
            if "File" in g.get("@type", []):
                rows[g["accession"]] = g
    for p in _manifest()["panel"]:
        for f in p["files"]:
            g = rows[f["file"]]
            assert g["md5sum"] == f["md5"]
            assert g["biological_replicates"] == [f["biological_replicate"]]
            assert g["assembly"] == "GRCh38" and g["output_type"] == "read-depth normalized signal"


# ------------------------------------------------------------------ cell-level verification


def _synthetic():
    """Two files, protocol windows, MCP calls, and an agent report that copies them exactly."""
    manifest = {"panel": [{"files": [{"file": "ENCFFA"}, {"file": "ENCFFB"}]}]}
    win = cs.windows(ANCHORS, 60553653, 6534511)
    value = {("ENCFFA", k): 1.0 + i for i, k in enumerate(cs.WINDOWS)}
    value |= {("ENCFFB", k): 10.0 + i for i, k in enumerate(cs.WINDOWS)}
    calls = [
        {
            "n": n + 1,
            "measurements": [
                {"file": f, **win[k], "mean": value[(f, k)], "status": "ok"}
                for f in ("ENCFFA", "ENCFFB")
            ],
        }
        for n, k in enumerate(cs.WINDOWS)
    ]
    results = {
        "coordinates": {"windows": win},
        "files": [
            {"file": f, "means": {k: value[(f, k)] for k in cs.WINDOWS}}
            for f in ("ENCFFA", "ENCFFB")
        ],
    }
    agent = {
        "windows": win,
        "files": [
            {"file": f, "mean": {k: value[(f, k)] for k in cs.WINDOWS}}
            for f in ("ENCFFA", "ENCFFB")
        ],
    }
    return results, manifest, calls, agent


def _checks(v):
    return sorted({f["check"] for f in v["failures"]})


def test_verifier_passes_exact_copies():
    v = replay.verify(*_synthetic())
    assert v["passed"] and v["cells_expected"] == 14 and v["cells_reported_by_agent"] == 14


def test_verifier_catches_a_value_reported_for_the_wrong_file():
    results, manifest, calls, agent = _synthetic()
    a, b = agent["files"]
    a["mean"]["E58"], b["mean"]["E58"] = b["mean"]["E58"], a["mean"]["E58"]
    v = replay.verify(results, manifest, calls, agent)
    assert not v["passed"] and _checks(v) == ["agent_vs_mcp"]
    assert {(f["file"], f["window"]) for f in v["failures"]} == {
        ("ENCFFA", "E58"),
        ("ENCFFB", "E58"),
    }


def test_verifier_does_not_let_a_duplicate_replace_a_missing_cell():
    results, manifest, calls, agent = _synthetic()
    g = calls[cs.WINDOWS.index("G")]
    g["measurements"] = [g["measurements"][0], g["measurements"][0]]  # ENCFFA twice, ENCFFB never
    v = replay.verify(results, manifest, calls, agent)
    assert ("coverage", "ENCFFB", "G") in {
        (f["check"], f.get("file"), f.get("window")) for f in v["failures"]
    }


def test_verifier_reports_failed_components_and_disagreeing_repeats():
    results, manifest, calls, agent = _synthetic()
    calls[0]["measurements"][0].update(mean=None, status="error", error="timeout")
    calls.append({"n": 99, "measurements": [{**calls[1]["measurements"][1], "mean": 123.0}]})
    v = replay.verify(results, manifest, calls, agent)
    assert _checks(v) == ["agent_vs_mcp", "coverage", "repeat"]


def test_verifier_catches_pybigwig_disagreement_and_wrong_windows():
    results, manifest, calls, agent = _synthetic()
    results["files"][0]["means"]["P"] *= 1.001
    agent["windows"] = {**agent["windows"], "E62": {**agent["windows"]["E62"], "start": 0}}
    v = replay.verify(results, manifest, calls, agent)
    assert _checks(v) == ["mcp_vs_pybigwig", "window"]


# ------------------------------------------------------------------ interrupted first attempt


def test_interrupted_attempt_is_labelled_and_has_no_findings():
    run = json.loads((INTERRUPTED / "run.json").read_text())
    assert run["completion"].startswith("interrupted")
    assert run["models_used"] == ["claude-opus-5-5", "claude-opus-5"]
    assert json.loads((INTERRUPTED / "agent_report.json").read_text()) == {
        "complete": False,
        "completion": run["completion"],
    }
    assert "Nothing here is a finding" in (INTERRUPTED / "agent_report.md").read_text()


# ------------------------------------------------------------------ complete agent run

needs_run = pytest.mark.skipif(not (RUN / "results.json").exists(), reason="no committed run")


def _events(run=RUN):
    return [json.loads(x) for x in (run / "transcript.redacted.jsonl").read_text().splitlines()]


@needs_run
def test_run_is_complete_one_model_and_tool_only():
    run = json.loads((RUN / "run.json").read_text())
    assert run["completion"] == "complete"
    assert run["models_used"] == [run["model_requested"]] and run["model_fallbacks"] == []
    init = next(e for e in _events() if e["type"] == "init")
    assert init["tools"] and all(t.startswith("mcp__genomics__") for t in init["tools"])
    launch = json.loads((RUN / "launch.json").read_text())
    assert launch["provenance"]["runtime_src_equals_v0_1_0"] is True
    assert launch["provenance"]["runtime_dirty"] is False


@needs_run
def test_every_tool_call_has_a_result_and_is_counted():
    ev = _events()
    uses = [e["id"] for e in ev if e["type"] == "tool_use"]
    results = [e["tool_use_id"] for e in ev if e["type"] == "tool_result"]
    assert sorted(uses) == sorted(results) and len(uses) == len(set(uses))
    calls = json.loads((RUN / "tool_calls.json").read_text())
    assert [c["id"] for c in calls] == uses
    assert json.loads((RUN / "run.json").read_text())["mcp_calls"] == len(uses) <= 45


@needs_run
def test_all_182_cells_verify_against_their_own_tool_results():
    results = json.loads((RUN / "results.json").read_text())
    calls = json.loads((RUN / "tool_calls.json").read_text())
    agent = json.loads((RUN / "agent_report.json").read_text())
    v = replay.verify(results, _manifest(), calls, agent)
    assert v["failures"] == []
    assert (
        v["cells_expected"] == v["cells_measured_over_mcp"] == v["cells_reported_by_agent"] == 182
    )
    # The measured values really are in the transcript's tool results, at the cited calls.
    by_id = {
        e["tool_use_id"]: json.loads(e["content"]) for e in _events() if e["type"] == "tool_result"
    }
    n_to_id = {c["n"]: c["id"] for c in calls}
    for cell in v["cells"]:
        body = by_id[n_to_id[cell["calls"][-1]]]
        comps = body["data"]["files"] if "files" in body["data"] else None
        values = (
            [c["summary"]["value"] for c in comps if c["accession"] == cell["file"]]
            if comps
            else [body["data"]["summary"]["value"]]
        )
        assert cell["mcp"] in values


@needs_run
def test_stored_contrasts_follow_from_stored_means():
    res = json.loads((RUN / "results.json").read_text())
    rows = [{**r, "metrics": cs.metrics(r["means"])} for r in res["files"]]
    for r, stored in zip(rows, res["files"], strict=True):
        assert r["metrics"] == stored["metrics"]
    assert json.loads(json.dumps(cs.contrasts(rows))) == res["contrasts"]
    assert res["verification"]["passed"] is True


@pytest.mark.parametrize("run", [RUN, INTERRUPTED], ids=["complete", "interrupted"])
def test_committed_run_files_are_redacted(run):
    if not run.exists():
        pytest.skip("no run")
    home = str(Path.home())
    for p in run.glob("**/*"):
        if p.is_file() and "raw" not in p.parts:
            text = p.read_text()
            assert home not in text and "/Users/" not in text, p.name
            assert not re.search(r"(?i)x-amz-signature=(?!REDACTED)", text), p.name
            assert "sk-ant-" not in text and "AKIA" not in text, p.name
            assert "request_id" not in text and '"thinking"' not in text, p.name
    assert all(e["type"] != "assistant_thinking" for e in _events(run))
