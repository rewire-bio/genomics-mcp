"""Local storage: allowed roots after symlink resolution, URI forms, observed indexes, listings."""

from __future__ import annotations

import os
import shutil

import pytest
from gm_test_support import envelope, iv, make_settings

from genomics_mcp.errors import InvalidInputError, NotFoundError, UnauthorizedError
from genomics_mcp.models import FileRef
from genomics_mcp.service import GenomicsService
from genomics_mcp.storage.local import resolve_local
from genomics_mcp.storage.uris import local_path_from_uri


def test_file_uri_forms():
    assert local_path_from_uri("/a/b.bam") == "/a/b.bam"
    assert local_path_from_uri("file:///a/b%20c.bam") == "/a/b c.bam"
    assert local_path_from_uri("file://localhost/a/b.bam") == "/a/b.bam"
    with pytest.raises(InvalidInputError, match="localhost"):
        local_path_from_uri("file://fileserver/share/b.bam")
    with pytest.raises(InvalidInputError, match="query"):
        local_path_from_uri("file:///a/b.bam?x=1")


async def test_roots_enforced_after_symlink_resolution(tmp_path, golden):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(golden["bam"], outside / "secret.bam")
    os.symlink(outside / "secret.bam", root / "link.bam")
    settings = make_settings(tmp_path, [root])
    with pytest.raises(UnauthorizedError):
        await resolve_local(settings, FileRef(uri=str(root / "link.bam")))
    with pytest.raises(UnauthorizedError):
        await resolve_local(settings, FileRef(uri=str(root / ".." / "outside" / "secret.bam")))
    with pytest.raises(NotFoundError):
        await resolve_local(settings, FileRef(uri=str(root / "missing.bam")))


async def test_observed_sidecar_and_explicit_index(tmp_path, golden):
    settings = make_settings(tmp_path, [golden["root"]])
    r = await resolve_local(settings, FileRef(uri=str(golden["bam"])))
    assert r.index_state == "present" and r.index_kind == "bai"
    assert r.index_open_uri == str(golden["bam"]) + ".bai"
    assert r.readiness.state == "ready" and r.compression == "bgzf" and r.content_kind == "bam"
    # CSI next to the VCF is found; a TBI given explicitly for a BAM is refused with a reason.
    v = await resolve_local(settings, FileRef(uri=str(golden["vcf"])))
    assert v.index_kind == "csi"
    with pytest.raises(InvalidInputError, match="index content is tbi"):
        await resolve_local(
            settings,
            FileRef(uri=str(golden["bam"]), index_uri=str(golden["vcf_tbi"]) + ".tbi"),
        )


async def test_missing_and_corrupt_sidecar_are_distinct(tmp_path, golden):
    root = tmp_path / "d"
    root.mkdir()
    shutil.copy(golden["bam"], root / "noidx.bam")
    shutil.copy(golden["bam"], root / "bad.bam")
    (root / "bad.bam.bai").write_bytes(b"not an index at all")
    settings = make_settings(tmp_path, [root])
    missing = await resolve_local(settings, FileRef(uri=str(root / "noidx.bam")))
    assert missing.index_state == "missing" and missing.readiness.state == "index_required"
    corrupt = await resolve_local(settings, FileRef(uri=str(root / "bad.bam")))
    assert corrupt.index_state == "corrupt" and corrupt.readiness.state == "index_required"

    svc = GenomicsService(settings)
    try:
        for name, needle in (("noidx.bam", "no index"), ("bad.bam", "not valid")):
            res = envelope(
                await svc.call(
                    "get_reads", {"file": {"uri": str(root / name)}, "interval": iv("chrG", 0, 10)}
                )
            )
            assert res["status"] == "error"
            assert res["error"]["code"] == "preparation_required"
            assert needle in res["error"]["message"]
    finally:
        await svc.aclose()


async def test_content_mismatch_is_invalid_not_empty(tmp_path, golden):
    root = tmp_path / "d"
    root.mkdir()
    (root / "fake.bam").write_text("this is text, not BAM\n")
    settings = make_settings(tmp_path, [root])
    with pytest.raises(InvalidInputError, match="looks like text"):
        await resolve_local(settings, FileRef(uri=str(root / "fake.bam")))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
async def test_permission_denied_is_unauthorized(tmp_path, golden):
    root = tmp_path / "d"
    root.mkdir()
    shutil.copy(golden["bam"], root / "locked.bam")
    os.chmod(root / "locked.bam", 0)
    try:
        settings = make_settings(tmp_path, [root])
        with pytest.raises(UnauthorizedError):
            await resolve_local(settings, FileRef(uri=str(root / "locked.bam")))
    finally:
        os.chmod(root / "locked.bam", 0o644)


async def test_listing_states_and_pairs(tmp_path, golden):
    root = tmp_path / "list"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(golden["bam"], root / "a.bam")
    shutil.copy(str(golden["bam"]) + ".bai", root / "a.bam.bai")
    shutil.copy(golden["vcf_plain"], root / "plain.vcf")
    (root / "fake.cram").write_text("text\n")
    (root / "notes.txt").write_text("x")
    (root / "orphan.bai").write_bytes(b"BAI\x01")
    os.symlink(root / "gone.bam", root / "broken.bam")
    shutil.copy(golden["bam"], outside / "o.bam")
    os.symlink(outside / "o.bam", root / "escape.bam")
    (root / "sub").mkdir()
    settings = make_settings(tmp_path, [root])
    svc = GenomicsService(settings)
    try:
        res = envelope(await svc.call("list_files", {"source": "local", "accession": str(root)}))
    finally:
        await svc.aclose()
    assert res["status"] == "ok"
    by_name = {os.path.basename(r["file"]["uri"]): r for r in res["data"]["records"]}
    assert by_name["a.bam"]["state"] == "available"
    assert by_name["a.bam"]["index"]["state"] == "present"
    assert by_name["a.bam"]["file"]["index_uri"].endswith("a.bam.bai")
    assert by_name["plain.vcf"]["file"]["readiness"]["state"] == "not_locus_ready"
    assert by_name["fake.cram"]["state"] == "corrupt"
    assert by_name["notes.txt"]["state"] == "unsupported"
    assert by_name["broken.bam"]["state"] == "missing"
    assert by_name["escape.bam"]["state"] == "denied"
    assert res["data"]["unpaired_indexes"] == [str(root / "orphan.bai")]
    assert res["data"]["subdirectories"] == [str(root / "sub")]


async def test_listing_pages_with_cursor(tmp_path, golden):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(5):
        shutil.copy(golden["bed"], root / f"f{i}.bed.gz")
    svc = GenomicsService(make_settings(tmp_path, [root]))
    try:
        first = envelope(
            await svc.call(
                "list_files", {"source": "local", "accession": str(root), "max_records": 2}
            )
        )
        assert len(first["data"]["records"]) == 2
        assert first["truncation"]["reason"] == "max_records"
        cursor = first["data"]["next_cursor"]
        second = envelope(
            await svc.call(
                "list_files",
                {"source": "local", "accession": str(root), "max_records": 2, "cursor": cursor},
            )
        )
        names = [r["file"]["uri"] for r in first["data"]["records"] + second["data"]["records"]]
        assert len(set(names)) == 4
    finally:
        await svc.aclose()


async def test_listing_outside_roots_refused(tmp_path, golden):
    svc = GenomicsService(make_settings(tmp_path, [golden["root"]]))
    try:
        res = envelope(await svc.call("list_files", {"source": "local", "accession": "/etc"}))
    finally:
        await svc.aclose()
    assert res["error"]["code"] == "unauthorized"
