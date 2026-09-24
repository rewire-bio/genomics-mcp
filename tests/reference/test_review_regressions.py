"""Regressions for the independent E8 review (2026-09-24) and follow-up requirements:
private-data egress consent, short supplied references, contig-start VCF anchors,
streamed byte bounds, deadline isolation and no build substitution."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from genomics_mcp.references import ReferenceConfig, ReferenceService
from genomics_mcp.references.http import RateLimiter, SourceFailure, SourceHttp
from genomics_mcp.references.schemas import (
    LookupGeneRequest,
    LookupVariantRequest,
    NormalizeVariantRequest,
)
from genomics_mcp.references.variants import (
    RawAllele,
    ReferenceWindow,
    normalize_allele,
    structured_allele,
)

from .conftest import FakeClock, Router, html_500, load_json, ncbi_fasta

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------ consent


@pytest.mark.parametrize(
    "kwargs",
    [
        # Review reproduction 1: local normalization, then gnomAD annotation.
        {"variant": "7-140753336-A-T", "use_remote_reference": False, "sources": ["gnomad"]},
        # Default remote reference retrieval plus every annotation source.
        {"variant": "7-140753336-A-T", "sources": ["ensembl", "clinvar", "gnomad"]},
        # Review reproduction 2: genomic HGVS on NG_ resolved through NCBI.
        {"variant": "NG_007807.2:g.100A>T", "use_remote_reference": False},
        {"variant": "rs113488022"},
        {"variant": "NM_004333.6:c.1799T>A"},
        {"variant": "NP_004324.2:p.Val600Glu"},
    ],
)
async def test_private_variant_without_consent_makes_zero_requests(
    router: Router, make_service, kwargs
) -> None:
    request = LookupVariantRequest(
        assembly="GRCh38", query_origin="private_file", allow_external_queries=False, **kwargs
    )
    result = await make_service().lookup_variant(request)
    assert router.calls == []
    assert result.status == "error"
    assert any(e.kind == "forbidden" and e.operation == "egress_check" for e in result.errors)


async def test_private_normalize_without_consent_is_local_only(
    router: Router, make_service
) -> None:
    result = await make_service().normalize_variant(
        NormalizeVariantRequest(
            variant="7-140753336-A-T", assembly="GRCh38", query_origin="private_file"
        )
    )
    assert router.calls == []
    assert result.status == "ok"
    assert result.canonical_variant.reference_check.status == "not_checked"
    assert any("allow_external_queries was not set" in lim for lim in result.limitations)


async def test_private_normalize_with_supplied_reference_verifies_locally(
    router: Router, make_service
) -> None:
    result = await make_service().normalize_variant(
        NormalizeVariantRequest(
            variant={"assembly": "GRCh38", "contig": "7", "start": 1000, "ref": "A", "alt": "T"},
            reference={"assembly": "GRCh38", "contig": "7", "start": 1000, "sequence": "A"},
            query_origin="private_file",
        )
    )
    assert router.calls == []
    assert result.canonical_variant.reference_check.status == "verified"


async def test_explicit_consent_permits_the_request(router: Router, make_service) -> None:
    router.json(
        "POST", r"gnomad\.broadinstitute\.org/api", load_json("gnomad_variant_7-140753336-A-T.json")
    )
    result = await make_service().lookup_variant(
        LookupVariantRequest(
            variant="7-140753336-A-T",
            assembly="GRCh38",
            use_remote_reference=False,
            sources=["gnomad"],
            query_origin="private_file",
            allow_external_queries=True,
        )
    )
    assert len(router.called(r"gnomad")) == 1
    assert result.status == "ok"
    assert result.evidence.population[0].data["exome"]["an"] == 1460618


# ------------------------------------------------------- supplied reference


def snv_request(base: str) -> NormalizeVariantRequest:
    return NormalizeVariantRequest(
        variant={"assembly": "GRCh38", "contig": "7", "start": 1000, "ref": "A", "alt": "T"},
        reference={"assembly": "GRCh38", "contig": "7", "start": 1000, "sequence": base},
        use_remote_reference=False,
    )


async def test_one_base_supplied_reference_detects_mismatch(router: Router, make_service) -> None:
    result = await make_service().normalize_variant(snv_request("C"))
    assert result.status == "error"
    assert result.canonical_variant is None
    assert result.alleles[0].reference_check.status == "mismatch"
    assert result.alleles[0].reference_check.observed_ref == "C"
    assert any(e.operation == "reference_check" for e in result.errors)
    assert router.calls == []


async def test_one_base_supplied_reference_verifies_snv(make_service) -> None:
    result = await make_service().normalize_variant(snv_request("A"))
    assert result.status == "ok"
    v = result.canonical_variant
    assert v.reference_check.status == "verified"
    assert v.normalization_status == "reference_normalized"


async def test_short_reference_covering_indel_keeps_ref_evidence(
    router: Router, make_service
) -> None:
    # REF "ACA" at 0-based 1007 is covered, but the CA repeat continues left of the window.
    result = await make_service().normalize_variant(
        NormalizeVariantRequest(
            variant={
                "assembly": "GRCh38",
                "contig": "7",
                "position": 1008,
                "ref": "ACA",
                "alt": "A",
            },
            reference={"assembly": "GRCh38", "contig": "7", "start": 1006, "sequence": "CACAG"},
            use_remote_reference=False,
        )
    )
    v = result.canonical_variant
    assert v.reference_check.status == "verified"
    assert v.normalization_status == "trimmed_only"
    assert any("too short to shift" in lim for lim in result.limitations)
    assert router.calls == []


async def test_short_reference_with_mismatching_indel_ref_is_error(make_service) -> None:
    result = await make_service().normalize_variant(
        NormalizeVariantRequest(
            variant={
                "assembly": "GRCh38",
                "contig": "7",
                "position": 1008,
                "ref": "AGA",
                "alt": "A",
            },
            reference={"assembly": "GRCh38", "contig": "7", "start": 1006, "sequence": "CACAG"},
            use_remote_reference=False,
        )
    )
    assert result.status == "error"
    assert result.alleles[0].reference_check.status == "mismatch"


# ------------------------------------------------------- contig-start VCF

START = ReferenceWindow(
    assembly="GRCh38",
    contig="7",
    start=0,
    sequence="AAAC",
    source="synthetic",
    at_contig_start=True,
    at_contig_end=True,
)


def vcf_of(position: int, ref: str, alt: str, window: ReferenceWindow = START):
    raw = structured_allele(assembly="GRCh38", contig="7", position=position, ref=ref, alt=alt)[0]
    return normalize_allele(raw, window)


@pytest.mark.parametrize(
    ("position", "ref", "alt", "expected"),
    [
        (2, "A", "AA", (1, "A", "AA")),  # review example: insertion shifted to 0 (bcftools 1.24)
        (1, "A", "AA", (1, "A", "AA")),  # insertion given at the first base
        (1, "AAAC", "C", (1, "AAAC", "C")),  # deletion starting at 0
        (3, "AC", "C", (1, "AA", "A")),  # deletion shifted to 0 from a later position
    ],
)
async def test_contig_start_indels_are_right_anchored(
    position: int, ref: str, alt: str, expected
) -> None:
    out = vcf_of(position, ref, alt)
    v = out.variant
    assert v.normalization_status == "reference_normalized"
    assert v.start == 0
    assert (v.vcf.pos, v.vcf.ref, v.vcf.alt) == expected


async def test_deleting_whole_contig_is_unanchorable_and_explained() -> None:
    whole = ReferenceWindow(
        assembly="GRCh38",
        contig="7",
        start=0,
        sequence="AAAC",
        source="synthetic",
        at_contig_start=True,
        at_contig_end=True,
    )
    raw = structured_allele(assembly="GRCh38", contig="7", start=0, ref="AAAC", alt="AAACG")[0]
    # Insertion at the very end is still left-anchorable; deletion of everything is not.
    assert normalize_allele(raw, whole).variant.vcf is not None
    gone = RawAllele(
        assembly="GRCh38", contig="7", start=0, end=4, ref="AAAC", alt="", origin="structured"
    )
    out = normalize_allele(gone, whole)
    assert out.variant.vcf is None
    assert any("no reference base is available" in lim for lim in out.limitations)


# ------------------------------------------------------ streaming byte bounds


def http_for(router: Router, clock: FakeClock, **kwargs) -> SourceHttp:
    client = httpx.AsyncClient(transport=httpx.MockTransport(router.handle), follow_redirects=True)
    return SourceHttp(
        client,
        source="test",
        limiter=RateLimiter(100, 1.0, clock=clock, sleep=clock.sleep),
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: int, size: int):
        self.chunks, self.size, self.sent = chunks, size, 0

    async def __aiter__(self):
        for _ in range(self.chunks):
            self.sent += 1
            yield b"x" * self.size


async def test_streamed_body_is_cut_at_limit_without_reading_everything(
    router: Router, clock: FakeClock
) -> None:
    stream = ChunkStream(chunks=1000, size=1024)
    router.add("GET", r"example\.org/big", lambda request: httpx.Response(200, stream=stream))
    http = http_for(router, clock, max_bytes=4096)
    with pytest.raises(SourceFailure) as exc:
        await http.request("GET", "https://example.org/big", operation="big")
    assert exc.value.error.kind == "invalid_response"
    assert "exceeds 4096 bytes" in exc.value.error.message
    assert stream.sent <= 5


async def test_declared_length_over_limit_fails_before_reading(
    router: Router, clock: FakeClock
) -> None:
    router.add("GET", r"example\.org/big", httpx.Response(200, content=b"y" * 10_000))
    http = http_for(router, clock, max_bytes=100)
    with pytest.raises(SourceFailure) as exc:
        await http.request("GET", "https://example.org/big", operation="big")
    assert "Content-Length" in exc.value.error.message


async def test_redirects_are_not_followed_even_if_client_would(
    router: Router, clock: FakeClock
) -> None:
    router.add(
        "GET",
        r"example\.org/start",
        httpx.Response(302, headers={"location": "https://elsewhere.test/x"}),
    )
    http = http_for(router, clock, max_retries=0)
    with pytest.raises(SourceFailure) as exc:
        await http.request("GET", "https://example.org/start", operation="start")
    assert exc.value.error.status_code == 302
    assert not router.called(r"elsewhere\.test")


# ---------------------------------------------------- deadline isolation


class StallingTransport(httpx.AsyncBaseTransport):
    """Routes like Router, but never answers requests to ``stall_host``."""

    def __init__(self, router: Router, stall_host: str):
        self.router, self.stall_host = router, stall_host

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == self.stall_host:
            await asyncio.sleep(3600)
        return self.router.handle(request)


async def test_stalled_source_does_not_discard_completed_results(router: Router) -> None:
    router.json("GET", r"rest\.genenames\.org/info$", load_json("hgnc_info.json"))
    router.json("GET", r"rest\.genenames\.org/fetch/symbol/", load_json("hgnc_symbol_BRCA2.json"))
    router.json(
        "GET", r"rest\.genenames\.org/fetch/(alias|prev)_symbol/", load_json("hgnc_empty.json")
    )
    router.json("POST", r"opentargets\.org", load_json("opentargets_BRCA2.json"))
    client = httpx.AsyncClient(transport=StallingTransport(router, "gnomad.broadinstitute.org"))
    service = ReferenceService(client, ReferenceConfig(deadline_seconds=0.5, max_retries=0))
    t0 = time.monotonic()
    result = await service.lookup_gene(
        LookupGeneRequest(gene="BRCA2", include=["identifiers", "disease", "constraint"])
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0
    assert result.status == "partial"
    assert {e.source for e in result.evidence} == {"hgnc", "open_targets"}
    assert [(e.source, e.kind) for e in result.errors] == [("gnomad", "timeout")]


# ------------------------------------------------------ no build substitution


async def test_ensembl_outage_falls_back_to_same_build_only(router: Router, make_service) -> None:
    seq = "N" * 64 + "A" + "N" * 64  # 0-based 140753271..140753400 around the variant
    router.add("GET", r"rest\.ensembl\.org/sequence", html_500())
    router.add(
        "GET",
        r"eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils/efetch\.fcgi",
        lambda request: ncbi_fasta(request.url.params["id"], seq),
    )
    result = await make_service(ReferenceConfig(max_retries=0)).normalize_variant(
        NormalizeVariantRequest(variant="7-140753336-A-T", assembly="GRCh38")
    )
    assert result.status == "partial"
    assert result.canonical_variant.assembly == "GRCh38"
    assert result.canonical_variant.reference_check.source == "ncbi_nuccore"
    fetches = router.called(r"efetch")
    assert [r.url.params["id"] for r in fetches] == ["NC_000007.14"]
    assert not router.called(r"grch37\.rest\.ensembl\.org")
    assert [(e.source, e.status_code) for e in result.errors] == [("ensembl", 500)]


ORACLE_START = ReferenceWindow(
    assembly="GRCh38",
    contig="7",
    start=0,
    sequence="AAACACACACAGGGGTTTT",
    source="synthetic",
    at_contig_start=True,
)


@pytest.mark.parametrize(
    ("position", "ref", "alt", "expected"),
    [
        # bcftools 1.24 results from the review oracle; the allele must stay rotated when the shift hits 0.
        (2, "AACAC", "C", (1, "AAACA", "A")),
        (2, "A", "AGTTAA", (1, "A", "AAGTTA")),
    ],
)
async def test_shift_to_contig_start_keeps_rotation(
    position: int, ref: str, alt: str, expected
) -> None:
    v = vcf_of(position, ref, alt, ORACLE_START).variant
    assert (v.vcf.pos, v.vcf.ref, v.vcf.alt) == expected
