"""E7 through the real core: service dispatch, MCP round-trip and the NCBI preparation component."""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest
from catalog_helpers import Router, json_response
from mcp.client import Client

from genomics_mcp import catalogs
from genomics_mcp.catalogs.encode import BASE as ENCODE
from genomics_mcp.catalogs.geo import ACC_CGI
from genomics_mcp.catalogs.ncbi_datasets import BASE as NCBI
from genomics_mcp.catalogs.preparation import PREPARER_COMPONENT
from genomics_mcp.config import load_settings
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import GenomicsError
from genomics_mcp.public import Deadline
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import EffectiveLimits
from genomics_mcp.server import build_server
from genomics_mcp.service import GenomicsService

ACC = "GCF_000819615.1"
S3 = "https://encode-public.s3.amazonaws.com/2026/05/05/x/ENCFF792QDS.bigWig"


def svc_for(router: Router, tmp_path: Path, **over) -> GenomicsService:
    s = load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path / "work")}, **over})
    reg = Registry()
    catalogs.register(reg, http_factory=router.client)
    return GenomicsService(s, reg, load_providers=False)


def ctx_for(svc: GenomicsService, timeout: float = 30) -> OperationContext:
    return OperationContext(
        operation=Operation.FETCH_FILE,
        settings=svc.settings,
        limits=EffectiveLimits.build(svc.settings.limits),
        deadline=Deadline(timeout),
        registry=svc.registry,
        http=svc.http,
        request_id="t",
    )


def encode_file_route(router: Router) -> None:
    router.add(
        "GET",
        f"{ENCODE}/files/ENCFF792QDS/",
        json_response(
            {
                "@id": "/files/ENCFF792QDS/",
                "accession": "ENCFF792QDS",
                "file_format": "bigWig",
                "assembly": "GRCh38",
                "file_size": 1413106336,
                "md5sum": "6b5e27fcb966d26cca1398e65b590dfd",
                "status": "released",
                "href": "/files/ENCFF792QDS/@@download/ENCFF792QDS.bigWig",
                "dataset": "/annotations/ENCSR901HTN/",
                "cloud_metadata": {"url": S3, "file_size": 1413106336},
                "azure_uri": "https://x.blob.core.windows.net/f?sv=1&sig=SASSECRETVALUE",
            }
        ),
    )


async def test_encode_signal_file_is_a_usable_fileref(router, tmp_path):
    encode_file_route(router)
    res = await svc_for(router, tmp_path).call(
        "list_files", {"source": "encode", "accession": "ENCFF792QDS"}
    )
    (f,) = res.data["records"]
    assert (f["uri"], f["assembly"], f["format"], f["size_bytes"]) == (
        S3,
        "GRCh38",
        "bigwig",
        1413106336,
    )
    assert f["native"]["href"] == f"{ENCODE}/files/ENCFF792QDS/@@download/ENCFF792QDS.bigWig"
    assert f["checksums"] == [{"algorithm": "md5", "value": "6b5e27fcb966d26cca1398e65b590dfd"}]
    assert (
        f["relationships"][0]["kind"] == "dataset"
        and f["relationships"][0]["accession"] == "ENCSR901HTN"
    )
    assert f["readiness"]["state"] == "unknown"  # listing does not probe; check_file does
    assert "SASSECRETVALUE" not in res.model_dump_json()


async def test_format_filter_and_search_filters(router, tmp_path):
    encode_file_route(router)
    svc = svc_for(router, tmp_path)
    res = await svc.call(
        "list_files", {"source": "encode", "accession": "ENCFF792QDS", "formats": ["bam"]}
    )
    assert res.data["records"] == [] and any("did not match" in w for w in res.warnings)
    router.add("GET", f"{ENCODE}/search/", json_response({"total": 0, "@graph": []}))
    await svc.call(
        "search_datasets",
        {"source": "encode", "query": "CTCF", "assembly": "GRCh38", "organism": "Homo sapiens"},
    )
    q = router.requests[-1].url.params
    assert (
        q["assembly"] == "GRCh38"
        and q["replicates.library.biosample.donor.organism.scientific_name"] == "Homo sapiens"
    )
    bad = await svc.call("search_datasets", {"source": "geo", "query": "x", "assembly": "GRCh38"})
    assert bad.error.code == "unsupported"


async def test_geo_sample_characteristics_and_not_found(router, tmp_path):
    soft = (
        "^SAMPLE = GSM1\n!Sample_title = t\n!Sample_channel_count = 1\n"
        "!Sample_characteristics_ch1 = antibody: H3K4me3\n!Sample_series_id = GSE1\n"
        "!Sample_contact_email = person@example.org\n"
    )
    router.add(
        "GET",
        ACC_CGI,
        lambda r: httpx.Response(
            200,
            text=soft
            if r.url.params["acc"] == "GSM1"
            else "<html>Could not find a public or private accession</html>",
        ),
    )
    svc = svc_for(router, tmp_path)
    ok = await svc.call("get_sample_metadata", {"source": "geo", "accession": "GSM1"})
    assert ok.data["phenotypes"] == [
        {
            "name": "antibody",
            "value": "H3K4me3",
            "unit": None,
            "ontology_term": None,
            "source": "geo",
        }
    ]
    assert "person@example.org" not in ok.model_dump_json()
    missing = await svc.call("describe_dataset", {"source": "geo", "accession": "GSE404"})
    assert missing.error.code == "not_found"


