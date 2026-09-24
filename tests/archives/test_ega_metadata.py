"""EGA metadata shapes, paging, relationships, auth redaction and whole-file download bounds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from archive_helpers import Router, json_response
from pydantic import SecretStr

from genomics_mcp.archives._common.errors import (
    BudgetExceededError,
    InvalidInputError,
    NotFoundError,
    UnauthorizedError,
    UnsupportedError,
    UpstreamError,
)
from genomics_mcp.archives.ega import EgaAuth, EgaClient, EgaPasswordGrant

META = "https://metadata.ega-archive.org"
DATA = "https://ega.ebi.ac.uk:8443/v2"
TOKEN_URL = "https://ega.ebi.ac.uk:8443/ega-openid-connect-server/token"
DS = "EGAD00000000009"
DATASET = {"accession_id": DS, "title": "Synthetic", "description": "d", "num_samples": 2,
           "access_type": "controlled", "policy_accession_id": "EGAP00000000001"}
FILES = [
    {"accession_id": "EGAF00000000001", "unencrypted_checksum": "AB" * 16, "unencrypted_checksum_type": "MD5",
     "filesize": 1000, "extension": "bam", "has_report": True, "locations": ["ebi"]},
    {"accession_id": "EGAF00000000002", "unencrypted_checksum": "cd" * 16, "unencrypted_checksum_type": "MD5",
     "filesize": 10, "extension": "bai", "has_report": False, "locations": ["ebi"]},
    {"accession_id": "EGAF00000000003", "unencrypted_checksum": "ef" * 16, "unencrypted_checksum_type": "MD5",
     "filesize": 10, "extension": "1664408722194", "has_report": True, "locations": ["ebi"]},
]


def client(router: Router, auth: EgaAuth | None = None) -> EgaClient:
    c = EgaClient(router.client(), auth=auth)
    c.meta.limiter.min_interval_s = c.data.limiter.min_interval_s = 0
    return c


def paged(rows: list[dict], total: int):
    def handler(req: httpx.Request) -> httpx.Response:
        total_hdr = {"EGA-API-Total-Count": str(total)}
        if req.method == "HEAD":
            return httpx.Response(200, headers=total_hdr)
        off, lim = int(req.url.params.get("offset", 0)), int(req.url.params.get("limit", 10))
        page = rows[off:off + lim]
        return json_response(page, 206 if off + lim < total else 200, total_hdr)
    return handler


def base_routes(router: Router) -> None:
    router.add("GET", f"{META}/datasets/{DS}", DATASET)
    for m in ("GET", "HEAD"):
        router.add(m, f"{META}/datasets/{DS}/files", paged(FILES, 3))


async def test_empty_200_is_not_found(router):
    router.add("GET", f"{META}/datasets/EGAD00000000404", httpx.Response(200, content=b""))
    with pytest.raises(NotFoundError) as ei:
        await client(router).describe_dataset("EGAD00000000404")
    assert ei.value.details["empty_body"] is True


async def test_list_files_paging_and_readiness(router):
    base_routes(router)
    c = client(router)
    p1 = await c.list_files(DS, limit=2)
    p2 = await c.list_files(DS, limit=2, cursor=p1.next_cursor)
    assert [f.accession for f in p1.items + p2.items] == [f["accession_id"] for f in FILES]
    assert p1.total == 3 and p2.next_cursor is None
    bam, bai = p1.items
    assert bam.uri == "ega://EGAF00000000001" and bam.format == "bam" and bam.access_status == "controlled"
    assert bam.visibility == "private" and bam.size_bytes is None and bam.native["filesize"] == 1000
    assert bam.checksums[0].value == "ab" * 16 and bam.index_uri is None  # public API links no index
    assert bam.readiness.state == "unknown" and "authorised" in bam.readiness.reasons[0]
    assert bai.readiness.state == "unsupported"
    assert p2.items[0].format is None  # unknown extension is not guessed
    assert bam.relationships[0].kind == "dataset" and bam.relationships[0].accession == DS
    with pytest.raises(InvalidInputError):
        await c.list_samples(DS, cursor=p1.next_cursor)  # cursor from another listing


async def test_samples_keep_phenotype_values_and_dataset_link(router):
    rows = [{"accession_id": "EGAN00000000001", "title": "S1", "description": None,
             "biological_sex": "female", "subject_id": "P1", "phenotype": "unknown"}]
    router.add("GET", f"{META}/datasets/{DS}", DATASET)
    for m in ("GET", "HEAD"):
        router.add(m, f"{META}/datasets/{DS}/samples", paged(rows, 1))
    page = await client(router).list_samples(DS)
    s = page.items[0]
    assert [(p.name, p.value) for p in s.phenotypes] == [("phenotype", "unknown"), ("biological_sex", "female")]
    assert s.native["subject_id"] == "P1" and s.phenotype_files == []
    assert (s.links[0].relation, s.links[0].kind, s.links[0].accession) == ("part_of", "dataset", DS)


async def test_describe_reports_unreconciled_counts_and_omits_dac_contacts(router):
    base_routes(router)
    router.add("GET", f"{META}/datasets/{DS}/studies", json_response([{"accession_id": "EGAS00000000001",
                                                                          "title": "St"}]))
    router.add("HEAD", f"{META}/datasets/{DS}/studies", httpx.Response(200))
    for m in ("GET", "HEAD"):
        router.add(m, f"{META}/datasets/{DS}/samples", paged([], 5))
    router.add("GET", f"{META}/policies/EGAP00000000001", {"accession_id": "EGAP00000000001",
                                                            "dac_accession_id": "EGAC00000000001"})
    router.add("GET", f"{META}/dacs/EGAC00000000001", {"accession_id": "EGAC00000000001", "title": "DAC",
                                                        "contacts": [{"email": "person@example.org"}]})
    d = await client(router).describe_dataset(DS)
    assert d.dataset.file_count == 3 and d.related["sample_endpoint_count"] == 5
    assert d.warnings and "unreconciled" in d.warnings[0]
    assert "contacts" not in d.related["dac"] and "person@example.org" not in json.dumps(d.related)
    assert {(link.relation, link.kind) for link in d.dataset.links} == {("governed_by", "policy"),
                                                                        ("part_of", "study")}


async def test_free_text_search_is_explicitly_unsupported(router):
    with pytest.raises(UnsupportedError):
        await client(router).search_datasets("breast cancer")
    with pytest.raises(InvalidInputError):
        await client(router).search_datasets("EGAD123")
    assert router.requests == []


# -- auth ---------------------------------------------------------------------------


async def test_direct_password_grant_secrets_are_redacted(router, tmp_path):
    router.add("POST", TOKEN_URL, json_response({"error": "literal synthetic-password-value echo",
                                                 "error_description": "synthetic-client-secret"}, 400))
    auth = EgaAuth(grant=EgaPasswordGrant(username="synthetic-user",
                                          password=SecretStr("synthetic-password-value"),
                                          client_secret=SecretStr("synthetic-client-secret")))
    c = client(router, auth)
    with pytest.raises(UnauthorizedError) as ei:
        await c.get_region("EGAF00000000001", _iv(), workspace=tmp_path)
    text = json.dumps(ei.value.to_dict())
    assert "synthetic-password-value" not in text and "synthetic-client-secret" not in text
    assert ei.value.details == {"http_status": 400}
    body = router.requests[0].content.decode()
    assert "grant_type=password" in body and "client_id=f20cd2d3" in body


async def test_invalid_grant_code_kept_and_file_loaded_grant(router, tmp_path):
    (tmp_path / "server.json").write_text(json.dumps({"url_auth": TOKEN_URL, "client_secret": "file-client-secret-x"}))
    (tmp_path / "cred.json").write_text(json.dumps({"username": "u", "password": "file-password-value-x"}))
    router.add("POST", TOKEN_URL, json_response({"error": "invalid_grant"}, 401))
    auth = EgaAuth(grant=EgaPasswordGrant.from_pyega3_files(tmp_path / "server.json", tmp_path / "cred.json"))
    with pytest.raises(UnauthorizedError) as ei:
        await auth.bearer(client(router).data)
    assert ei.value.details == {"http_status": 401, "source_error": "invalid_grant"}
    from genomics_mcp.archives._common.redact import redact
    assert redact("x file-password-value-x file-client-secret-x") == "x [REDACTED] [REDACTED]"


async def test_token_is_cached_and_never_serialised(router):
    router.add("POST", TOKEN_URL, json_response({"access_token": "synthetic-access-token-abc", "expires_in": 3600}))
    auth = EgaAuth(grant=EgaPasswordGrant(username="u", password=SecretStr("pw-synthetic-1"),
                                          client_secret=SecretStr("cs-synthetic-1")))
    c = client(router, auth)
    assert await auth.bearer(c.data) == "synthetic-access-token-abc"
    assert await auth.bearer(c.data) == "synthetic-access-token-abc"
    assert len(router.requests) == 1
    assert "synthetic-access-token-abc" not in repr(auth)
    from genomics_mcp.archives._common.redact import redact
    assert "synthetic-access-token-abc" not in redact("token synthetic-access-token-abc")


# -- whole files ----------------------------------------------------------------------


def authed_file_routes(router: Router, data: bytes, *, name: str, md5: str | None = None) -> None:
    router.add("GET", f"{META}/files/EGAF00000000001", FILES[0])
    router.add("GET", f"{META}/files/EGAF00000000001/datasets", json_response([DATASET]))
    router.add("HEAD", f"{META}/files/EGAF00000000001/datasets", httpx.Response(200))
    router.add("GET", f"{DATA}/metadata/files/EGAF00000000001", {
        "fileId": "EGAF00000000001", "datasetId": [DS], "indexFileId": "EGAF00000000002",
        "displayFileName": name, "fileSize": len(data) + 16,
        "plainChecksum": md5 or hashlib.md5(data).hexdigest(), "plainChecksumType": "MD5",
        "fileStatus": "available"})
    router.add("GET", f"{DATA}/files/EGAF00000000001", lambda r: httpx.Response(
        206, content=data, headers={"content-range": f"bytes 0-{len(data) - 1}/{len(data)}"}))


def token_auth() -> EgaAuth:
    return EgaAuth(token=SecretStr("synthetic-bearer-token-2"))


async def test_fetch_file_verifies_md5_uses_range_and_safe_name(router, tmp_path):
    data = b"BAM\x01synthetic"
    authed_file_routes(router, data, name="../../escape/evil.bam")
    ws = tmp_path / "ws"
    art = await client(router, token_auth()).fetch_file("EGAF00000000001", workspace=ws, budget_bytes=1000)
    p = Path(art.path)
    assert p.parent == ws.resolve() and p.read_bytes() == data and art.checksum_verified
    assert art.origin.index_uri == "ega://EGAF00000000002"
    rng = router.hits(f"{DATA}/files/")[0]
    assert rng.headers["range"] == f"bytes=0-{len(data) - 1}"
    assert rng.url.params["destinationFormat"] == "plain"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["ws"]


async def test_fetch_file_md5_mismatch_removes_file(router, tmp_path):
    authed_file_routes(router, b"real-bytes", name="x.bam", md5="0" * 32)
    with pytest.raises(UpstreamError):
        await client(router, token_auth()).fetch_file("EGAF00000000001", workspace=tmp_path, budget_bytes=1000)
    assert list(tmp_path.iterdir()) == []


async def test_fetch_file_over_budget_never_downloads(router, tmp_path):
    authed_file_routes(router, b"x" * 5000, name="big.bam")
    with pytest.raises(BudgetExceededError):
        await client(router, token_auth()).fetch_file("EGAF00000000001", workspace=tmp_path, budget_bytes=100)
    assert not router.hits(f"{DATA}/files/")


def _iv():
    from genomics_mcp.archives._common.models import Interval
    return Interval(contig="chr1", start=0, end=10, assembly="GRCh38")
