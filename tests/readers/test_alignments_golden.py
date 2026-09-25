"""BAM/CRAM reads, coverage and pileup against samtools 1.x with matching options."""

from __future__ import annotations

import collections
import re
import subprocess

import pytest
from gm_test_support import SAMTOOLS, envelope, iv, needs_samtools

pytestmark = needs_samtools


def region(contig: str, start: int, end: int) -> str:
    return f"{contig}:{start + 1}-{end}"


def samtools(*args: str) -> str:
    return subprocess.run([SAMTOOLS, *args], check=True, capture_output=True, text=True).stdout


# --------------------------------------------------------------------------- reads


@pytest.mark.parametrize(
    ("require", "exclude", "mapq"),
    [(0, 0, 0), (0, 0x400, 0), (0, 0x704, 20), (0x10, 0, 0), (0x1, 0x100, 0), (0x40, 0, 0)],
)
async def test_reads_match_samtools_view(service, golden, require, exclude, mapq):
    res = envelope(
        await service.call(
            "get_reads",
            {
                "file": {"uri": str(golden["bam"])},
                "interval": iv("chrG", 100, 300),
                "require_flags": require,
                "exclude_flags": exclude,
                "min_mapping_quality": mapq,
            },
        )
    )
    assert res["status"] == "ok", res.get("error")
    expected = samtools(
        "view",
        "-f",
        str(require),
        "-F",
        str(exclude),
        "-q",
        str(mapq),
        str(golden["bam"]),
        region("chrG", 100, 300),
    ).splitlines()
    got = [(r["name"], r["flag"], r["start"] + 1, r["cigar"]) for r in res["data"]["records"]]
    want = [(f[0], int(f[1]), int(f[3]), f[5]) for f in (ln.split("\t") for ln in expected)]
    assert got == want
    assert res["data"]["applied_filters"]["require_flags"] == require
    assert res["data"]["applied_filters"]["exclude_flags"] == exclude


async def test_reads_have_no_sequence_by_default(service, golden):
    base = {"file": {"uri": str(golden["bam"])}, "interval": iv("chrG", 100, 120)}
    res = envelope(await service.call("get_reads", base))
    assert all("sequence" not in r for r in res["data"]["records"])
    res = envelope(await service.call("get_reads", {**base, "include_sequence": True}))
    rec = next(r for r in res["data"]["records"] if r["name"] == "m1")
    assert rec["sequence"] == golden["seqs"]["chrG"][100:150]
    assert rec["base_qualities"] == [30] * 50


async def test_reads_mates_and_coordinates(service, golden):
    res = envelope(
        await service.call(
            "get_reads", {"file": {"uri": str(golden["bam"])}, "interval": iv("chrG", 200, 201)}
        )
    )
    p1 = next(r for r in res["data"]["records"] if r["name"] == "p1")
    assert p1["start"] == 200 and p1["end"] == 250  # 0-based half-open
    assert p1["mate"] == {"contig": "chrG", "start": 220, "unmapped": False, "reverse": True}
    assert set(p1["flags"]) >= {"PAIRED", "PROPER_PAIR", "READ1", "MREVERSE"}
    # A read ending exactly at the query start does not overlap [start, end).
    edge = envelope(
        await service.call(
            "get_reads", {"file": {"uri": str(golden["bam"])}, "interval": iv("chrG", 150, 151)}
        )
    )
    assert "m1" not in {r["name"] for r in edge["data"]["records"]}


async def test_reads_truncation_is_explicit(service, golden):
    res = envelope(
        await service.call(
            "get_reads",
            {
                "file": {"uri": str(golden["bam"])},
                "interval": iv("chrG", 1000, 1400),
                "max_records": 25,
            },
        )
    )
    assert res["status"] == "ok"
    assert len(res["data"]["records"]) == 25
    assert res["truncation"] == {
        "truncated": True,
        "reason": "max_records",
        "limit": 25,
        "returned": 25,
        "available": None,
        "next_cursor": None,
    }


