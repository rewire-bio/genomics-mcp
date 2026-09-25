"""VCF/BCF records and genotypes against bcftools query with matching options."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from gm_test_support import BCFTOOLS, envelope, iv, needs_bcftools

pytestmark = needs_bcftools


def bcftools_query(path, reg, fmt, *extra) -> list[list[str]]:
    out = subprocess.run(
        [BCFTOOLS, "query", "-r", reg, "-f", fmt, *extra, str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [ln.split("\t") for ln in out.splitlines()]


@pytest.mark.parametrize("key", ["vcf", "vcf_tbi", "bcf"])
async def test_records_and_genotypes_match_bcftools(service, golden, key):
    res = envelope(
        await service.call(
            "get_variants", {"file": {"uri": str(golden[key])}, "interval": iv("chrG", 0, 3000)}
        )
    )
    assert res["status"] == "ok", res.get("error")
    oracle = bcftools_query(
        golden[key],
        "chrG:1-3000",
        "%CHROM\t%POS\t%END\t%ID\t%REF\t%ALT\t%QUAL\t%FILTER[\t%GT]\n",
    )
    got = []
    for r in res["data"]["records"]:
        gts = [r["samples"][s]["genotype"]["text"] for s in ("S1", "S2", "S3")]
        got.append(
            [
                r["contig"],
                str(r["pos"]),
                str(r["end"]),
                ";".join(r["ids"]) or ".",
                r["ref"],
                ",".join(r["alts"]) or ".",
                "." if r["qual"] is None else f"{r['qual']:g}",
                ";".join(r["filters"]) or ".",
                *gts,
            ]
        )
    assert got == oracle


async def test_genotype_semantics_are_preserved(service, golden):
    res = envelope(
        await service.call(
            "get_variants", {"file": {"uri": str(golden["bcf"])}, "interval": iv("chrG", 20, 45)}
        )
    )
    first, second, third = res["data"]["records"]
    assert first["alts"] == ["C", "G"] and first["ids"] == ["rs1", "rsAlt"]
    s1, s2, s3 = (first["samples"][s]["genotype"] for s in ("S1", "S2", "S3"))
    assert s1 == {
        "text": "1|2",
        "alleles": [1, 2],
        "allele_bases": ["C", "G"],
        "ploidy": 2,
        "phased": True,
        "separators": ["|"],
        "missing": False,
    }
    assert s2["alleles"] == [0, None] and s2["allele_bases"] == ["A", None] and s2["missing"]
    assert s3["ploidy"] == 3 and s3["alleles"] == [0, 1, 2] and s3["phased"] is False
    assert first["samples"]["S1"]["format"] == {"DP": 12, "AD": [0, 6, 6]}
    assert first["samples"]["S2"]["format"]["AD"] == [7, 0, None]
    assert first["info"] == {"DP": 30, "AF": [0.25, 0.5], "SOMATIC": True}
    haploid = second["samples"]["S1"]["genotype"]
    assert haploid["text"] == "1" and haploid["ploidy"] == 1 and haploid["allele_bases"] == ["G"]
    assert second["samples"]["S3"]["genotype"]["alleles"] == [None, None]
    assert second["start"] == 30 and second["end"] == 33  # REF GTA, 0-based half-open
    assert (
        third["alts"] == ["T", "*"] and third["filter_status"] == "FAIL" and third["qual"] is None
    )
    assert third["samples"]["S1"]["genotype"]["allele_bases"] == ["*", "T"]


async def test_symbolic_end_overlap_and_boundaries(service, golden):
    f = {"uri": str(golden["vcf"])}
    sv = envelope(await service.call("get_variants", {"file": f, "interval": iv("chrG", 200, 201)}))
    assert [r["ids"] for r in sv["data"]["records"]] == [["sv1"]]
    assert sv["data"]["records"][0]["end"] == 250 and sv["data"]["records"][0]["alts"] == ["<DEL>"]
    last = envelope(
        await service.call("get_variants", {"file": f, "interval": iv("chrG", 2999, 3000)})
    )
    assert [r["pos"] for r in last["data"]["records"]] == [3000]
    beyond = envelope(
        await service.call("get_variants", {"file": f, "interval": iv("chrG", 2999, 3001)})
    )
    assert beyond["error"]["code"] == "invalid_input"
    asm = envelope(
        await service.call("get_variants", {"file": f, "interval": iv("chrG", 0, 10, "GRCh38")})
    )
    assert asm["error"]["code"] == "invalid_input" and "synthetic-g1" in asm["error"]["message"]


async def test_filters_samples_and_truncation(service, golden):
    f = {"uri": str(golden["vcf"])}
    passing = envelope(
        await service.call(
            "get_variants", {"file": f, "interval": iv("chrG", 0, 3000), "pass_only": True}
        )
    )
    oracle = bcftools_query(golden["vcf"], "chrG:1-3000", "%POS\n", "-i", 'FILTER="PASS"')
    assert [[str(r["pos"])] for r in passing["data"]["records"]] == oracle
    sub = envelope(
        await service.call(
            "get_variants", {"file": f, "interval": iv("chrG", 0, 50), "samples": ["S3"]}
        )
    )
    assert list(sub["data"]["records"][0]["samples"]) == ["S3"]
    assert sub["data"]["records"][0]["samples"]["S3"]["genotype"]["text"] == "0/1/2"
    unknown = envelope(
        await service.call(
            "get_variants", {"file": f, "interval": iv("chrG", 0, 50), "samples": ["NA"]}
        )
    )
    assert unknown["error"]["code"] == "invalid_input"
    nogt = envelope(
        await service.call(
            "get_variants", {"file": f, "interval": iv("chrG", 0, 50), "include_genotypes": False}
        )
    )
    assert all("samples" not in r for r in nogt["data"]["records"])
    capped = envelope(
        await service.call(
            "get_variants", {"file": f, "interval": iv("chrG", 0, 3000), "max_records": 2}
        )
    )
    assert len(capped["data"]["records"]) == 2 and capped["truncation"]["reason"] == "max_records"


@pytest.mark.parametrize("key", ["vcf", "bcf"])
async def test_csi_long_contig(service, golden, key):
    res = envelope(
        await service.call(
            "get_variants",
            {
                "file": {"uri": str(golden[key])},
                "interval": iv("chrLong", 549_999_990, 550_000_010),
            },
        )
    )
    assert res["status"] == "ok", res.get("error")
    assert [(r["pos"], r["start"]) for r in res["data"]["records"]] == [(550_000_001, 550_000_000)]


async def test_missing_and_corrupt_indexes(service, golden, tmp_path):
    d = golden["root"] / "idx-cases"
    d.mkdir(exist_ok=True)
    shutil.copy(golden["vcf_tbi"], d / "noindex.vcf.gz")
    shutil.copy(golden["vcf_tbi"], d / "corrupt.vcf.gz")
    (d / "corrupt.vcf.gz.tbi").write_bytes(b"\x1f\x8b garbage")
    shutil.copy(golden["vcf_tbi"], d / "explicit.vcf.gz")
    interval = iv("chrG", 0, 50)
    missing = envelope(
        await service.call(
            "get_variants", {"file": {"uri": str(d / "noindex.vcf.gz")}, "interval": interval}
        )
    )
    assert missing["error"]["code"] == "preparation_required" and missing["data"] is None
    corrupt = envelope(
        await service.call(
            "get_variants", {"file": {"uri": str(d / "corrupt.vcf.gz")}, "interval": interval}
        )
    )
    assert (
        corrupt["error"]["code"] == "preparation_required"
        and "not valid" in corrupt["error"]["message"]
    )
    wrong = envelope(
        await service.call(
            "get_variants",
            {
                "file": {
                    "uri": str(d / "explicit.vcf.gz"),
                    "index_uri": str(golden["bam"]) + ".bai",
                },
                "interval": interval,
            },
        )
    )
    assert wrong["error"]["code"] == "invalid_input"
    plain = envelope(
        await service.call(
            "get_variants", {"file": {"uri": str(golden["vcf_plain"])}, "interval": interval}
        )
    )
    assert plain["error"]["code"] == "preparation_required"
    gz = envelope(
        await service.call(
            "get_variants", {"file": {"uri": str(golden["vcf_gzip"])}, "interval": interval}
        )
    )
    assert (
        gz["error"]["code"] == "preparation_required" and "ordinary gzip" in gz["error"]["message"]
    )


async def test_index_built_for_another_format_is_refused(service, golden):
    # A structurally valid TBI built with the BED preset, given for a VCF.
    res = envelope(
        await service.call(
            "get_variants",
            {
                "file": {
                    "uri": str(golden["vcf_tbi"]),
                    "index_uri": str(golden["root"] / "features.bed.gz.tbi"),
                },
                "interval": iv("chrG", 0, 50),
            },
        )
    )
    assert res["error"]["code"] == "invalid_input"
    assert "VCF preset" in res["error"]["message"]


@pytest.mark.parametrize("key", ["vcf", "bcf"])
async def test_requested_sample_order_never_swaps_genotypes(service, golden, key):
    f = {"uri": str(golden[key])}
    full = envelope(await service.call("get_variants", {"file": f, "interval": iv("chrG", 0, 50)}))
    rev = envelope(
        await service.call(
            "get_variants", {"file": f, "interval": iv("chrG", 0, 50), "samples": ["S3", "S1"]}
        )
    )
    assert rev["data"]["samples"] == ["S3", "S1"]
    for a, b in zip(full["data"]["records"], rev["data"]["records"], strict=True):
        assert list(b["samples"]) == ["S3", "S1"]
        for name in ("S3", "S1"):
            assert b["samples"][name] == a["samples"][name], (a["pos"], name)
    first = rev["data"]["records"][0]["samples"]
    assert first["S1"]["genotype"]["text"] == "1|2" and first["S1"]["genotype"]["alleles"] == [1, 2]
    assert first["S3"]["genotype"]["text"] == "0/1/2" and first["S3"]["genotype"]["ploidy"] == 3
    second = rev["data"]["records"][1]["samples"]
    assert second["S1"]["genotype"]["text"] == "1" and second["S1"]["format"]["DP"] == 9
    assert second["S3"]["genotype"]["alleles"] == [None, None]


@pytest.mark.parametrize("name", ["variants.vcf.gz", "variants.bcf"])
async def test_review_data_reversed_samples(tmp_path, name):
    from gm_test_support import REVIEW_DATA, make_settings

    from genomics_mcp.service import GenomicsService

    if not REVIEW_DATA:
        pytest.skip("set GENOMICS_MCP_TEST_FIXTURES")
    from pathlib import Path

    svc = GenomicsService(make_settings(tmp_path, [Path(REVIEW_DATA)]))
    try:
        res = envelope(
            await svc.call(
                "get_variants",
                {
                    "file": {"uri": str(Path(REVIEW_DATA) / name)},
                    "interval": {
                        "contig": "chrTest",
                        "start": 0,
                        "end": 60,
                        "assembly": "synthetic-v1",
                    },
                    "samples": ["SAMPLE2", "SAMPLE1"],
                },
            )
        )
    finally:
        await svc.aclose()
    a, b = res["data"]["records"]
    assert a["samples"]["SAMPLE1"]["genotype"]["text"] == "1|2"
    assert a["samples"]["SAMPLE1"]["format"]["DP"] == 12
    assert a["samples"]["SAMPLE2"]["genotype"] | {} == {
        **a["samples"]["SAMPLE2"]["genotype"],
        "text": "0/.",
        "alleles": [0, None],
        "phased": False,
    }
    assert b["samples"]["SAMPLE1"]["genotype"]["text"] == "1"
    assert b["samples"]["SAMPLE2"]["genotype"]["text"] == "0/1"
