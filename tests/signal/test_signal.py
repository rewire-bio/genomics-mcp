"""bigWig signal and bigBed features: exact summaries, bins, nulls, schema, local and remote."""

from __future__ import annotations

import json
import time

import pyBigWig
import pytest
from gm_test_support import NETWORK, envelope, iv, make_settings

from genomics_mcp.service import GenomicsService
from genomics_mcp.signal.native_signal import bin_edges, parse_autosql


def test_pybigwig_build_has_remote_support():
    # Compiled support alone is not proof that remote sources work; see the HTTP tests below.
    assert pyBigWig.remote


async def test_exact_mean_and_intervals(service, golden):
    res = envelope(
        await service.call(
            "get_signal", {"file": {"uri": str(golden["bigwig"])}, "interval": iv("chrG", 50, 160)}
        )
    )
    assert res["status"] == "ok"
    # [50,100)=1.0, [100,150)=3.0, [150,160)=0.5 -> exact base-weighted mean.
    assert res["data"]["summary"] == {
        "type": "mean",
        "value": (50 * 1 + 50 * 3 + 10 * 0.5) / 110,
        "exact": True,
    }
    assert res["data"]["records"] == [
        {"start": 50, "end": 100, "value": 1.0},
        {"start": 100, "end": 150, "value": 3.0},
        {"start": 150, "end": 160, "value": 0.5},
    ]
    assert res["data"]["assembly"]["status"] == "caller_asserted"


async def test_bins_have_libbigwig_edges_and_null_gaps(service, golden):
    res = envelope(
        await service.call(
            "get_signal",
            {
                "file": {"uri": str(golden["bigwig"])},
                "interval": iv("chrG", 150, 1004),
                "bins": 7,
                "summary": "max",
            },
        )
    )
    recs = res["data"]["records"]
    assert [(r["start"], r["end"]) for r in recs] == bin_edges(150, 1004, 7)
    with pyBigWig.open(str(golden["bigwig"])) as bw:
        for r in recs:
            expected = bw.stats("chrG", r["start"], r["end"], type="max", exact=True)[0]
            assert r["value"] == expected
    assert any(r["value"] is None for r in recs)  # [160,1000) has no data
    text = json.dumps(res)
    assert "NaN" not in text


async def test_no_data_is_null_not_zero(service, golden):
    res = envelope(
        await service.call(
            "get_signal", {"file": {"uri": str(golden["bigwig"])}, "interval": iv("chrH", 0, 100)}
        )
    )
    assert res["data"]["summary"]["value"] is None and res["data"]["records"] == []


async def test_signal_truncation_and_limits(service, golden):
    f = {"uri": str(golden["bigwig"])}
    res = envelope(
        await service.call(
            "get_signal", {"file": f, "interval": iv("chrG", 0, 2000), "max_records": 2}
        )
    )
    assert len(res["data"]["records"]) == 2 and res["truncation"]["available"] == 7
    # The summary is over the whole interval, not the returned records.
    with pyBigWig.open(str(golden["bigwig"])) as bw:
        assert res["data"]["summary"]["value"] == bw.stats("chrG", 0, 2000, exact=True)[0]
    too_many = envelope(
        await service.call(
            "get_signal", {"file": f, "interval": iv("chrG", 0, 100), "bins": 50, "max_records": 10}
        )
    )
    assert too_many["error"]["code"] == "invalid_input"
    beyond = envelope(await service.call("get_signal", {"file": f, "interval": iv("chrH", 0, 501)}))
    assert beyond["error"]["code"] == "invalid_input"


async def test_bigbed_features_with_autosql_names(service, golden):
    res = envelope(
        await service.call(
            "get_features",
            {"file": {"uri": str(golden["bigbed"])}, "interval": iv("chrG", 0, 3000)},
        )
    )
    assert res["status"] == "ok", res.get("error")
    assert [f["name"] for f in res["data"]["schema"]][:6] == [
        "chrom",
        "chromStart",
        "chromEnd",
        "name",
        "score",
        "strand",
    ]
    first = res["data"]["records"][0]
    assert first == {
        "contig": "chrG",
        "start": 10,
        "end": 20,
        "fields": {"name": "featA", "score": "0", "strand": "+"},
        "raw": "featA\t0\t+",
    }
    tail = envelope(
        await service.call(
            "get_features",
            {"file": {"uri": str(golden["bigbed"])}, "interval": iv("chrG", 2999, 3000)},
        )
    )
    assert [r["fields"]["name"] for r in tail["data"]["records"]] == ["tail"]
    typed = envelope(
        await service.call(
            "get_features",
            {
                "file": {"uri": str(golden["bigbed"])},
                "interval": iv("chrG", 0, 30),
                "feature_types": ["gene"],
            },
        )
    )
    assert typed["error"]["code"] == "invalid_input"