async def test_contig_boundaries_and_names(service, golden):
    f = {"uri": str(golden["bam"])}
    last = envelope(await service.call("get_reads", {"file": f, "interval": iv("chrH", 499, 500)}))
    assert [r["name"] for r in last["data"]["records"]] == ["endH"]
    beyond = envelope(
        await service.call("get_reads", {"file": f, "interval": iv("chrH", 490, 501)})
    )
    assert beyond["error"]["code"] == "invalid_input" and "length 500" in beyond["error"]["message"]
    renamed = envelope(await service.call("get_reads", {"file": f, "interval": iv("G", 0, 10)}))
    assert renamed["error"]["code"] == "not_found"
    assert "no chr-prefix renaming" in renamed["error"]["hint"]


async def test_assembly_declared_vs_asserted(service, golden):
    f = {"uri": str(golden["bam"])}
    ok = envelope(await service.call("get_reads", {"file": f, "interval": iv("chrG", 0, 10)}))
    assert ok["data"]["assembly"]["status"] == "file_header_declared"
    bad = envelope(
        await service.call("get_reads", {"file": f, "interval": iv("chrG", 0, 10, "GRCh38")})
    )
    assert bad["error"]["code"] == "invalid_input" and "no liftover" in bad["error"]["hint"]
    noas = {"uri": str(golden["bam_noas"])}
    caller = envelope(
        await service.call("get_reads", {"file": noas, "interval": iv("chrG", 0, 10, "GRCh38")})
    )
    assert caller["data"]["assembly"] == {
        "requested": "GRCh38",
        "file_declared": None,
        "status": "caller_asserted",
    }
    meta = envelope(
        await service.call(
            "get_reads",
            {"file": {**noas, "assembly": "GRCh38"}, "interval": iv("chrG", 0, 10, "GRCh38")},
        )
    )
    assert meta["data"]["assembly"]["status"] == "file_metadata_asserted"


# --------------------------------------------------------------------------- coverage


def depth_oracle(bam, reg, exclude, mq, bq) -> list[int]:
    g = 0x704 & ~exclude
    args = ["depth", "-a", "-r", reg, "-Q", str(mq), "-q", str(bq)]
    if g:
        args += ["-g", str(g)]
    if exclude:
        args += ["-G", str(exclude)]
    return [int(ln.split("\t")[2]) for ln in samtools(*args, str(bam)).splitlines()]


@pytest.mark.parametrize(
    ("exclude", "mq", "bq"),
    [(0x704, 0, 0), (0, 0, 0), (0x400, 0, 0), (0x704, 20, 0), (0x704, 0, 20), (0x714, 10, 31)],
)
async def test_coverage_matches_samtools_depth(service, golden, exclude, mq, bq):
    res = envelope(
        await service.call(
            "get_coverage",
            {
                "file": {"uri": str(golden["bam"])},
                "interval": iv("chrG", 95, 300),
                "exclude_flags": exclude,
                "min_mapping_quality": mq,
                "min_base_quality": bq,
            },
        )
    )
    assert res["status"] == "ok", res.get("error")
    got = [r["depth"] for r in res["data"]["records"]]
    assert got == depth_oracle(golden["bam"], region("chrG", 95, 300), exclude, mq, bq)
    assert [r["pos"] for r in res["data"]["records"]] == list(range(95, 300))
    assert res["data"]["summary"]["total_depth"] == sum(got)


async def test_coverage_excludes_deletions_and_skips(service, golden):
    res = envelope(
        await service.call(
            "get_coverage", {"file": {"uri": str(golden["bam"])}, "interval": iv("chrG", 150, 153)}
        )
    )
    # 'del' has 3D at [150,153); 'skip' has 100N over it; neither contributes depth there.
    names_over = samtools("view", str(golden["bam"]), region("chrG", 150, 153)).count("\n")
    assert names_over > max(r["depth"] for r in res["data"]["records"])


