"""FASTA sequence and tabix BED/GFF3/GTF features: coordinates, native columns, readiness."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from gm_test_support import TABIX, envelope, iv


async def test_sequence_plain_bgzf_and_remote(service, golden, fixture_server):
    want = golden["seqs"]["chrG"][100:160]
    for uri in (str(golden["ref"]), fixture_server.url("ref.fa")):
        res = envelope(
            await service.call(
                "get_sequence", {"file": {"uri": uri}, "interval": iv("chrG", 100, 160)}
            )
        )
        assert res["status"] == "ok", res.get("error")
        rec = res["data"]["records"][0]
        assert (
            rec["sequence"] == want
            and rec["start"] == 100
            and rec["end"] == 160
            and rec["length"] == 60
        )
        assert res["data"]["assembly"]["status"] == "caller_asserted"
    masked = {"uri": str(golden["fasta_masked"])}
    bgzf = {"uri": str(golden["fasta_bgzf"])}
    for f in (masked, bgzf):
        res = envelope(
            await service.call("get_sequence", {"file": f, "interval": iv("m1", 2, 14, "x")})
        )
        assert res["data"]["records"][0]["sequence"] == "GTacgtNNNNAC"  # case preserved
    remote_bgzf = envelope(
        await service.call(
            "get_sequence",
            {"file": {"uri": fixture_server.url("masked.fa.gz")}, "interval": iv("m1", 0, 20, "x")},
        )
    )
    assert remote_bgzf["data"]["records"][0]["sequence"] == "ACGTacgtNNNNACGTGGGG"


async def test_sequence_boundaries_and_readiness(service, golden, tmp_path):
    f = {"uri": str(golden["fasta_masked"])}
    end = envelope(await service.call("get_sequence", {"file": f, "interval": iv("m2", 0, 4, "x")}))
    assert end["data"]["records"][0]["sequence"] == "TTTT"
    beyond = envelope(
        await service.call("get_sequence", {"file": f, "interval": iv("m2", 0, 5, "x")})
    )
    assert beyond["error"]["code"] == "invalid_input"
    missing = envelope(
        await service.call("get_sequence", {"file": f, "interval": iv("chr1", 0, 5, "x")})
    )
    assert missing["error"]["code"] == "not_found"
    unindexed = envelope(
        await service.call(
            "get_sequence",
            {"file": {"uri": str(golden["fasta_unindexed"])}, "interval": iv("m1", 0, 4, "x")},
        )
    )
    assert unindexed["error"]["code"] == "preparation_required"
    assert not (golden["root"] / "unindexed.fa.fai").exists()  # never indexed in place
    gz = envelope(
        await service.call(
            "get_sequence",
            {"file": {"uri": str(golden["fasta_gzip"])}, "interval": iv("m1", 0, 4, "x")},
        )
    )
    assert (
        gz["error"]["code"] == "preparation_required" and "ordinary gzip" in gz["error"]["message"]
    )
    no_gzi = golden["root"] / "nogzi"
    no_gzi.mkdir(exist_ok=True)
    shutil.copy(golden["fasta_bgzf"], no_gzi / "m.fa.gz")
    shutil.copy(str(golden["fasta_bgzf"]) + ".fai", no_gzi / "m.fa.gz.fai")
    res = envelope(
        await service.call(
            "get_sequence",
            {"file": {"uri": str(no_gzi / "m.fa.gz")}, "interval": iv("m1", 0, 4, "x")},
        )
    )
    assert res["error"]["code"] == "preparation_required" and ".gzi" in res["error"]["message"]


def tabix_oracle(path, reg):
    out = subprocess.run([TABIX, str(path), reg], check=True, capture_output=True, text=True).stdout
    return out.splitlines()


@pytest.mark.skipif(TABIX is None, reason="tabix not installed")
@pytest.mark.parametrize(
    ("key", "start", "end"),
    [("bed", 14, 16), ("gff3", 14, 16), ("gtf", 10, 11), ("bed", 20, 100), ("gff3", 100, 101)],
)
async def test_features_match_tabix_and_convert_coordinates(service, golden, key, start, end):
    res = envelope(
        await service.call(
            "get_features", {"file": {"uri": str(golden[key])}, "interval": iv("chrG", start, end)}
        )
    )
    assert res["status"] == "ok", res.get("error")
    # tabix regions are 1-based closed.
    assert [r["native_line"] for r in res["data"]["records"]] == tabix_oracle(
        golden[key], f"chrG:{start + 1}-{end}"
    )
    for r in res["data"]["records"]:
        assert r["start"] < end and r["end"] > start
        if key != "bed":
            assert r["start"] == r["native_start"] - 1 and r["end"] == r["native_end"]


async def test_gff3_and_gtf_attributes_are_preserved(service, golden):
    gff = envelope(
        await service.call(
            "get_features", {"file": {"uri": str(golden["gff3"])}, "interval": iv("chrG", 0, 30)}
        )
    )
    gene = gff["data"]["records"][0]
    assert gene["type"] == "gene" and gene["start"] == 10 and gene["end"] == 20
    assert gene["attributes"] == {"ID": ["gene1"], "Name": ["G;1"], "Alias": ["a", "b"]}
    assert gene["native_attributes"] == "ID=gene1;Name=G%3B1;Alias=a,b"
    exon = gff["data"]["records"][1]
    assert (exon["score"], exon["strand"], exon["phase"]) == ("0.5", "-", "0")
    gtf = envelope(
        await service.call(
            "get_features",
            {
                "file": {"uri": str(golden["gtf"])},
                "interval": iv("chrG", 0, 100),
                "feature_types": ["exon"],
            },
        )
    )
    assert [r["type"] for r in gtf["data"]["records"]] == ["exon"]
    assert gtf["data"]["records"][0]["attributes"] == {
        "gene_id": ["g1"],
        "transcript_id": ["t1"],
        "tag": ["a", "b"],
    }


async def test_bed_fields_and_errors(service, golden):
    res = envelope(
        await service.call(
            "get_features", {"file": {"uri": str(golden["bed"])}, "interval": iv("chrH", 0, 1)}
        )
    )
    assert res["data"]["records"][0]["fields"] == {
        "chrom": "chrH",
        "chromStart": "0",
        "chromEnd": "10",
        "name": "startH",
    }
    typed = envelope(
        await service.call(
            "get_features",
            {
                "file": {"uri": str(golden["bed"])},
                "interval": iv("chrG", 0, 10),
                "feature_types": ["x"],
            },
        )
    )
    assert typed["error"]["code"] == "invalid_input"
    for key in ("bed_plain", "bed_gzip"):
        r = envelope(
            await service.call(
                "get_features", {"file": {"uri": str(golden[key])}, "interval": iv("chrG", 0, 10)}
            )
        )
        assert r["error"]["code"] == "preparation_required", key
    unknown = envelope(
        await service.call(
            "get_features", {"file": {"uri": str(golden["bed"])}, "interval": iv("1", 0, 10)}
        )
    )
    assert unknown["error"]["code"] == "not_found"
