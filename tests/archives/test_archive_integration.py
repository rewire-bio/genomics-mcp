"""E6 through the real core: GenomicsService dispatch, MCP round-trips, the ega:// region
resolver and the EGA transfer backend. HTTP is mocked; see test_archives_live.py for live runs."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
from pathlib import Path

import httpx
import pytest
from archive_helpers import Router, json_response, make_bam
from mcp.client import Client

from genomics_mcp import archives
from genomics_mcp.archives.ega.integration import TRANSFER_BACKEND_COMPONENT
from genomics_mcp.config import load_settings
from genomics_mcp.context import OperationContext
from genomics_mcp.contracts import RegionFileResolver
from genomics_mcp.errors import GenomicsError
from genomics_mcp.models import FileRef, Interval
from genomics_mcp.public import Deadline
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import EffectiveLimits
from genomics_mcp.server import build_server
from genomics_mcp.service import GenomicsService

META = "https://metadata.ega-archive.org"
DATA = "https://ega.ebi.ac.uk:8443/v2"
PORTAL = "https://www.ebi.ac.uk/ena/portal/api"
BROWSER = "https://www.ebi.ac.uk/ena/browser/api"
TOKEN = "synthetic-ega-personal-token-0123456789"
EGAF = "EGAF00000000001"
DS = "EGAD00000000009"
IV = {"contig": "chr1", "start": 100, "end": 200, "assembly": "GRCh38"}


def settings_for(tmp_path: Path, *, token: bool = False, **extra):
    over = {"paths": {"work_dir": str(tmp_path / "work")}, **extra}
    env = {}
    if token:
        env["MY_EGA_TOKEN"] = TOKEN
        over.setdefault("sources", {}).setdefault("ega", {})["api_key_env"] = "MY_EGA_TOKEN"
    return load_settings(env=env, overrides=over)


def service(router: Router, settings) -> GenomicsService:
    reg = Registry()
    archives.register(reg, http_factory=router.client)
    return GenomicsService(settings, reg, load_providers=False)


def ctx_for(
    svc: GenomicsService, op: Operation = Operation.GET_READS, timeout: float = 30
) -> OperationContext:
    limits = EffectiveLimits.build(svc.settings.limits)
    return OperationContext(
        operation=op,
        settings=svc.settings,
        limits=limits,
        deadline=Deadline(timeout),
        registry=svc.registry,
        http=svc.http,
        request_id="t",
    )


def paged(rows, total):
    def handler(req: httpx.Request) -> httpx.Response:
        hdr = {"EGA-API-Total-Count": str(total)}
        if req.method == "HEAD":
            return httpx.Response(200, headers=hdr)
        off, lim = int(req.url.params.get("offset", 0)), int(req.url.params.get("limit", 10))
        return json_response(rows[off : off + lim], 206 if off + lim < total else 200, hdr)

    return handler


def ega_meta_routes(router: Router, n_files: int = 5) -> None:
    router.add(
        "GET",
        f"{META}/datasets/{DS}",
        {
            "accession_id": DS,
            "title": "T",
            "access_type": "controlled",
            "policy_accession_id": None,
            "num_samples": 1,
        },
    )
    files = [
        {
            "accession_id": f"EGAF{i:011d}",
            "unencrypted_checksum": "a" * 32,
            "unencrypted_checksum_type": "MD5",
            "filesize": 10,
            "extension": "bam",
        }
        for i in range(n_files)
    ]
    for m in ("GET", "HEAD"):
        router.add(m, f"{META}/datasets/{DS}/files", paged(files, n_files))


# -- discovery through the service ----------------------------------------------------------


async def test_list_sources_reports_registered_archives(router, tmp_path):
    svc = service(router, settings_for(tmp_path))
    res = await svc.call("list_sources", {"kind": "archive"})
    by = {r["name"]: r for r in res.data["records"]}
    assert by["ega"]["state"] == "ok" and by["ega"]["auth"] == "account"
    assert set(by["ega"]["operations"]) == {
        "search_datasets",
        "describe_dataset",
        "list_files",
        "list_samples",
        "get_sample_metadata",
    }
    assert "explicit credentials" in by["ega"]["notes"] and by["ena"]["terms_url"]
    assert svc.registry.resolver("ega") is not None and svc.registry.component(
        TRANSFER_BACKEND_COMPONENT
    )
    assert isinstance(svc.registry.resolver("ega"), RegionFileResolver)


async def test_list_files_records_paging_and_max_records(router, tmp_path):
    ega_meta_routes(router)
    svc = service(router, settings_for(tmp_path))
    res = await svc.call("list_files", {"source": "ega", "accession": DS, "max_records": 2})
    assert res.status == "ok" and len(res.data["records"]) == 2 and res.data["total"] == 5
    assert (
        res.truncation.reason == "source_page"
        and res.truncation.next_cursor == res.data["next_cursor"]
    )
    f = res.data["records"][0]
    assert (
        f["uri"].startswith("ega://")
        and f["visibility"] == "private"
        and f["access_status"] == "controlled"
    )
    nxt = await svc.call(
        "list_files",
        {"source": "ega", "accession": DS, "max_records": 2, "cursor": res.data["next_cursor"]},
    )
    assert [r["accession"] for r in nxt.data["records"]] == ["EGAF00000000002", "EGAF00000000003"]
    assert res.provenance and res.provenance[0].source == "ega"


async def test_response_byte_limit_trims_records(router, tmp_path):
    ega_meta_routes(router, n_files=40)
    svc = service(router, settings_for(tmp_path))
    res = await svc.call(
        "list_files", {"source": "ega", "accession": DS, "max_response_bytes": 6000}
    )
    assert res.status == "ok" and res.truncation.reason == "max_response_bytes"
    assert 0 < len(res.data["records"]) < 40 and res.json_size() <= 6000


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (401, "unauthorized"),
        (403, "unauthorized"),
        (500, "upstream_error"),
        (400, "invalid_input"),
    ],
)
async def test_source_errors_map_to_core_codes(router, tmp_path, status, code):
    router.add("GET", f"{BROWSER}/xml/SAMEA0000001", httpx.Response(status, text="nope"))
    svc = service(router, settings_for(tmp_path, sources={"ena": {"timeout_s": 2}}))
    res = await svc.call("get_sample_metadata", {"source": "ena", "accession": "SAMEA0000001"})
    assert res.status == "error" and res.error.code == code and res.data is None


async def test_unsupported_free_text_and_filters_are_explicit(router, tmp_path):
    svc = service(router, settings_for(tmp_path))
    r1 = await svc.call("search_datasets", {"source": "ega", "query": "breast cancer"})
    r2 = await svc.call("search_datasets", {"source": "ena", "query": "x", "assembly": "GRCh38"})
    assert (
        r1.error.code == "unsupported" and r2.error.code == "unsupported" and router.requests == []
    )


async def test_disabled_source_makes_no_request(router, tmp_path):
    svc = service(router, settings_for(tmp_path, sources={"ena": {"enabled": False}}))
    res = await svc.call("describe_dataset", {"source": "ena", "accession": "PRJEB1"})
    assert res.error.code == "unsupported" and router.requests == []
    listed = await svc.call("list_sources", {})
    assert {r["name"]: r["state"] for r in listed.data["records"]}["ena"] == "disabled"


async def test_call_deadline_bounds_source_requests(tmp_path):
    import asyncio

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return json_response([])

    reg = Registry()
    archives.register(
        reg, http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(slow))
    )
    s = settings_for(tmp_path, limits={"interactive_timeout_s": 0.2})
    res = await GenomicsService(s, reg, load_providers=False).call(
        "describe_dataset", {"source": "ena", "accession": "PRJEB1"}
    )
    assert res.status == "error" and res.error.code == "timeout"


async def test_one_source_outage_does_not_affect_another(router, tmp_path):
    router.add("GET", f"{PORTAL}/search", httpx.Response(503, text="maintenance"))
    ega_meta_routes(router)
    svc = service(router, settings_for(tmp_path, sources={"ena": {"timeout_s": 3}}))
    bad = await svc.call("describe_dataset", {"source": "ena", "accession": "PRJEB1"})
    good = await svc.call("list_files", {"source": "ega", "accession": DS})
    assert bad.status == "error" and bad.error.code == "upstream_error" and bad.error.retryable
    assert good.status == "ok" and good.data["records"]


async def test_credentials_never_in_outputs_or_logs(router, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    router.add("GET", f"{META}/datasets/{DS}", {"accession_id": DS, "access_type": "controlled"})
    router.add(
        "GET",
        f"{DATA}/metadata/datasets/{DS}/files",
        json_response({"message": f"bad token {TOKEN}"}, 401),
    )
    svc = service(router, settings_for(tmp_path, token=True, logging={"log_query_content": True}))
    res = await svc.call("list_files", {"source": "ega", "accession": DS})
    assert res.error.code == "unauthorized"
    assert router.hits(f"{DATA}/metadata")[0].headers["authorization"] == f"Bearer {TOKEN}"
    dumped = res.model_dump_json() + caplog.text + json.dumps(svc.status())
    assert TOKEN not in dumped


async def test_mcp_round_trip_in_process(router, tmp_path):
    ega_meta_routes(router)
    svc = service(router, settings_for(tmp_path))
    async with Client(build_server(svc), mode="legacy") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "not implemented" not in tools["list_files"].description.lower()
        res = await client.call_tool(
            "list_files", {"source": "ega", "accession": DS, "max_records": 3}
        )
        assert res.is_error is False
        body = res.structured_content
        assert body["status"] == "ok" and len(body["data"]["records"]) == 3
        assert body["truncation"]["reason"] == "source_page"
        err = await client.call_tool("search_datasets", {"source": "ega", "query": "free text"})
        assert err.is_error is True and err.structured_content["error"]["code"] == "unsupported"


# -- ega:// region resolver -------------------------------------------------------------------


def ticket_for(bam: bytes) -> dict:
    enc = base64.b64encode(bam).decode()
    return {
        "htsget": {"format": "BAM", "urls": [{"url": f"data:base64,{enc}", "urlClass": "body"}]}
    }


async def test_resolve_region_returns_bounded_private_artifact(router, tmp_path):
    bam = make_bam(tmp_path / "fixture.bam")
    router.add("GET", f"{DATA}/htsget/reads/{EGAF}", json_response(ticket_for(bam)))
    svc = service(router, settings_for(tmp_path, token=True))
    f = FileRef(
        uri=f"ega://{EGAF}", format="bam", checksums=[{"algorithm": "md5", "value": "f" * 32}]
    )
    iv = Interval(**IV)
    resolved = await ctx_for(svc).resolve_file(f, interval=iv)
    assert (
        resolved.region == iv
        and resolved.local_path.parent == (tmp_path / "work/archives/ega-regions").resolve()
    )
    assert resolved.local_path.read_bytes() == bam and resolved.readiness.state == "ready"
    meta = resolved.file.native["region_artifact"]
    assert (meta["records_received"], meta["records_overlapping"]) == (6, 3)
    assert meta["sha256"] == hashlib.sha256(bam).hexdigest() and meta["bytes"] == len(bam)
    assert "not assembly accessions" in meta["header"]["assembly_note"]
    assert [c.value for c in resolved.file.checksums] == [
        hashlib.sha256(bam).hexdigest(),
        hashlib.md5(bam).hexdigest(),
    ]
    assert (
        resolved.file.native["source_checksums"][0]["value"] == "f" * 32
    )  # full-file MD5 kept apart
    assert resolved.file.visibility == "private"
    q = router.requests[0].url.params
    assert (q["referenceName"], q["start"], q["end"]) == ("chr1", "100", "200")
    assert {r.url.host for r in router.requests} == {"ega.ebi.ac.uk"}  # nothing else contacted


async def test_resolve_region_rejects_assembly_mismatch(router, tmp_path):
    router.add(
        "GET",
        f"{DATA}/htsget/reads/{EGAF}",
        json_response(ticket_for(make_bam(tmp_path / "f.bam"))),
    )
    svc = service(router, settings_for(tmp_path, token=True))
    with pytest.raises(GenomicsError) as ei:
        await ctx_for(svc).resolve_file(
            FileRef(uri=f"ega://{EGAF}", format="bam"),
            interval=Interval(**{**IV, "assembly": "GRCh37"}),
        )
    assert ei.value.error_code == "invalid_input"
    assert not any((tmp_path / "work/archives/ega-regions").glob("*.bam"))


async def test_resolve_region_refuses_cram_payload_before_pysam(router, tmp_path):
    enc = base64.b64encode(b"CRAM\x03\x00fake").decode()
    router.add(
        "GET",
        f"{DATA}/htsget/reads/{EGAF}",
        json_response({"htsget": {"format": "BAM", "urls": [{"url": f"data:base64,{enc}"}]}}),
    )
    svc = service(router, settings_for(tmp_path, token=True))
    with pytest.raises(GenomicsError) as ei:
        await ctx_for(svc).resolve_file(
            FileRef(uri=f"ega://{EGAF}", format="cram"), interval=Interval(**IV)
        )
    assert ei.value.error_code == "upstream_error" and "cram" in ei.value.info.message.lower()


async def test_region_guards_make_no_request(router, tmp_path):
    svc = service(router, settings_for(tmp_path))
    ctx = ctx_for(svc)
    f = FileRef(uri=f"ega://{EGAF}", format="bam")
    for kwargs, code in [
        ({"interval": Interval(**IV)}, "unauthorized"),  # anonymous: no credentials configured
        (
            {"interval": Interval(contig="chr1", start=0, end=2_000_000, assembly="GRCh38")},
            "budget_exceeded",
        ),
        ({}, "preparation_required"),  # whole-file resolution never downloads
    ]:
        with pytest.raises(GenomicsError) as ei:
            await ctx.resolve_file(f, **kwargs)
        assert ei.value.error_code == code
    with pytest.raises(GenomicsError) as ei:
        await ctx.resolve_file(
            FileRef(uri="ega://../../etc", format="bam"), interval=Interval(**IV)
        )
    assert ei.value.error_code == "invalid_input"
    assert router.requests == []


async def test_workspace_quota_checked_before_region(router, tmp_path):
    s = settings_for(tmp_path, token=True, limits={"workspace_max_bytes": 1024})
    svc = service(router, s)
    with pytest.raises(GenomicsError) as ei:
        await ctx_for(svc).resolve_file(
            FileRef(uri=f"ega://{EGAF}", format="bam"), interval=Interval(**IV)
        )
    assert ei.value.error_code == "budget_exceeded" and router.requests == []


# -- whole files through the transfer backend -----------------------------------------------------


def file_routes(router: Router, data: bytes) -> None:
    router.add(
        "GET",
        f"{META}/files/{EGAF}",
        {
            "accession_id": EGAF,
            "unencrypted_checksum": hashlib.md5(data).hexdigest(),
            "unencrypted_checksum_type": "MD5",
            "filesize": len(data) + 99,
            "extension": "bam",
        },
    )
    router.add(
        "GET",
        f"{META}/files/{EGAF}/datasets",
        json_response([{"accession_id": DS, "access_type": "controlled"}]),
    )
    router.add("HEAD", f"{META}/files/{EGAF}/datasets", httpx.Response(200))
    router.add(
        "GET",
        f"{DATA}/metadata/files/{EGAF}",
        {
            "fileId": EGAF,
            "datasetId": [DS],
            "displayFileName": "../x/HG.bam",
            "fileSize": len(data) + 16,
            "plainChecksum": hashlib.md5(data).hexdigest(),
            "plainChecksumType": "MD5",
            "fileStatus": "available",
        },
    )

    def ranged(req: httpx.Request) -> httpx.Response:
        a, b = (int(x) for x in req.headers["range"].removeprefix("bytes=").split("-"))
        return httpx.Response(
            206, content=data[a : b + 1], headers={"content-range": f"bytes {a}-{b}/{len(data)}"}
        )

    router.add("GET", f"{DATA}/files/{EGAF}", ranged)


async def test_transfer_backend_describe_and_resumable_stream(router, tmp_path):
    data = bytes(range(256)) * 40
    file_routes(router, data)
    svc = service(router, settings_for(tmp_path, token=True))
    backend = svc.registry.component(TRANSFER_BACKEND_COMPONENT)
    f = FileRef(uri=f"ega://{EGAF}", format="bam")
    desc = await backend.describe(f, ctx_for(svc, Operation.FETCH_FILE))
    assert desc.size_bytes == len(data) and desc.checksums[0].value == hashlib.md5(data).hexdigest()
    assert desc.file.visibility == "private" and "/" not in desc.suggested_name
    assert any("fileSize - 16" in n for n in desc.notes)
    assert not router.hits(f"{DATA}/files/")  # describe never downloads
    got = bytearray()
    async with backend.open(f, ctx_for(svc, Operation.FETCH_FILE), start=0, end=1000) as body:
        async for chunk in body:
            got += chunk
    async with backend.open(
        f, ctx_for(svc, Operation.FETCH_FILE), start=1000, end=len(data)
    ) as body:  # resume
        async for chunk in body:
            got += chunk
    assert bytes(got) == data and hashlib.md5(got).hexdigest() == desc.checksums[0].value
    ranges = [r.headers["range"] for r in router.hits(f"{DATA}/files/")]
    assert ranges == ["bytes=0-999", f"bytes=1000-{len(data) - 1}"]
    assert all(
        r.headers["authorization"] == f"Bearer {TOKEN}" for r in router.hits(f"{DATA}/files/")
    )
    assert all(r.url.params["destinationFormat"] == "plain" for r in router.hits(f"{DATA}/files/"))


async def test_transfer_backend_requires_credentials(router, tmp_path):
    svc = service(router, settings_for(tmp_path))
    backend = svc.registry.component(TRANSFER_BACKEND_COMPONENT)
    with pytest.raises(GenomicsError) as ei:
        await backend.describe(FileRef(uri=f"ega://{EGAF}"), ctx_for(svc, Operation.FETCH_FILE))
    assert ei.value.error_code == "unauthorized" and router.requests == []


def test_settings_never_read_ambient(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADUMMYDUMMYDUMMY0")
    from genomics_mcp.archives.ega.access import EgaAccessConfig

    s = load_settings(
        env={"AWS_ACCESS_KEY_ID": "AKIADUMMYDUMMYDUMMY0", "EGA_TOKEN": "ambient-looking"},
        overrides={"paths": {"work_dir": str(tmp_path)}},
    )
    assert EgaAccessConfig.from_settings(s).mode == "anonymous"
    s2 = load_settings(
        env={"GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT": "1"},
        overrides={"paths": {"work_dir": str(tmp_path)}},
    )
    assert EgaAccessConfig.from_settings(s2).mode == "public_test_account"
    assert dataclasses.asdict(EgaAccessConfig.from_settings(s2))["password"] is None