def test_autosql_parser():
    fields = parse_autosql(
        'table x\n"d"\n(\nstring chrom; "c"\nuint chromStart; "s"\nfloat[3] v; "vals"\n)\n'
    )
    assert [(f["type"], f["name"]) for f in fields] == [
        ("string", "chrom"),
        ("uint", "chromStart"),
        ("float[3]", "v"),
    ]


async def test_remote_bigwig_and_bigbed_over_http_ranges(service, golden, fixture_server):
    for op, name, interval in (
        ("get_signal", "signal.bw", iv("chrG", 0, 2000)),
        ("get_features", "features.bb", iv("chrG", 0, 3000)),
    ):
        local = envelope(
            await service.call(
                op, {"file": {"uri": str(golden["root"] / name)}, "interval": interval}
            )
        )
        remote = envelope(
            await service.call(
                op, {"file": {"uri": fixture_server.url(name)}, "interval": interval}
            )
        )
        assert remote["status"] == "ok", remote.get("error")
        assert remote["data"]["records"] == local["data"]["records"]
        redirected = envelope(
            await service.call(
                op, {"file": {"uri": fixture_server.url("redirect/" + name)}, "interval": interval}
            )
        )
        assert redirected["data"]["records"] == local["data"]["records"]
        assert redirected["provenance"][0]["url"].endswith("redirect/" + name)


async def test_remote_without_range_support_is_not_downloaded(service, fixture_server):
    res = envelope(
        await service.call(
            "get_signal",
            {
                "file": {"uri": fixture_server.url("ignore-range/signal.bw")},
                "interval": iv("chrG", 0, 10),
            },
        )
    )
    assert res["error"]["code"] == "preparation_required"
    assert res["error"]["details"]["readiness"] == "download_required"


network = pytest.mark.skipif(
    not NETWORK, reason="set GENOMICS_MCP_NETWORK_TESTS=1 for live sources"
)
ENCODE_BW = "https://www.encodeproject.org/files/ENCFF792QDS/@@download/ENCFF792QDS.bigWig"
ENCODE_BB = "https://www.encodeproject.org/files/ENCFF001JBR/@@download/ENCFF001JBR.bigBed"


@network
async def test_live_encode_bigwig_exact_mean(tmp_path):
    svc = GenomicsService(make_settings(tmp_path, []))
    try:
        started = time.monotonic()
        res = envelope(
            await svc.call(
                "get_signal",
                {
                    "file": {"uri": ENCODE_BW, "visibility": "public"},
                    "interval": {
                        "contig": "chr1",
                        "start": 1_000_000,
                        "end": 1_001_000,
                        "assembly": "GRCh38",
                    },
                    "max_records": 5,
                },
            )
        )
        elapsed = time.monotonic() - started
    finally:
        await svc.aclose()
    assert res["status"] == "ok", res.get("error")
    assert res["data"]["summary"]["value"] == pytest.approx(26.361254017233847, rel=0, abs=1e-12)
    assert [r["value"] for r in res["data"]["records"][:3]] == pytest.approx(
        [10.124589920043945, 7.800464630126953, 5.335175037384033]
    )
    assert res["data"]["contig_length"] == 248956422
    assert elapsed < 30
    # Only the original URL is reported; the signed redirect target stays internal.
    assert res["provenance"][0]["url"] == ENCODE_BW
    assert "X-Amz" not in json.dumps(res)


@network
async def test_live_encode_bigbed(tmp_path):
    svc = GenomicsService(make_settings(tmp_path, []))
    try:
        res = envelope(
            await svc.call(
                "get_features",
                {
                    "file": {"uri": ENCODE_BB, "visibility": "public"},
                    "interval": {
                        "contig": "chr1",
                        "start": 3_000_000,
                        "end": 3_600_000,
                        "assembly": "mm9",
                    },
                    "max_records": 3,
                },
            )
        )
    finally:
        await svc.aclose()
    assert res["status"] == "ok", res.get("error")
    assert len(res["data"]["records"]) == 3 and res["truncation"]["reason"] == "max_records"
    assert [f["name"] for f in res["data"]["schema"]][:3] == ["chrom", "chromStart", "chromEnd"]
    assert res["data"]["assembly"]["status"] == "caller_asserted"


async def test_build_without_remote_support_is_explicit(service, fixture_server, monkeypatch):
    import genomics_mcp.signal as sig

    monkeypatch.setattr(sig, "REMOTE_CAPABLE", False)
    res = envelope(
        await service.call(
            "get_signal",
            {"file": {"uri": fixture_server.url("signal.bw")}, "interval": iv("chrG", 0, 10)},
        )
    )
    assert res["error"]["code"] == "unsupported" and "libcurl" in res["error"]["message"]
