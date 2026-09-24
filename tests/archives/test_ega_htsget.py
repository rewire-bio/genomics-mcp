"""EGA htsget: data-URL variants, block budgets/auth scoping, post-filtering and artifact safety."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

import httpx
import pytest
from archive_helpers import Router, json_response, make_bam
from pydantic import SecretStr

from genomics_mcp.archives._common.errors import (
    BudgetExceededError,
    DeadlineExceededError,
    InvalidInputError,
    NotFoundError,
    UnauthorizedError,
    UpstreamError,
)
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy
from genomics_mcp.archives._common.models import Interval
from genomics_mcp.archives.ega import EgaAuth, EgaClient
from genomics_mcp.archives.ega import htsget as hg

DATA = "https://ega.ebi.ac.uk:8443/v2"
TICKET = f"{DATA}/htsget/reads/EGAF00000000001"
IV = Interval(contig="chr1", start=100, end=200, assembly="GRCh38")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def ega(router: Router, **kw) -> EgaClient:
    c = EgaClient(router.client(), auth=EgaAuth(token=SecretStr("synthetic-bearer-token-1")), **kw)
    for h in (c.meta, c.data):
        h.limiter.min_interval_s = 0
    return c


def ticket(*urls: dict, md5: str | None = None) -> dict:
    body = {"format": "BAM", "urls": list(urls)}
    if md5:
        body["md5"] = md5
    return {"htsget": body}


def split_blocks(bam: bytes) -> list[dict]:
    """EGA-style: nonstandard `data:base64,` and `urlClass`, header then body."""
    return [{"url": f"data:base64,{b64(bam[:40])}", "urlClass": "header"},
            {"url": f"data:base64,{b64(bam[40:])}", "urlClass": "body"}]


# -- data URLs ---------------------------------------------------------------------


def test_data_url_variants_decode_once() -> None:
    raw = b"\x1f\x8bBGZF-ish"
    assert hg.decode_data_url(f"data:base64,{b64(raw)}", max_bytes=100) == raw  # EGA form
    assert hg.decode_data_url(f"data:application/octet-stream;base64,{b64(raw)}", max_bytes=100) == raw
    assert hg.decode_data_url("data:,a%20b", max_bytes=100) == b"a b"  # percent-encoded, not base64
    inner = b64(b"payload").encode()  # a block whose bytes happen to be base64 text
    assert hg.decode_data_url(f"data:base64,{b64(inner)}", max_bytes=100) == inner


def test_data_url_invalid_and_oversized() -> None:
    with pytest.raises(UpstreamError):
        hg.decode_data_url("data:base64,***", max_bytes=100)
    with pytest.raises(BudgetExceededError):
        hg.decode_data_url(f"data:base64,{b64(b'x' * 300)}", max_bytes=100)


def test_ticket_accepts_class_and_urlclass() -> None:
    t = hg.parse_ticket(ticket({"url": "data:base64,QQ==", "class": "header"},
                               {"url": "https://ega.ebi.ac.uk:8443/b", "urlClass": "body"}))
    assert [b.url_class for b in t.blocks] == ["header", "body"]
    assert [b.kind for b in t.blocks] == ["data", "remote"]


# -- block fetching -----------------------------------------------------------------


def block_http(router: Router, hosts: set[str]) -> SourceHttp:
    return SourceHttp(router.client(), SourcePolicy("ega", frozenset(hosts), min_interval_s=0))


async def test_exhausted_budget_stops_before_remote_block(router: Router) -> None:
    router.add("GET", "https://ega.ebi.ac.uk:8443/blk", httpx.Response(200, content=b"z" * 1024))
    t = hg.parse_ticket(ticket({"url": "data:base64,QQ=="}, {"url": "https://ega.ebi.ac.uk:8443/blk"}))
    with pytest.raises(BudgetExceededError):
        await hg.fetch_blocks(block_http(router, {"ega.ebi.ac.uk"}), t, ticket_url=TICKET, bearer="tok",
                              budget_bytes=1, allowed_hosts=frozenset({"ega.ebi.ac.uk"}))
    assert router.requests == []  # the remote block was never requested


async def test_remote_block_body_bounded_by_remaining_budget(router: Router) -> None:
    router.add("GET", "https://ega.ebi.ac.uk:8443/blk", httpx.Response(200, content=b"z" * 1024))
    t = hg.parse_ticket(ticket({"url": "data:base64,QQ=="}, {"url": "https://ega.ebi.ac.uk:8443/blk"}))
    with pytest.raises(BudgetExceededError):
        await hg.fetch_blocks(block_http(router, {"ega.ebi.ac.uk"}), t, ticket_url=TICKET, bearer="tok",
                              budget_bytes=100, allowed_hosts=frozenset({"ega.ebi.ac.uk"}))


async def test_bearer_only_to_ticket_origin_and_ticket_headers_filtered(router: Router) -> None:
    router.add("GET", "https://ega.ebi.ac.uk:8443/blk", httpx.Response(206, content=b"AA"))
    router.add("GET", "https://blocks.example.org/blk", httpx.Response(200, content=b"BB"))
    t = hg.parse_ticket(ticket(
        {"url": "https://ega.ebi.ac.uk:8443/blk", "headers": {"Range": "bytes=0-1"}},
        {"url": "https://blocks.example.org/blk", "headers": {"Range": "bytes=2-3", "Cookie": "c=1",
                                                              "X-Other": "no"}},
    ))
    hosts = frozenset({"ega.ebi.ac.uk", "blocks.example.org"})
    data, infos = await hg.fetch_blocks(block_http(router, set(hosts)), t, ticket_url=TICKET, bearer="tok",
                                        budget_bytes=100, allowed_hosts=hosts)
    same, other = router.requests
    assert data == b"AABB" and [i.bytes for i in infos] == [2, 2]
    assert same.headers["authorization"] == "Bearer tok" and same.headers["range"] == "bytes=0-1"
    assert "authorization" not in other.headers and "cookie" not in other.headers
    assert "x-other" not in other.headers and other.headers["range"] == "bytes=2-3"


async def test_remote_block_on_unapproved_host_rejected(router: Router) -> None:
    t = hg.parse_ticket(ticket({"url": "https://evil.example.org/blk"}))
    with pytest.raises(InvalidInputError):
        await hg.fetch_blocks(block_http(router, {"ega.ebi.ac.uk"}), t, ticket_url=TICKET, bearer="tok",
                              budget_bytes=100, allowed_hosts=frozenset({"ega.ebi.ac.uk"}))
    assert router.requests == []


# -- get_region end to end -------------------------------------------------------


def route_ticket(router: Router, body: dict | httpx.Response) -> None:
    router.add("GET", TICKET, body if isinstance(body, httpx.Response) else json_response(body))


async def test_region_postfilters_by_cigar_overlap_and_passes_coordinates(router, tmp_path, synthetic_bam):
    route_ticket(router, ticket(*split_blocks(synthetic_bam)))
    res = await ega(router).get_region("EGAF00000000001", IV, workspace=tmp_path)
    q = router.requests[0].url.params
    assert (q["referenceName"], q["start"], q["end"], q["format"]) == ("chr1", "100", "200", "BAM")
    assert router.requests[0].headers["accept"].startswith("application/vnd.ga4gh.htsget")
    assert res.records_in_blocks == 6 and res.records_skipped_unplaced == 1
    assert [r["query_name"] for r in res.records] == ["r_span", "r_in", "r_end"]
    assert res.records_overlapping == 3 and not res.truncated
    assert res.artifact.size_bytes == len(synthetic_bam)
    assert res.artifact.checksums[0].value == hashlib.sha256(synthetic_bam).hexdigest()
    assert res.artifact.checksum_verified is False  # no ticket MD5 supplied
    assert Path(res.artifact.path).stat().st_mode & 0o777 == 0o600
    assert res.header.sq_assembly_tags == ["GRCh38"] and res.header.contig_present


async def test_region_record_cap_reports_truncation(router, tmp_path, synthetic_bam):
    route_ticket(router, ticket(*split_blocks(synthetic_bam)))
    res = await ega(router).get_region("EGAF00000000001", IV, workspace=tmp_path, max_records=1)
    assert len(res.records) == 1 and res.records_overlapping == 3 and res.truncated


async def test_ticket_md5_checked(router, tmp_path, synthetic_bam):
    route_ticket(router, ticket(*split_blocks(synthetic_bam), md5="0" * 32))
    with pytest.raises(UpstreamError):
        await ega(router).get_region("EGAF00000000001", IV, workspace=tmp_path)
    assert list(tmp_path.iterdir()) == []
    router.add("GET", TICKET, json_response(ticket(*split_blocks(synthetic_bam),
                                                   md5=hashlib.md5(synthetic_bam).hexdigest())))
    res = await ega(router).get_region("EGAF00000000001", IV, workspace=tmp_path)
    assert res.artifact.checksum_verified


@pytest.mark.parametrize("contig", ["../../../escaped", "/abs/path", "..", "a/../../b"])
async def test_contig_never_becomes_a_path(router, tmp_path, contig):
    (tmp_path / "src").mkdir()
    bam = make_bam(tmp_path / "src" / "src.bam")
    route_ticket(router, ticket(*split_blocks(bam)))
    ws = tmp_path / "ws" / "allowed"
    iv = Interval(contig=contig, start=10, end=12, assembly="GRCh38")
    res = await ega(router).get_region("EGAF00000000001", iv, workspace=ws, assembly_policy="warn")
    p = Path(res.artifact.path)
    assert p.parent == ws.resolve() and p.exists()
    assert {x.name for x in (tmp_path / "ws").iterdir()} == {"allowed"}
    assert sorted(x.name for x in tmp_path.iterdir()) == ["src", "ws"]
    assert any("not in the file header" in w for w in res.warnings)


async def test_symlinked_artifact_target_refused(router, tmp_path, synthetic_bam):
    ws = tmp_path / "ws"
    ws.mkdir()
    name = hg.region_artifact_name("EGAF00000000001", IV, "bam")
    outside = tmp_path / "outside.bam"
    (ws / name).symlink_to(outside)
    route_ticket(router, ticket(*split_blocks(synthetic_bam)))
    with pytest.raises(InvalidInputError):
        await ega(router).get_region("EGAF00000000001", IV, workspace=ws)
    assert not outside.exists() and router.requests == []


async def test_assembly_mismatch_rejected_by_default(router, tmp_path, synthetic_bam):
    route_ticket(router, ticket(*split_blocks(synthetic_bam)))
    iv37 = Interval(contig="chr1", start=100, end=200, assembly="GRCh37")
    with pytest.raises(InvalidInputError) as ei:
        await ega(router).get_region("EGAF00000000001", iv37, workspace=tmp_path)
    assert "no liftover" in ei.value.message and list(tmp_path.iterdir()) == []
    res = await ega(router).get_region("EGAF00000000001", iv37, workspace=tmp_path, assembly_policy="warn")
    assert res.warnings and res.records_overlapping == 3


@pytest.mark.parametrize(("status", "body", "exc", "http_status"), [
    (401, {"htsget": {"error": "InvalidAuthentication", "message": "x"}}, UnauthorizedError, 401),
    (403, {"htsget": {"error": "PermissionDenied", "message": "x"}}, UnauthorizedError, 403),
    (404, {"htsget": {"error": "NotFound", "message": "No such accession"}}, NotFoundError, 404),
])
async def test_ticket_errors_are_source_native(router, tmp_path, status, body, exc, http_status):
    route_ticket(router, json_response(body, status))
    with pytest.raises(exc) as ei:
        await ega(router).get_region("EGAF00000000001", IV, workspace=tmp_path)
    assert ei.value.details["http_status"] == http_status
    assert "synthetic-bearer-token-1" not in str(ei.value.to_dict())


async def test_ticket_500_is_upstream(router, tmp_path):
    route_ticket(router, json_response({"status": 500, "error": "Internal Server Error"}, 500))
    c = ega(router)
    c.data.policy = SourcePolicy("ega", c.data.policy.allowed_hosts, min_interval_s=0, max_retries=0)
    with pytest.raises(UpstreamError):
        await c.get_region("EGAF00000000001", IV, workspace=tmp_path)


async def test_no_credentials_means_no_request(router, tmp_path):
    c = EgaClient(router.client())
    with pytest.raises(UnauthorizedError):
        await c.get_region("EGAF00000000001", IV, workspace=tmp_path)
    assert router.requests == []


async def test_region_size_and_budget_validated_before_request(router, tmp_path):
    big = Interval(contig="chr1", start=0, end=2_000_001, assembly="GRCh38")
    with pytest.raises(InvalidInputError):
        await ega(router).get_region("EGAF00000000001", big, workspace=tmp_path)
    with pytest.raises(InvalidInputError):
        await ega(router).get_region("EGAF00000000001", IV, workspace=tmp_path, budget_bytes=0)
    assert router.requests == []


async def test_operation_deadline_covers_all_blocks(router, tmp_path):
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.04)
        return httpx.Response(200, content=b"AA")

    route_ticket(router, ticket({"url": "https://ega.ebi.ac.uk:8443/b1"}, {"url": "https://ega.ebi.ac.uk:8443/b2"}))
    client = EgaClient(httpx.AsyncClient(transport=httpx.MockTransport(_async_router(router, slow))),
                       auth=EgaAuth(token=SecretStr("synthetic-bearer-token-1")))
    client.data.limiter.min_interval_s = 0
    with pytest.raises(DeadlineExceededError):
        await client.get_region("EGAF00000000001", IV, workspace=tmp_path, timeout_s=0.06)


def _async_router(router: Router, slow):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/b1", "/b2")):
            return await slow(request)
        return router(request)

    return handler