async def test_ncbi_packages_not_ready_and_no_fake_samples(router, tmp_path):
    router.add(
        "GET",
        f"{NCBI}/genome/accession/{ACC}/download_summary",
        json_response(
            {"available_files": {"all_genomic_fasta": {"file_count": 1, "size_mb": 0.01}}}
        ),
    )
    router.add(
        "GET",
        f"{NCBI}/genome/accession/{ACC}/dataset_report",
        json_response({"reports": [{"accession": ACC, "assembly_info": {"assembly_name": "X"}}]}),
    )
    svc = svc_for(router, tmp_path)
    files = await svc.call("list_files", {"source": "ncbi_datasets", "accession": ACC})
    (pkg,) = files.data["records"]
    assert pkg["readiness"]["state"] == "download_required" and pkg["native"]["container"] == "zip"
    assert pkg["format"] == "other"
    samples = await svc.call("list_samples", {"source": "ncbi_datasets", "accession": ACC})
    assert samples.status == "ok" and samples.data["records"] == [] and samples.warnings


async def test_mcp_round_trip_catalogs(router, tmp_path):
    encode_file_route(router)
    async with Client(build_server(svc_for(router, tmp_path)), mode="legacy") as client:
        res = await client.call_tool("list_files", {"source": "encode", "accession": "ENCFF792QDS"})
        assert (
            res.is_error is False
            and res.structured_content["data"]["records"][0]["assembly"] == "GRCh38"
        )
        listed = await client.call_tool("list_sources", {"kind": "catalog"})
        states = {r["name"]: r["state"] for r in listed.structured_content["data"]["records"]}
        assert states == {"encode": "ok", "geo": "ok", "ncbi_datasets": "ok"}


# -- preparation component ---------------------------------------------------------------------


def package(fna: bytes, manifest: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"ncbi_dataset/data/{ACC}/{ACC}_genomic.fna", fna)
        z.writestr("md5sum.txt", manifest)
    return buf.getvalue()


def prep_routes(router: Router, body: bytes, delay: float = 0) -> None:
    router.add(
        "GET",
        f"{NCBI}/genome/accession/{ACC}/download_summary",
        json_response(
            {"available_files": {"all_genomic_fasta": {"file_count": 1, "size_mb": 0.01}}}
        ),
    )
    router.add("GET", f"{NCBI}/genome/accession/{ACC}/download", httpx.Response(200, content=body))


async def test_preparer_produces_verified_indexed_fasta(router, tmp_path):
    fna = b">NC_001422.1 x\nACGT\n"
    prep_routes(
        router,
        package(
            fna,
            f"{hashlib.md5(fna).hexdigest()}  ncbi_dataset/data/{ACC}/{ACC}_genomic.fna\n".encode(),
        ),
    )
    svc = svc_for(router, tmp_path)
    art = await svc.registry.component(PREPARER_COMPONENT).prepare(
        ACC, ctx_for(svc), budget_bytes=1_000_000
    )
    assert (
        art.checksum_verified
        and Path(art.path).read_bytes() == fna  # noqa: ASYNC240 - small local test file
        and Path(art.index_path).exists()  # noqa: ASYNC240 - small local test file
    )
    assert Path(art.path).parent == (tmp_path / "work/catalogs/ncbi-genomes").resolve()


async def test_preparer_manifest_bomb_is_bounded(router, tmp_path):
    prep_routes(router, package(b">a\nA\n", b"0" * (8 * 1024 * 1024)))  # compresses to a few KB
    svc = svc_for(router, tmp_path)
    with pytest.raises(GenomicsError) as ei:
        await svc.registry.component(PREPARER_COMPONENT).prepare(
            ACC, ctx_for(svc), budget_bytes=1_000_000
        )
    assert ei.value.error_code == "budget_exceeded"
    assert not list((tmp_path / "work/catalogs/ncbi-genomes").iterdir())


async def test_preparer_quota_and_ceiling_checked_before_download(router, tmp_path):
    svc = svc_for(
        router,
        tmp_path,
        limits={"workspace_max_bytes": 1000, "transfer_budget_ceiling_bytes": 5000},
    )
    prep = svc.registry.component(PREPARER_COMPONENT)
    for budget in (4000, 10_000):
        with pytest.raises(GenomicsError) as ei:
            await prep.prepare(ACC, ctx_for(svc), budget_bytes=budget)
        assert ei.value.error_code == "budget_exceeded"
    assert router.requests == []


async def test_preparer_deadline_cleans_up(tmp_path):
    fna = b">a\n" + b"ACGT" * 1000 + b"\n"
    body = package(fna, b"")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/download"):
            await asyncio.sleep(1)
            return httpx.Response(200, content=body)
        return json_response({"available_files": {"all_genomic_fasta": {"size_mb": 0.01}}})

    s = load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path / "work")}})
    reg = Registry()
    catalogs.register(
        reg, http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    svc = GenomicsService(s, reg, load_providers=False)
    with pytest.raises(GenomicsError) as ei:
        await reg.component(PREPARER_COMPONENT).prepare(
            ACC, ctx_for(svc, timeout=0.3), budget_bytes=100_000
        )
    assert ei.value.error_code == "timeout"
    root = tmp_path / "work/catalogs/ncbi-genomes"
    assert not root.exists() or not list(root.iterdir())


async def test_disabled_ncbi_blocks_preparer_with_zero_requests(router, tmp_path):
    svc = svc_for(router, tmp_path, sources={"ncbi_datasets": {"enabled": False}})
    with pytest.raises(GenomicsError) as ei:
        await svc.registry.component(PREPARER_COMPONENT).prepare(ACC, ctx_for(svc))
    assert ei.value.error_code == "unsupported" and ei.value.info.source == "ncbi_datasets"
    assert router.requests == [] and not (tmp_path / "work" / "catalogs").exists()