async def test_coverage_bins_and_truncation(service, golden):
    base = {"file": {"uri": str(golden["bam"])}, "interval": iv("chrG", 1000, 1400)}
    per_base = depth_oracle(golden["bam"], region("chrG", 1000, 1400), 0x704, 0, 0)
    res = envelope(await service.call("get_coverage", {**base, "bin_size": 100}))
    assert [b["mean"] for b in res["data"]["records"]] == [
        sum(per_base[i : i + 100]) / 100 for i in range(0, 400, 100)
    ]
    assert all(b["complete"] for b in res["data"]["records"])
    trunc = envelope(await service.call("get_coverage", {**base, "max_records": 10}))
    assert len(trunc["data"]["records"]) == 10
    assert trunc["truncation"]["available"] == 400 and trunc["truncation"]["returned"] == 10
    # The output cap does not cap the input: the summary covers every position.
    assert trunc["data"]["summary"]["total_depth"] == sum(per_base)
    assert trunc["data"]["summary"]["max"] == max(per_base)
    assert trunc["data"]["complete"] is True


async def test_coverage_input_cap_is_disclosed_not_zero(service, golden, monkeypatch):
    from genomics_mcp.readers import handlers

    monkeypatch.setattr(handlers, "COVERAGE_INPUT_READ_CAP", 50)
    res = envelope(
        await service.call(
            "get_coverage",
            {"file": {"uri": str(golden["bam"])}, "interval": iv("chrG", 1000, 1400)},
        )
    )
    assert res["status"] == "partial"
    assert res["errors"][0]["code"] == "budget_exceeded"
    until = res["data"]["complete_until"]
    assert 1000 < until < 1400
    assert res["data"]["complete"] is False
    positions = [r["pos"] for r in res["data"]["records"]]
    assert positions == list(range(1000, until))  # nothing reported past the processed reads
    per_base = depth_oracle(golden["bam"], region("chrG", 1000, until), 0x704, 0, 0)
    assert [r["depth"] for r in res["data"]["records"]] == per_base
    bins = envelope(
        await service.call(
            "get_coverage",
            {
                "file": {"uri": str(golden["bam"])},
                "interval": iv("chrG", 1000, 1400),
                "bin_size": 100,
            },
        )
    )
    assert any(b["mean"] is None and not b["complete"] for b in bins["data"]["records"])


# --------------------------------------------------------------------------- pileup


def mpileup_oracle(bam, ref, reg, bq, mq, ff, maxd, overlap=True):
    args = [
        "mpileup",
        "-B",
        "-A",
        "-Q",
        str(bq),
        "-q",
        str(mq),
        "--ff",
        str(ff),
        "-d",
        str(maxd),
        "-r",
        reg,
        "-f",
        str(ref),
    ]
    if not overlap:
        args.append("-x")
    out = {}
    for line in samtools(*args, str(bam)).splitlines():
        _c, pos, refb, n, s = line.split("\t")[:5]
        c = collections.Counter()
        s = re.sub(r"\^.", "", s).replace("$", "")
        i = 0
        ins, dels = collections.Counter(), collections.Counter()
        while i < len(s):
            ch = s[i]
            if ch in "+-":
                m = re.match(r"[+-](\d+)", s[i:])
                k = int(m.group(1))
                seq = s[i + len(m.group(0)) : i + len(m.group(0)) + k]
                (ins if ch == "+" else dels)[seq.upper() if ch == "+" else k] += 1
                i += len(m.group(0)) + k
                continue
            if ch in ".,":
                c[refb.upper()] += 1
            elif ch in "*#":
                c["*"] += 1
            elif ch in "<>":
                c[">"] += 1
            else:
                c[ch.upper()] += 1
            i += 1
        out[int(pos) - 1] = (int(n), dict(c), dict(ins), dict(dels))
    return out


