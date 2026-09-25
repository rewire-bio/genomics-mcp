"""Private-file egress consent for inspect_locus annotation, with transport spies."""

from __future__ import annotations

from genomics_mcp.errors import ErrorCode
from genomics_mcp.registry import Operation
from genomics_mcp.result import ResultStatus

from .conftest import iv, ref
from .fixtures import COHORT_ROWS, dense_vcf

EUTILS = "eutils.ncbi.nlm.nih.gov"


def annotation(body):
    return body["data"]["annotation"]


def variant_records(body, cid="f0.variants"):
    return [r for r in body["data"]["records"] if r["component"] == cid]


async def test_private_without_consent_sends_nothing_and_keeps_local_data(make_service, fx, spy):
    res = await make_service().call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "files": [ref(fx.cohort_vcf)],  # private by default
            "reference": ref(fx.fasta, assembly="GRCh38", visibility="public"),
            "reference_sources": ["clinvar", "gnomad", "ensembl"],
        },
    )
    body = res.model_dump(mode="json")
    assert spy.requests == []
    assert res.status is ResultStatus.PARTIAL
    assert len(variant_records(body)) == len(COHORT_ROWS)
    consent = [e for e in body["errors"] if e["code"] == "consent_required"]
    assert len(consent) == 1 and "allow_external_annotation" in consent[0]["hint"]
    skipped = annotation(body)["external_skipped"]
    assert skipped["sources"] == ["clinvar", "gnomad", "ensembl"]
    assert len(skipped["alleles"]) == 5  # 4 records, one multi-allelic
    states = {s["source"]: s["state"] for s in body["source_status"]}
    assert states["clinvar"] == states["gnomad"] == "skipped"


async def test_private_without_consent_can_still_normalize_locally(make_service, fx, spy):
    res = await make_service().call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "files": [ref(fx.cohort_vcf)],
            "reference": ref(fx.fasta, assembly="GRCh38"),
            "reference_sources": ["local_fasta", "clinvar"],
        },
    )
    body = res.model_dump(mode="json")
    assert spy.requests == []
    comps = annotation(body)["components"]
    assert comps and all(c["mode"] == "local_normalization" for c in comps)
    assert all(c["status"] in ("ok", "partial") for c in comps), comps
    assert comps[0]["result"]["canonical_variant"] is not None
    assert annotation(body)["external_skipped"]["sources"] == ["clinvar"]


async def test_explicit_consent_permits_only_the_requested_source(make_service, fx, spy):
    res = await make_service().call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "files": [ref(fx.cohort_vcf)],
            "reference": ref(fx.fasta, assembly="GRCh38"),
            "reference_sources": ["clinvar"],
            "allow_external_annotation": True,
        },
    )
    body = res.model_dump(mode="json")
    assert spy.requests, "consented annotation should reach ClinVar"
    assert spy.hosts == {EUTILS}
    assert "external_skipped" not in annotation(body)
    comps = annotation(body)["components"]
    assert [c["mode"] for c in comps] == ["lookup_variant"] * len(comps)
    assert all(c["sources"] == ["clinvar"] for c in comps)
    assert not any(e["code"] == "consent_required" for e in body["errors"])
    assert variant_records(body)


async def test_public_files_need_no_consent_but_private_reference_does(make_service, fx, spy):
    svc = make_service()
    public = {
        "interval": iv(100, 200),
        "files": [ref(fx.cohort_vcf, visibility="public")],
        "reference": ref(fx.fasta, assembly="GRCh38", visibility="public"),
        "reference_sources": ["clinvar"],
    }
    await svc.call(Operation.INSPECT_LOCUS, public)
    assert spy.hosts == {EUTILS}

    spy.requests.clear()
    private_ref = {**public, "reference": ref(fx.fasta, assembly="GRCh38")}
    res = await svc.call(Operation.INSPECT_LOCUS, private_ref)
    assert spy.requests == []
    assert any(e.code == ErrorCode.CONSENT_REQUIRED for e in res.errors)


async def test_bare_locus_is_reported_unanswerable(make_service, spy):
    res = await make_service().call(
        Operation.INSPECT_LOCUS,
        {"interval": iv(100, 200), "reference_sources": ["clinvar", "hgnc"]},
    )
    assert spy.requests == []
    assert res.status is ResultStatus.ERROR
    messages = " ".join(e.message for e in res.errors)
    assert "not bare loci" in messages
    states = {s.source: s.state.value for s in res.source_status}
    assert states["hgnc"] == "not_implemented" and states["clinvar"] == "skipped"


async def test_dense_region_is_bounded_and_deterministic(make_service, fx, spy, data_root):
    vcf = dense_vcf(data_root, 30)
    call = {
        "interval": iv(150, 400),
        "files": [ref(vcf)],
        "reference": ref(fx.fasta, assembly="GRCh38"),
        "reference_sources": ["clinvar"],
        "allow_external_annotation": True,
    }
    svc = make_service()
    first = (await svc.call(Operation.INSPECT_LOCUS, call)).model_dump(mode="json")
    sel = annotation(first)["selection"]
    assert sel["observed_alleles"] == 30 and len(sel["selected"]) == 5
    assert sel["omitted_count"] == 25
    assert [int(k.split(":")[1]) for k in sel["selected"]] == [200, 203, 206, 209, 212]
    looked_up = {r.url.params.get("term") for r in spy.requests if "esearch" in r.url.path}
    assert 0 < len(looked_up) <= 5
    second = (await svc.call(Operation.INSPECT_LOCUS, call)).model_dump(mode="json")
    assert annotation(second)["selection"] == sel


async def test_compare_samples_never_sends_private_values(make_service, fx, spy):
    await make_service().call(
        Operation.COMPARE_SAMPLES,
        {"interval": iv(100, 300), "files": [ref(fx.cohort_vcf), ref(fx.deep_bam)]},
    )
    assert spy.requests == []
