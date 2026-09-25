"""ENCODE: file conversion, typed relationships, range-verified readiness and biosample traversal."""

from __future__ import annotations

import httpx
import pytest
from catalog_helpers import Router, json_response

from genomics_mcp.archives._common.errors import NotFoundError
from genomics_mcp.archives._common.models import Provenance
from genomics_mcp.catalogs.encode import BASE, EncodeClient, dataset_kind

S3 = "https://encode-public.s3.amazonaws.com/2012/07/30/x/ENCFF000AAA.bigBed"
PROV = Provenance(source="encode")


def enc(router: Router) -> EncodeClient:
    c = EncodeClient(router.client())
    c.http.limiter.min_interval_s = c.data.limiter.min_interval_s = 0
    return c


def file_obj(**kw) -> dict:
    base = {
        "@id": "/files/ENCFF000AAA/",
        "accession": "ENCFF000AAA",
        "file_format": "bigBed",
        "assembly": "mm9",
        "file_size": 1000,
        "md5sum": "a" * 32,
        "href": "/files/ENCFF000AAA/@@download/ENCFF000AAA.bigBed",
        "status": "archived",
        "dataset": "/experiments/ENCSR000AAA/",
        "cloud_metadata": {"url": S3, "file_size": 1000},
        "azure_uri": "https://x.blob.core.windows.net/d/f?sv=1&sig=SASSECRET",
        "derived_from": ["/files/ENCFF000BBB/"],
    }
    base.update(kw)
    return base


def test_file_uses_unsigned_url_drops_sas_and_types_links():
    f = EncodeClient.__new__(EncodeClient)._file(file_obj(), PROV)
    assert (
        f.uri == S3
        and f.native["href"] == f"{BASE}/files/ENCFF000AAA/@@download/ENCFF000AAA.bigBed"
    )
    assert "azure_uri" not in f.native and "SASSECRET" not in str(f.model_dump())
    assert f.assembly == "mm9" and f.format == "bigbed" and f.readiness.state == "unknown"
    assert {(link.relation, link.kind, link.accession) for link in f.relationships} == {
        ("part_of", "experiment", "ENCSR000AAA"),
        ("derived_from", "file", "ENCFF000BBB"),
    }


def test_annotation_dataset_is_not_called_an_experiment():
    f = EncodeClient.__new__(EncodeClient)._file(
        file_obj(dataset="/annotations/ENCSR000ABC/"), PROV
    )
    assert f.relationships[0].kind == "dataset"
    assert dataset_kind({"@id": "/references/ENCSR1/"}) == "dataset"
    assert dataset_kind("/experiments/ENCSR2/") == "experiment"


@pytest.mark.parametrize(
    ("kw", "state"),
    [
        ({"file_format": "bam"}, "index_required"),
        ({"file_format": "fastq"}, "not_locus_ready"),
        ({"file_format": "tsv"}, "download_required"),
        ({"restricted": True}, "unsupported"),
    ],
)
def test_listed_readiness_is_conservative(kw, state):
    f = EncodeClient.__new__(EncodeClient)._file(file_obj(**kw), PROV)
    assert f.readiness.state == state


@pytest.mark.parametrize(
    ("resp", "state"),
    [
        (
            httpx.Response(
                206,
                content=b"\xeb\xf2\x89\x87" + b"\0" * 60,
                headers={"content-range": "bytes 0-63/1000"},
            ),
            "ready",
        ),
        (httpx.Response(200, content=b"\xeb\xf2\x89\x87" + b"\0" * 60), "download_required"),
        (
            httpx.Response(
                206, content=b"<html>" + b"\0" * 58, headers={"content-range": "bytes 0-63/1000"}
            ),
            "unsupported",
        ),
        (
            httpx.Response(
                206,
                content=b"\xeb\xf2\x89\x87" + b"\0" * 60,
                headers={"content-range": "bytes 0-63/999"},
            ),
            "unknown",
        ),
    ],
)
async def test_check_file_is_based_on_actual_probe(router, resp, state):
    router.add("GET", S3, resp)
    f = enc(router)._file(file_obj(), PROV)
    out = await enc(router).check_file(f)
    assert out.readiness.state == state and out.readiness.checked_at is not None
    assert router.requests[0].headers["range"] == "bytes=0-63"


async def test_list_samples_follows_replicate_library_biosample(router):
    bio = {
        "accession": "ENCBS000AAA",
        "sex": "female",
        "age": "14",
        "age_units": "day",
        "summary": "s",
        "organism": {"scientific_name": "Mus musculus", "taxon_id": "10090"},
        "biosample_ontology": {"term_name": "liver", "term_id": "UBERON:0002107"},
        "donor": {"accession": "ENCDO000AAA", "@id": "/mouse-donors/ENCDO000AAA/"},
    }
    exp = {
        "@id": "/experiments/ENCSR000AAA/",
        "accession": "ENCSR000AAA",
        "replicates": [
            {
                "@id": "/replicates/r1/",
                "biological_replicate_number": 1,
                "technical_replicate_number": 1,
                "library": {"accession": "ENCLB1", "biosample": bio},
            },
            {
                "@id": "/replicates/r2/",
                "biological_replicate_number": 1,
                "technical_replicate_number": 2,
                "library": {"accession": "ENCLB2", "biosample": bio},
            },
        ],
    }
    router.add("GET", f"{BASE}/ENCSR000AAA/", json_response(exp))
    page = await enc(router).list_samples("ENCSR000AAA")
    (s,) = page.items
    assert s.taxon_id == 10090 and [r["library"] for r in s.native["replicates"]] == [
        "ENCLB1",
        "ENCLB2",
    ]
    phen = {p.name: (p.value, p.ontology_term) for p in s.phenotypes}
    assert phen["sex"] == ("female", None) and phen["biosample_term_name"] == (
        "liver",
        "UBERON:0002107",
    )
    assert {(link.relation, link.kind, link.accession) for link in s.links} == {
        ("replicate_of", "experiment", "ENCSR000AAA"),
        ("donor", "individual", "ENCDO000AAA"),
    }


async def test_missing_object_is_not_found(router):
    router.add("GET", f"{BASE}/ENCSR999ZZZ/", json_response({"@type": ["HTTPNotFound"]}, 404))
    with pytest.raises(NotFoundError):
        await enc(router).describe_dataset("ENCSR999ZZZ")


async def test_list_files_pages_sorted_accessions(router):
    router.add(
        "GET",
        f"{BASE}/ENCSR000AAA/",
        json_response({"@id": "/experiments/ENCSR000AAA/", "accession": "ENCSR000AAA"}),
    )

    def search(req: httpx.Request) -> httpx.Response:
        wanted = req.url.params.get_list("accession")
        if not wanted:
            ids = ["ENCFF000CCC", "ENCFF000AAA", "ENCFF000BBB"]
            return json_response({"total": 3, "@graph": [{"accession": a} for a in ids]})
        return json_response(
            {"@graph": [file_obj(accession=a, **{"@id": f"/files/{a}/"}) for a in wanted]}
        )

    router.add("GET", f"{BASE}/search/", search)
    c = enc(router)
    p1 = await c.list_files("ENCSR000AAA", limit=2)
    p2 = await c.list_files("ENCSR000AAA", limit=2, cursor=p1.next_cursor)
    assert [f.accession for f in p1.items + p2.items] == [
        "ENCFF000AAA",
        "ENCFF000BBB",
        "ENCFF000CCC",
    ]
    assert p1.total == 3 and p2.next_cursor is None