@pytest.mark.parametrize(
    ("bq", "mq", "ff", "maxd"),
    [
        (13, 0, 0x704, 8000),
        (0, 0, 0x704, 8000),
        (13, 20, 0x704, 8000),
        (30, 0, 0, 8000),
        (13, 0, 0x704, 2),
    ],
)
async def test_pileup_matches_samtools_mpileup(service, golden, bq, mq, ff, maxd):
    res = envelope(
        await service.call(
            "get_pileup",
            {
                "file": {"uri": str(golden["bam"])},
                "reference": {"uri": str(golden["ref"])},
                "interval": iv("chrG", 100, 260),
                "min_base_quality": bq,
                "min_mapping_quality": mq,
                "exclude_flags": ff,
                "max_depth": maxd,
            },
        )
    )
    assert res["status"] == "ok", res.get("error")
    oracle = mpileup_oracle(
        golden["bam"], golden["ref"], region("chrG", 100, 260), bq, mq, ff, maxd
    )
    got = {}
    for r in res["data"]["records"]:
        counts = dict(r["bases"])
        if r["deletions"]:
            counts["*"] = r["deletions"]
        if r["ref_skips"]:
            counts[">"] = r["ref_skips"]
        ins = {i["sequence"]: i["count"] for i in r["insertions_after"]}
        dels = {d["length"]: d["count"] for d in r["deletions_starting_after"]}
        got[r["pos"]] = (r["depth"], counts, ins, dels)
        assert r["ref_base"] == golden["seqs"]["chrG"][r["pos"]]
    assert got == oracle
    af = res["data"]["applied_filters"]
    assert af["baq"] is False and af["overlap_detection"] is True and af["count_orphans"] is True


async def test_pileup_observations_are_distinct(service, golden):
    res = envelope(
        await service.call(
            "get_pileup",
            {
                "file": {"uri": str(golden["bam"])},
                "interval": iv("chrG", 149, 166),
                "min_base_quality": 0,
            },
        )
    )
    by = {r["pos"]: r for r in res["data"]["records"]}
    assert by[149]["deletions_starting_after"] == [{"length": 3, "count": 1}]
    assert by[150]["deletions"] == 1 and by[150]["ref_skips"] == 1
    assert by[164]["insertions_after"] == [{"sequence": "GG", "count": 1}]
    assert by[150]["ref_base"] is None  # no reference given: no invented reference base
    overlap = envelope(
        await service.call(
            "get_pileup",
            {
                "file": {"uri": str(golden["bam"])},
                "interval": iv("chrG", 225, 226),
                "min_base_quality": 0,
            },
        )
    )
    assert overlap["data"]["records"][0]["reads_with_mate_in_column"] == 2


async def test_pileup_depth_limit_is_reported(service, golden):
    res = envelope(
        await service.call(
            "get_pileup",
            {
                "file": {"uri": str(golden["bam"])},
                "interval": iv("chrG", 1100, 1110),
                "max_depth": 5,
            },
        )
    )
    assert res["status"] == "ok"
    assert all(r["depth_limit_reached"] for r in res["data"]["records"])
    assert res["truncation"]["reason"] == "max_depth"
    assert any("max_depth=5" in w for w in res["warnings"])


# --------------------------------------------------------------------------- CRAM references


async def test_cram_with_correct_reference_matches_bam(service, golden):
    base = {"interval": iv("chrG", 100, 300), "exclude_flags": 0}
    bam = envelope(
        await service.call(
            "get_reads", {"file": {"uri": str(golden["bam"])}, **base, "include_sequence": True}
        )
    )
    cram = envelope(
        await service.call(
            "get_reads",
            {
                "file": {"uri": str(golden["cram"])},
                "reference": {"uri": str(golden["ref"])},
                **base,
                "include_sequence": True,
            },
        )
    )
    assert cram["status"] == "ok", cram.get("error")
    strip = lambda recs: [{k: v for k, v in r.items() if k != "tags"} for r in recs]  # noqa: E731
    assert strip(cram["data"]["records"]) == strip(bam["data"]["records"])
    assert cram["data"]["reference"]["status"] == "md5_verified"
    assert cram["data"]["reference"]["reference_md5"] == golden["md5"]["chrG"]


