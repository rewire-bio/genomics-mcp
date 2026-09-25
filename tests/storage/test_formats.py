"""Sniffing, index compatibility, readiness rules and the small core extensions E2/E3 added."""

from __future__ import annotations

import gzip

import pysam
import pytest

from genomics_mcp.config import ConfigError, load_settings
from genomics_mcp.models import Compression, FileFormat
from genomics_mcp.requests import FetchFileRequest, ListFilesRequest
from genomics_mcp.storage.formats import check_index, readiness, sidecar_names, sniff
from genomics_mcp.storage.http import parse_content_range, signed_url_expiry


def test_sniff_distinguishes_bgzf_from_gzip(golden):
    assert sniff(golden["bam"].read_bytes()[:65536]).kind == "bam"
    assert sniff(golden["vcf"].read_bytes()).compression is Compression.BGZF
    s = sniff(golden["vcf_gzip"].read_bytes())
    assert s.compression is Compression.GZIP and s.kind == "vcf"
    assert sniff(golden["cram"].read_bytes()[:100]).kind == "cram"
    assert sniff(golden["bigwig"].read_bytes()[:8]).kind == "bigwig"
    assert sniff(golden["bigbed"].read_bytes()[:8]).kind == "bigbed"
    assert sniff(b"").kind == "empty"


def test_index_checks(golden):
    bai = (golden["root"] / "golden.bam.bai").read_bytes()
    assert check_index(FileFormat.BAM, bai).ok
    tbi = (golden["root"] / "short.vcf.gz.tbi").read_bytes()
    assert check_index(FileFormat.VCF, tbi).ok
    assert not check_index(FileFormat.BAM, tbi).ok
    bed_tbi = (golden["root"] / "features.bed.gz.tbi").read_bytes()
    assert check_index(FileFormat.BED, bed_tbi).ok
    assert "VCF preset" in check_index(FileFormat.VCF, bed_tbi).problem
    gff_tbi = (golden["root"] / "features.gff3.gz.tbi").read_bytes()
    assert check_index(FileFormat.GFF3, gff_tbi).ok and not check_index(FileFormat.BED, gff_tbi).ok
    crai = (golden["root"] / "cram.cram.crai").read_bytes()
    assert check_index(FileFormat.CRAM, crai).ok
    fai = (golden["root"] / "ref.fa.fai").read_bytes()
    assert check_index(FileFormat.FASTA, fai).ok
    assert not check_index(FileFormat.FASTA, gzip.compress(fai)).ok


def test_sidecar_candidates_are_only_candidates():
    assert sidecar_names("s3://b/x.bam", FileFormat.BAM) == [
        ("s3://b/x.bam.bai", "bai"),
        ("s3://b/x.bam.csi", "csi"),
        ("s3://b/x.bai", "bai"),
    ]
    assert sidecar_names("x.bw", FileFormat.BIGWIG) == []


def test_readiness_rules():
    r = readiness(
        FileFormat.BAM, Compression.BGZF, index_state="present", range_capable=False, local=False
    )
    assert r.state == "download_required"
    r = readiness(
        FileFormat.BED, Compression.GZIP, index_state="missing", range_capable=True, local=True
    )
    assert r.state == "not_locus_ready" and "ordinary gzip" in r.reasons[0]
    r = readiness(
        FileFormat.FASTQ, Compression.GZIP, index_state="not_needed", range_capable=True, local=True
    )
    assert r.state == "not_locus_ready"
    r = readiness(FileFormat.CRAM, None, index_state="present", range_capable=True, local=True)
    assert r.state == "ready" and "MD5" in r.reasons[-1]
    r = readiness(None, None, index_state="not_needed", range_capable=True, local=True)
    assert r.state == "unsupported"


def test_content_range_and_expiry():
    assert parse_content_range("bytes 0-0/1234") == (0, 0, 1234)
    assert parse_content_range("bytes 5-9/*") == (5, 9, None)
    assert parse_content_range("bytes x") is None
    exp = signed_url_expiry("https://h/p?X-Amz-Date=20260101T000000Z&X-Amz-Expires=900")
    assert exp is not None and exp.isoformat() == "2026-01-01T00:15:00+00:00"


def test_core_extensions_are_backward_compatible():
    assert FetchFileRequest(file={"uri": "/a.bam"}).prepare is False
    assert ListFilesRequest(source="s3", accession="s3://b/").storage_profile is None
    s = load_settings(env={}, overrides={"storage": {"local_network_hosts": ["127.0.0.1"]}})
    assert s.storage.local_network_hosts == ["127.0.0.1"]
    assert load_settings(env={}).storage.local_network_hosts == []
    with pytest.raises(ConfigError):
        load_settings(env={}, overrides={"storage": {"local_network_host": ["x"]}})


def test_pysam_versions_are_pinned():
    assert pysam.__version__ == "0.24.1"
