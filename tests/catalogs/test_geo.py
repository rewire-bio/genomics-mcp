"""GEO: SOFT parsing, GSE/GSM/GPL distinction, heterogeneous characteristics and real file URLs."""

from __future__ import annotations

import httpx
import pytest
from catalog_helpers import Router, json_response

from genomics_mcp.archives._common.errors import InvalidInputError, NotFoundError, UpstreamError
from genomics_mcp.catalogs.geo import ACC_CGI, EUTILS, GeoClient, parse_soft, to_https

GSE = """^SERIES = GSE1000
!Series_title = Synthetic series
!Series_summary = s
!Series_sample_id = GSM1
!Series_sample_id = GSM2
!Series_sample_id = GSM3
!Series_supplementary_file = ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/GSE1000/suppl/GSE1000_RAW.tar
!Series_platform_id = GPL1
!Series_contact_email = someone@example.org
!Series_relation = BioProject: https://www.ncbi.nlm.nih.gov/bioproject/PRJNA1
"""


def gsm(acc: str, channels: int = 1) -> str:
    lines = [f"^SAMPLE = {acc}", f"!Sample_title = {acc} title", f"!Sample_channel_count = {channels}",
             "!Sample_organism_ch1 = Homo sapiens", "!Sample_taxid_ch1 = 9606",
             "!Sample_characteristics_ch1 = tissue: liver",
             "!Sample_characteristics_ch1 = free text without a colon",
             "!Sample_platform_id = GPL1", "!Sample_series_id = GSE1000",
             "!Sample_contact_name = Person,,Name",
             "!Sample_relation = BioSample: https://www.ncbi.nlm.nih.gov/biosample/SAMN1",
             "!Sample_relation = SRA: https://www.ncbi.nlm.nih.gov/sra?term=SRX1",
             f"!Sample_supplementary_file_1 = ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSMnnn/{acc}/suppl/{acc}_x.bw",
             "!Sample_supplementary_file_2 = NONE",
             "!Sample_data_processing = Assembly: hg19 (free text; not trusted)"]
    if channels == 2:
        lines.append("!Sample_characteristics_ch2 = tissue: reference")
    return "\n".join(lines) + "\n"


def geo(router: Router) -> GeoClient:
    c = GeoClient(router.client())
    c.http.limiter.min_interval_s = c.files.limiter.min_interval_s = 0
    return c


def soft_routes(router: Router) -> None:
    pages = {"GSE1000": GSE, "GSM1": gsm("GSM1"), "GSM2": gsm("GSM2", 2), "GSM3": gsm("GSM3")}
    router.add("GET", ACC_CGI, lambda r: httpx.Response(200, text=pages.get(
        r.url.params["acc"], '<html>Could not find a public or private accession "X"</html>')))


def test_parse_soft_skips_tables():
    ents = parse_soft("^PLATFORM = GPL1\n!Platform_title = t\n!platform_table_begin\nID\tX\n1\t2\n"
                      "!platform_table_end\n")
    assert ents == [("PLATFORM", "GPL1", {"Platform_title": ["t"]})]


def test_ftp_to_https_only_for_ncbi_host():
    assert to_https("ftp://ftp.ncbi.nlm.nih.gov/geo/x.bw") == ("https://ftp.ncbi.nlm.nih.gov/geo/x.bw", True)
    assert to_https("ftp://other.example.org/x.bw") == ("ftp://other.example.org/x.bw", False)


async def test_sample_characteristics_verbatim_links_typed_contacts_dropped(router):
    soft_routes(router)
    s = await geo(router).get_sample_metadata("GSM1")
    assert [(p.name, p.value) for p in s.phenotypes] == [("tissue", "liver"),
                                                         ("characteristics_ch1", "free text without a colon")]
    links = {(link.relation, link.kind, link.accession) for link in s.links}
    assert links == {("part_of", "study", "GSE1000"), ("biosample", "sample", "SAMN1"),
                     ("sra_experiment", "experiment", "SRX1")}
    assert all(link.accession != "GPL1" for link in s.links)  # a platform is not a reference
    assert s.native["platforms"] == ["GPL1"]
    assert not any("contact" in k.lower() for k in s.native)


async def test_two_channel_names_are_suffixed_and_recorded(router):
    soft_routes(router)
    s = await geo(router).get_sample_metadata("GSM2")
    assert [p.name for p in s.phenotypes][-1] == "tissue (ch2)"
    assert any("channel" in t for t in s.provenance[0].transformations)


async def test_list_files_series_then_samples_with_https_and_readiness(router):
    soft_routes(router)
    c = geo(router)
    p1 = await c.list_files("GSE1000", limit=2)
    p2 = await c.list_files("GSE1000", limit=2, cursor=p1.next_cursor)
    names = [f.uri.rsplit("/", 1)[-1] for f in p1.items + p2.items]
    assert names == ["GSE1000_RAW.tar", "GSM1_x.bw", "GSM2_x.bw", "GSM3_x.bw"]
    raw, bw = p1.items[0], p1.items[1]
    assert raw.readiness.state == "download_required" and raw.relationships[0].kind == "study"
    assert bw.uri.startswith("https://ftp.ncbi.nlm.nih.gov/") and bw.readiness.state == "unknown"
    assert bw.assembly is None  # free-text data processing is not parsed into an assembly
    assert (bw.relationships[0].kind, bw.relationships[0].accession) == ("sample", "GSM1")


async def test_accession_kinds_are_distinct(router):
    c = geo(router)
    with pytest.raises(InvalidInputError):
        await c.describe_dataset("GSM1")
    with pytest.raises(InvalidInputError):
        await c.describe_dataset("GPL1")
    with pytest.raises(InvalidInputError):
        await c.get_sample_metadata("GSE1000")
    assert router.requests == []


async def test_not_found_and_non_soft(router):
    soft_routes(router)
    with pytest.raises(NotFoundError):
        await geo(router).describe_dataset("GSE999")
    router.add("GET", ACC_CGI, httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(UpstreamError):
        await geo(router).describe_dataset("GSE1000")


async def test_platform_uses_esummary_and_checks_accession(router):
    router.add("GET", f"{EUTILS}/esummary.fcgi", lambda r: json_response({"result": {
        "uids": [r.url.params["id"]], r.url.params["id"]: {"accession": "GPL16791", "title": "HiSeq",
                                                           "n_samples": 5, "gse": "1;2;3"}}}))
    p = await geo(router).describe_platform("GPL16791")
    assert router.requests[0].url.params["id"] == "100016791"
    assert p["kind"] == "platform" and p["native"]["series_count"] == 3
    router.add("GET", f"{EUTILS}/esummary.fcgi", json_response({"result": {"uids": []}}))
    with pytest.raises(NotFoundError):
        await geo(router).describe_platform("GPL16791")


async def test_search_pages_natively(router):
    router.add("GET", f"{EUTILS}/esearch.fcgi", json_response({"esearchresult": {"count": "3", "idlist": ["200001"]}}))
    router.add("GET", f"{EUTILS}/esummary.fcgi", json_response({"result": {
        "uids": ["200001"], "200001": {"accession": "GSE1", "title": "t", "n_samples": 2}}}))
    page = await geo(router).search_datasets("CTCF liver", limit=1)
    q = router.requests[0].url.params
    assert q["term"] == "(CTCF liver) AND gse[Entry Type]" and q["retstart"] == "0" and q["retmax"] == "1"
    assert page.total == 3 and page.items[0].accession == "GSE1" and page.next_cursor
    await geo(router).search_datasets("CTCF liver", limit=1, cursor=page.next_cursor)
    assert router.requests[2].url.params["retstart"] == "1"