async def test_cram_wrong_reference_is_refused(service, golden):
    res = envelope(
        await service.call(
            "get_reads",
            {
                "file": {"uri": str(golden["cram"])},
                "reference": {"uri": str(golden["wrong_ref"])},
                "interval": iv("chrG", 100, 200),
            },
        )
    )
    assert res["error"]["code"] == "invalid_input"
    assert "does not match the file header (M5)" in res["error"]["message"]


async def test_cram_missing_reference_never_uses_header_ur(service, golden):
    # samtools recorded @SQ UR:<path of ref.fa>; that file exists and is under an allowed root,
    # but the caller did not approve it, so decoding must fail rather than read it.
    header = samtools("view", "-H", str(golden["cram"]))
    assert f"UR:{golden['ref']}" in header or "UR:" in header
    res = envelope(
        await service.call(
            "get_reads", {"file": {"uri": str(golden["cram"])}, "interval": iv("chrG", 100, 200)}
        )
    )
    assert res["error"]["code"] == "preparation_required"
    assert "reference" in res["error"]["message"]


async def test_cram_ignores_ambient_ref_path(service, golden, tmp_path, monkeypatch):
    cache = tmp_path / "ambient-cache"
    cache.mkdir()
    (cache / golden["md5"]["chrG"]).write_text(golden["seqs"]["chrG"])
    (cache / golden["md5"]["chrH"]).write_text(golden["seqs"]["chrH"])
    monkeypatch.setenv("REF_PATH", str(cache) + "/%s")
    monkeypatch.setenv("REF_CACHE", str(cache) + "/%s")
    res = envelope(
        await service.call(
            "get_reads", {"file": {"uri": str(golden["cram"])}, "interval": iv("chrG", 100, 200)}
        )
    )
    assert res["error"]["code"] == "preparation_required"


@pytest.mark.parametrize("key", ["cram_embedded", "cram_noref"])
async def test_self_contained_cram_decodes_without_reference(service, golden, key):
    base = {"interval": iv("chrG", 100, 300), "exclude_flags": 0, "include_sequence": True}
    cram = envelope(await service.call("get_reads", {"file": {"uri": str(golden[key])}, **base}))
    bam = envelope(await service.call("get_reads", {"file": {"uri": str(golden["bam"])}, **base}))
    assert cram["status"] == "ok", cram.get("error")
    assert [r["sequence"] for r in cram["data"]["records"]] == [
        r["sequence"] for r in bam["data"]["records"]
    ]
    assert cram["data"]["reference"]["status"] == "none_supplied"


async def test_cram_coverage_and_pileup_with_reference(service, golden):
    for op in ("get_coverage", "get_pileup"):
        args = {"interval": iv("chrG", 100, 260), "reference": {"uri": str(golden["ref"])}}
        a = envelope(await service.call(op, {"file": {"uri": str(golden["bam"])}, **args}))
        b = envelope(await service.call(op, {"file": {"uri": str(golden["cram"])}, **args}))
        assert b["status"] == "ok", b.get("error")
        assert a["data"]["records"] == b["data"]["records"], op


def _reheader(src, header_text, dest, tmp_path) -> None:
    (tmp_path / "h.sam").write_text(header_text)
    with dest.open("wb") as out:
        subprocess.run(
            [SAMTOOLS, "reheader", str(tmp_path / "h.sam"), str(src)], check=True, stdout=out
        )


async def test_malformed_m5_is_refused(service, golden, tmp_path):
    import shutil

    bad = golden["root"] / "badm5.cram"
    if not bad.exists():
        hdr = samtools("view", "-H", str(golden["cram_noref"]))
        _reheader(
            golden["cram_noref"],
            hdr.replace(golden["md5"]["chrH"], "../../../../etc/hosts"),
            bad,
            tmp_path,
        )
        shutil.copy(str(golden["cram_noref"]) + ".crai", str(bad) + ".crai")
    res = envelope(
        await service.call(
            "get_reads", {"file": {"uri": str(bad)}, "interval": iv("chrG", 100, 200)}
        )
    )
    assert res["error"]["code"] == "invalid_input" and "malformed @SQ M5" in res["error"]["message"]
