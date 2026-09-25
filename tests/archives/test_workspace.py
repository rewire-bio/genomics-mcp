"""Untrusted names never escape the artifact workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from genomics_mcp.archives._common.errors import InvalidInputError
from genomics_mcp.archives._common.redact import redact, redact_url
from genomics_mcp.archives._common.workspace import artifact_path, safe_name, write_atomic


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".",
        "../x",
        "a/../../b",
        "/etc/passwd",
        "..\\..\\win",
        "",
        ".hidden",
        "x" * 400,
        "ok\x00name",
    ],
)
def test_safe_name_is_one_plain_component(name):
    out = safe_name(name)
    assert (
        out
        and "/" not in out
        and "\\" not in out
        and out not in (".", "..")
        and not out.startswith(".")
    )
    assert len(out) <= 140


def test_safe_name_keeps_clean_names_and_disambiguates_changed_ones():
    assert safe_name("HG00096.bam") == "HG00096.bam"
    assert safe_name("../HG00096.bam") != safe_name("..HG00096.bam")


@pytest.mark.parametrize("name", ["../../escaped.bam", "/abs.bam", "sub/dir.bam"])
def test_artifact_path_stays_inside(tmp_path: Path, name):
    ws = tmp_path / "ws"
    p = artifact_path(ws, name)
    write_atomic(p, b"x")
    assert p.parent == ws.resolve() and p.read_bytes() == b"x"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["ws"]
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_symlink_target_refused(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.bam").symlink_to(tmp_path / "outside")
    with pytest.raises(InvalidInputError):
        artifact_path(ws, "a.bam")


def test_signed_url_and_token_redaction():
    signed = redact_url(
        "https://h/x?AWSAccessKeyId=AKID&Signature=abcsig&x-amz-security-token=tok&Expires=1"
    )
    assert "abcsig" not in signed and "tok" not in signed and "AKID" not in signed
    assert "sig=secretvalue" not in redact_url("https://h/x?sv=1&sig=secretvalue")
    assert redact_url("data:base64,QUJD") == "data:[inline]"
    assert "eyJhbGciOiJIUzI1NiJ9" not in redact(
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXZhbA"
    )
