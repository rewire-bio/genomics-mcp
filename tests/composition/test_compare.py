"""compare_samples through GenomicsService with real synthetic BAM/VCF/bigWig files."""

from __future__ import annotations

import pytest

from genomics_mcp.errors import ErrorCode
from genomics_mcp.registry import Operation
from genomics_mcp.result import ResultStatus

from .conftest import configured, iv, ref
from .fixtures import COHORT_ROWS, SECOND_ROWS, _vcf, other_base


def sites(body):
    return {r["pos"]: r for r in body["data"]["records"] if r["type"] == "variant_site"}


def calls(row):
    return {c["sample_key"]: c for c in row["calls"]}


def file_entry(body, fid):
    return next(f for f in body["data"]["files"] if f["id"] == fid)


async def test_contrasting_coverage_and_signal(make_service, fx, spy):
    files = [ref(fx.deep_bam), ref(fx.shallow_bam)]
    if fx.bigwig is not None:
        files.append(ref(fx.bigwig))
    res = await make_service().call(
        Operation.COMPARE_SAMPLES, {"interval": iv(100, 200), "files": files}
    )
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.OK, body["errors"]
    deep, shallow = fx.depth("deep", 100, 200), fx.depth("shallow", 100, 200)
    assert file_entry(body, "f0")["summary"]["mean"] == sum(deep) / 100
    assert file_entry(body, "f1")["summary"]["mean"] == sum(shallow) / 100
    assert file_entry(body, "f0")["sample_links"] == []  # nothing invented from @RG SM
    data = body["data"]
    assert data["normalization"]["library_size_normalized"] is False
    assert "not association" in data["interpretation"]
    bins = [r for r in data["records"] if r["type"] == "bin"]
    assert len(bins) == data["bins"]["count"] == 20
    first = bins[0]
    assert (first["start"], first["end"]) == (100, 105)
    assert first["values"]["f0"]["mean"] == sum(deep[:5]) / 5
    assert first["values"]["f1"]["mean"] == sum(shallow[:5]) / 5
    if fx.bigwig is not None:
        assert first["values"]["f2"]["value"] == 2.0 and bins[-1]["values"]["f2"]["value"] == 4.0
    assert spy.requests == []


async def test_genotypes_missing_vs_reference_and_duplicate_names(make_service, fx, spy):
    res = await make_service().call(
        Operation.COMPARE_SAMPLES,
        {"interval": iv(100, 300), "files": [ref(fx.cohort_vcf), ref(fx.second_vcf)]},
    )
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.OK, body["errors"]
    data = body["data"]
    assert [s["key"] for s in data["samples"]] == ["f0:S1", "f0:S2", "f1:S1"]
    assert data["duplicate_sample_names"] == {"S1": ["f0:S1", "f1:S1"]}
    rows = sites(body)
    assert sorted(rows) == sorted(set(COHORT_ROWS) | set(SECOND_ROWS))

    c121 = calls(rows[121])
    assert c121["f0:S1"]["call_class"] == "heterozygous"
    assert c121["f0:S2"]["call_class"] == "homozygous_reference"
    assert c121["f1:S1"]["call_class"] == "homozygous_alternate"
    assert rows[121]["summary"]["distinct_called_allele_sets"] == 3

    c131 = calls(rows[131])
    assert c131["f0:S1"]["genotype"]["phased"] is True
    assert c131["f0:S2"]["observation"] == "missing_call"
    assert c131["f0:S2"]["genotype"]["alleles"] == [None, None]
    assert c131["f1:S1"]["observation"] == "no_record"  # not homozygous reference
    assert "call_class" not in c131["f1:S1"]

    c141 = calls(rows[141])
    assert c141["f0:S1"]["genotype"]["alleles"] == [1, 2]
    assert len(rows[141]["file_records"]["f0"][0]["alts"]) == 2
    assert c141["f0:S2"]["genotype"]["phased"] is True

    c151 = calls(rows[151])
    assert c151["f0:S1"]["call_class"] == "haploid_alternate"
    assert c151["f0:S2"]["call_class"] == "haploid_reference"
    assert c151["f0:S1"]["genotype"]["ploidy"] == 1

    c201 = calls(rows[201])
    assert c201["f0:S1"]["observation"] == "no_record"
    assert c201["f0:S2"]["observation"] == "no_record"
    assert spy.requests == []


async def test_sample_order_keeps_each_genotype_with_its_sample(make_service, fx):
    """Reversed sample order must not move GT text/phasing between samples."""
    svc = make_service()
    body_fwd = (
        await svc.call(
            Operation.COMPARE_SAMPLES,
            {"interval": iv(100, 200), "files": [ref(fx.cohort_vcf)], "samples": ["S1", "S2"]},
        )
    ).model_dump(mode="json")
    body_rev = (
        await svc.call(
            Operation.COMPARE_SAMPLES,
            {"interval": iv(100, 200), "files": [ref(fx.cohort_vcf)], "samples": ["S2", "S1"]},
        )
    ).model_dump(mode="json")
    assert [s["sample"] for s in body_rev["data"]["samples"]] == ["S2", "S1"]
    fields = ("text", "alleles", "ploidy", "phased", "missing")
    for pos, (gt1, gt2) in COHORT_ROWS.items():
        fwd, rev = calls(sites(body_fwd)[pos]), calls(sites(body_rev)[pos])
        for key, expected in (("f0:S1", gt1), ("f0:S2", gt2)):
            a, b = fwd[key]["genotype"], rev[key]["genotype"]
            assert a["text"] == expected, (pos, key, a)
            assert {f: a[f] for f in fields} == {f: b[f] for f in fields}
            assert a["phased"] == ("|" in expected)
            idx = [None if x == "." else int(x) for x in expected.replace("|", "/").split("/")]
            assert a["alleles"] == idx


