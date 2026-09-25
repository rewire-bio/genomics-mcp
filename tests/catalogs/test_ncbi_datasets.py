"""NCBI Datasets: versioned assembly reports, packages that are not FASTA, explicit preparation."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest
from catalog_helpers import Router, json_response

from genomics_mcp.archives._common.errors import BudgetExceededError, NotFoundError, UpstreamError
from genomics_mcp.catalogs.ncbi_datasets import BASE, NcbiDatasetsClient

ACC = "GCF_000819615.1"
REPORT = {
    "reports": [
        {
            "accession": ACC,
            "current_accession": ACC,
            "paired_accession": "GCA_000819615.1",
            "source_database": "SOURCE_DATABASE_REFSEQ",
            "organism": {"tax_id": 1, "organism_name": "phage"},
            "assembly_info": {
                "assembly_name": "ViralProj14015",
                "assembly_status": "current",
                "bioproject_accession": "PRJNA1",
            },
        }
    ],
    "total_count": 1,
}
FASTA = b">NC_001422.1 synthetic\nACGTACGTAC\nACGT\n"


def ncbi(router: Router) -> NcbiDatasetsClient:
    c = NcbiDatasetsClient(router.client())
    c.http.limiter.min_interval_s = 0
    return c


def package(members: dict[str, bytes], md5: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
        sums = (
            md5 if md5 is not None else {n: hashlib.md5(d).hexdigest() for n, d in members.items()}
        )
        z.writestr("md5sum.txt", "".join(f"{v}  {k}\n" for k, v in sums.items()))
    return buf.getvalue()


def summary_route(router: Router, size_mb: float = 0.01) -> None:
    router.add(
        "GET",
        f"{BASE}/genome/accession/{ACC}/download_summary",
        json_response(
            {
                "record_count": 1,
                "available_files": {
                    "all_genomic_fasta": {"file_count": 1, "size_mb": size_mb},
                    "genome_gff": {"file_count": 1, "size_mb": 0.01},
                },
            },
            headers={"x-datasets-version": "18.37.0"},
        ),
    )


async def test_empty_object_is_not_found(router):
    router.add("GET", f"{BASE}/genome/accession/GCF_999999999.1/dataset_report", json_response({}))
    with pytest.raises(NotFoundError):
        await ncbi(router).describe_dataset("GCF_999999999.1")


async def test_describe_keeps_versions_and_insdc_linkage(router):
    router.add(
        "GET",
        f"{BASE}/genome/accession/{ACC}/dataset_report",
        json_response(REPORT, headers={"x-datasets-version": "18.37.0"}),
    )
    d = await ncbi(router).describe_dataset(ACC)
    assert d.dataset.assemblies == [ACC, "ViralProj14015"]
    assert d.related["insdc_accession"] == "GCA_000819615.1"
    assert d.provenance[0].source_version == "datasets 18.37.0"
    assert {(link.relation, link.kind) for link in d.dataset.links} == {
        ("paired_assembly", "reference"),
        ("bioproject", "study"),
    }


async def test_packages_are_listed_as_zip_not_ready(router):
    summary_route(router)
    page = await ncbi(router).list_files(ACC)
    assert {f.native["annotation_type"] for f in page.items} == {"GENOME_FASTA", "GENOME_GFF"}
    assert all(f.readiness.state == "download_required" and f.format == "other" for f in page.items)
    assert all(f.native["container"] == "zip" for f in page.items)


async def test_assembly_without_biosample_is_empty_with_warning(router):
    router.add("GET", f"{BASE}/genome/accession/{ACC}/dataset_report", json_response(REPORT))
    page = await ncbi(router).list_samples(ACC)
    assert page.items == [] and page.warnings


async def test_prepare_genome_fasta_verifies_extracts_and_indexes(router, tmp_path):
    summary_route(router)
    member = f"ncbi_dataset/data/{ACC}/{ACC}_genomic.fna"
    router.add(
        "GET",
        f"{BASE}/genome/accession/{ACC}/download",
        httpx.Response(200, content=package({member: FASTA, "README.md": b"r"})),
    )
    art = await ncbi(router).prepare_genome_fasta(ACC, workspace=tmp_path, budget_bytes=100_000)
    assert Path(art.path).read_bytes() == FASTA and art.checksum_verified and art.format == "fasta"  # noqa: ASYNC240 - small local test file
    assert Path(art.index_path).read_text().startswith("NC_001422.1\t14\t")  # noqa: ASYNC240 - small local test file
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        f"{ACC}.fna",
        f"{ACC}.fna.fai",
    ]  # ZIP removed
    assert router.requests[-1].url.params["include_annotation_type"] == "GENOME_FASTA"


async def test_prepare_rejects_md5_mismatch(router, tmp_path):
    summary_route(router)
    member = f"ncbi_dataset/data/{ACC}/{ACC}_genomic.fna"
    router.add(
        "GET",
        f"{BASE}/genome/accession/{ACC}/download",
        httpx.Response(200, content=package({member: FASTA}, md5={member: "0" * 32})),
    )
    with pytest.raises(UpstreamError):
        await ncbi(router).prepare_genome_fasta(ACC, workspace=tmp_path, budget_bytes=100_000)
    assert list(tmp_path.iterdir()) == []


async def test_prepare_ignores_traversal_members(router, tmp_path):
    summary_route(router)
    router.add(
        "GET",
        f"{BASE}/genome/accession/{ACC}/download",
        httpx.Response(
            200,
            content=package(
                {"../../evil.fna": FASTA, f"ncbi_dataset/data/{ACC}/../../x.fna": FASTA}
            ),
        ),
    )
    ws = tmp_path / "ws"
    with pytest.raises(UpstreamError):
        await ncbi(router).prepare_genome_fasta(ACC, workspace=ws, budget_bytes=100_000)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ws"] and list(ws.iterdir()) == []


async def test_prepare_budget_checked_before_download(router, tmp_path):
    summary_route(router, size_mb=900)
    with pytest.raises(BudgetExceededError):
        await ncbi(router).prepare_genome_fasta(ACC, workspace=tmp_path, budget_bytes=1_000_000)
    assert not router.hits(f"{BASE}/genome/accession/{ACC}/download?")


async def test_taxon_search_uses_native_page_token(router):
    def report(req: httpx.Request) -> httpx.Response:
        token = req.url.params.get("page_token")
        return json_response(
            {
                "reports": REPORT["reports"],
                "total_count": 2,
                **({} if token else {"next_page_token": "tok2"}),
            }
        )

    router.add("GET", f"{BASE}/genome/taxon/Escherichia%20virus%20phiX174/dataset_report", report)
    c = ncbi(router)
    p1 = await c.search_datasets("Escherichia virus phiX174", limit=1)
    p2 = await c.search_datasets("Escherichia virus phiX174", limit=1, cursor=p1.next_cursor)
    assert router.requests[1].url.params["page_token"] == "tok2" and p2.next_cursor is None
    assert p1.total == 2