async def test_samples_filter_and_absence(make_service, fx):
    res = await make_service().call(
        Operation.COMPARE_SAMPLES,
        {
            "interval": iv(100, 300),
            "files": [ref(fx.cohort_vcf), ref(fx.second_vcf)],
            "samples": ["S2", "NOPE"],
        },
    )
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.OK, (body["error"], body["errors"])
    data = body["data"]
    assert [s["key"] for s in data["samples"]] == ["f0:S2"]
    assert data["samples_requested_not_found"] == ["NOPE"]
    assert file_entry(body, "f1")["requested_samples_absent"] == ["S2", "NOPE"]
    assert file_entry(body, "f0")["requested_samples_absent"] == ["NOPE"]


async def test_capped_samples_are_omitted_not_reported_missing(make_service, data_root):
    names = [f"S{i}" for i in range(1, 52)]
    vcf = _vcf(data_root / "wide.vcf", names, [(121, other_base(121), "GT", ["0/1"] * 51)])
    res = await make_service().call(
        Operation.COMPARE_SAMPLES,
        {"interval": iv(100, 200), "files": [ref(vcf)], "samples": names},
    )
    body = res.model_dump(mode="json")
    data = body["data"]
    assert len(data["samples"]) == 50
    assert data["samples_requested_not_found"] == []
    entry = file_entry(body, "f0")
    assert entry["samples_total"] == 51 and entry["samples_omitted"] == 1


async def test_failed_and_disabled_files_are_unavailable_not_zero(
    make_service, fx, tmp_path, data_root
):
    cfg = configured(tmp_path, data_root, sources={"ena": {"enabled": False}})
    calls_made = []

    def extra(reg):
        for op, fmt in ((Operation.GET_VARIANTS, "vcf"), (Operation.GET_COVERAGE, "bam")):
            spec = reg._handlers[op][fmt]

            async def counted(req, ctx, _orig=spec.handler):
                calls_made.append(req.file.uri)
                return await _orig(req, ctx)

            reg._handlers[op][fmt] = spec.__class__(op, fmt, counted, "t")

    res = await make_service(cfg, extra).call(
        Operation.COMPARE_SAMPLES,
        {
            "interval": iv(100, 200),
            "files": [
                ref(fx.cohort_vcf),
                ref(fx.second_vcf, source="ena"),
                ref(data_root / "missing.vcf.gz"),
                ref(fx.deep_bam),
                ref(fx.shallow_bam, source="ena"),
            ],
        },
    )
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.PARTIAL
    assert file_entry(body, "f1")["status"] == "disabled"
    assert file_entry(body, "f4")["status"] == "disabled"
    assert file_entry(body, "f2")["status"] == "not_found"
    assert str(fx.second_vcf) not in calls_made and str(fx.shallow_bam) not in calls_made
    for row in body["data"]["records"]:
        if row["type"] == "variant_site":
            assert row["unavailable_files"] == ["f1", "f2"]
            assert not any(c["file"] in ("f1", "f2") for c in row["calls"])
        else:
            assert row["unavailable_files"] == ["f4"]
            assert set(row["values"]) == {"f3"}


async def test_wrong_assembly_and_formats_rejected_before_work(make_service, fx):
    svc = make_service()
    res = await svc.call(
        Operation.COMPARE_SAMPLES,
        {
            "interval": iv(100, 200),
            "files": [ref(fx.cohort_vcf), ref(fx.deep_bam, assembly="hg19")],
        },
    )
    assert res.error.code == ErrorCode.INVALID_INPUT and "no liftover" in res.error.message
    res = await svc.call(
        Operation.COMPARE_SAMPLES, {"interval": iv(100, 200), "files": [ref(fx.fasta)]}
    )
    assert res.error.code == ErrorCode.INVALID_INPUT


async def test_record_cap_reports_truncation(make_service, fx):
    res = await make_service().call(
        Operation.COMPARE_SAMPLES,
        {"interval": iv(100, 300), "files": [ref(fx.cohort_vcf)], "max_records": 2},
    )
    body = res.model_dump(mode="json")
    assert len(body["data"]["records"]) <= 2
    assert body["truncation"] is not None
    # sites beyond the truncated file's last record are not_read, never no_record
    for row in sites(body).values():
        for c in row["calls"]:
            assert c["observation"] in ("called", "missing_call", "not_read")


@pytest.mark.parametrize("n", [3])
async def test_response_cap_keeps_valid_envelope(make_service, fx, n):
    res = await make_service().call(
        Operation.COMPARE_SAMPLES,
        {
            "interval": iv(100, 300),
            "files": [ref(fx.cohort_vcf), ref(fx.second_vcf), ref(fx.deep_bam)],
            "max_response_bytes": 8192,
        },
    )
    assert res.json_size() <= 8192
    assert res.status is not ResultStatus.ERROR
    assert res.data["records"] and res.truncation.reason == "max_response_bytes"
