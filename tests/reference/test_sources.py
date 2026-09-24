"""gnomAD, UniProt, Ensembl, Open Targets and AlphaGenome Atlas mappings."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from genomics_mcp.references.adapters.atlas import AtlasClient, build_filter
from genomics_mcp.references.http import SourceFailure
from genomics_mcp.references.models import CanonicalVariant, ReferenceCheck, VcfRepresentation
from genomics_mcp.references.schemas import LookupProteinRequest

from .conftest import Router, load_json

pytestmark = pytest.mark.asyncio
GNOMAD = r"gnomad\.broadinstitute\.org/api"


def variant(
    assembly: str = "GRCh38",
    *,
    pos: int = 140753336,
    ref: str = "A",
    alt: str = "T",
    klass: str = "SNV",
) -> CanonicalVariant:
    return CanonicalVariant(
        assembly=assembly,
        contig="7",
        start=pos - 1,
        end=pos - 1 + len(ref),
        ref=ref,
        alt=alt,
        variant_class=klass,
        normalization_status="reference_normalized",
        reference_check=ReferenceCheck(status="verified"),
        vcf=VcfRepresentation(contig="7", pos=pos, ref=ref, alt=alt),
    )


# ---------------------------------------------------------------- gnomAD


async def test_gnomad_keeps_counts_and_denominators(router: Router, make_service) -> None:
    router.json("POST", GNOMAD, load_json("gnomad_variant_7-140753336-A-T.json"))
    ev = await make_service().gnomad.variant(variant())
    assert ev.source_record_id == "7-140753336-A-T"
    assert ev.source_release == "dataset gnomad_r4"
    exome, joint = ev.data["exome"], ev.data["joint"]
    assert (exome["ac"], exome["an"]) == (2, 1460618)
    assert exome["af"] == pytest.approx(2 / 1460618)
    nfe = next(p for p in exome["populations"] if p["id"] == "nfe")
    assert (nfe["ac"], nfe["an"]) == (1, 1111098)
    assert "genome" not in ev.data
    assert "No genome data for this variant in gnomad_r4." in ev.limitations
    # The joint block repeats XX/XY rows in the real response; exact duplicates are dropped visibly.
    assert "af" not in joint or joint.get("af") is None
    assert joint["af_derived"] == pytest.approx(2 / 1612922)
    ops = [t.operation for t in ev.transformations]
    assert "drop_duplicate_population_rows" in ops and "af_derived" in ops
    sent = router.called("gnomad")[0]
    assert b'"dataset":"gnomad_r4"' in sent.content.replace(b" ", b"")


async def test_gnomad_missing_denominator_is_not_turned_into_a_frequency(
    router: Router, make_service
) -> None:
    body = {
        "data": {
            "variant": {
                "variant_id": "7-140753336-A-T",
                "reference_genome": "GRCh38",
                "chrom": "7",
                "pos": 140753336,
                "ref": "A",
                "alt": "T",
                "exome": {
                    "ac": 3,
                    "an": None,
                    "af": None,
                    "populations": [{"id": "afr", "ac": 1, "an": 0}],
                },
                "genome": None,
                "joint": None,
            }
        }
    }
    router.json("POST", GNOMAD, body)
    ev = await make_service().gnomad.variant(variant())
    assert "af_derived" not in ev.data["exome"]
    assert ev.data["exome"]["ac"] == 3
    assert any("allele number is missing or zero" in lim for lim in ev.limitations)
    assert any("populations without allele number: afr" in lim for lim in ev.limitations)


async def test_gnomad_not_found_and_build_mismatch(router: Router, make_service) -> None:
    router.json(
        "POST", GNOMAD, {"errors": [{"message": "Variant not found"}], "data": {"variant": None}}
    )
    service = make_service()
    with pytest.raises(SourceFailure) as exc:
        await service.gnomad.variant(variant())
    assert exc.value.error.kind == "not_found"
    with pytest.raises(SourceFailure) as exc:
        await service.gnomad.variant(variant(), dataset="gnomad_r2_1")
    assert exc.value.error.kind == "invalid_input" and "No liftover" in exc.value.error.message
    assert len(router.calls) == 1


async def test_gnomad_rejects_a_different_returned_allele(router: Router, make_service) -> None:
    body = load_json("gnomad_variant_7-140753336-A-T.json")
    body["data"]["variant"]["alt"] = "C"
    router.json("POST", GNOMAD, body)
    with pytest.raises(SourceFailure) as exc:
        await make_service().gnomad.variant(variant())
    assert exc.value.error.kind == "invalid_response"


async def test_gnomad_constraint(router: Router, make_service) -> None:
    router.json("POST", GNOMAD, load_json("gnomad_constraint_BRCA2.json"))
    ev = await make_service().gnomad.constraint("ENSG00000139618", "GRCh38")
    assert ev.data["constraint"]["oe_lof_upper"] == pytest.approx(0.8240908463710617)
    assert ev.source_record_version == "17"


# --------------------------------------------------------------- UniProt


async def test_uniprot_entry_identity_function_features(router: Router, make_service) -> None:
    router.json(
        "GET",
        r"rest\.uniprot\.org/uniprotkb/P51587\.json",
        load_json("uniprot_P51587.json"),
        headers={"x-uniprot-release": "2026_03", "x-uniprot-release-date": "02-September-2026"},
    )
    result = await make_service().lookup_protein(LookupProteinRequest(protein="P51587"))
    assert result.status == "ok"
    ev = result.evidence[0]
    assert ev.source_release == "UniProt 2026_03 (02-September-2026)"
    assert ev.source_record_version == "252"
    d = ev.data
    assert d["reviewed"] is True and d["entry_type"] == "UniProtKB reviewed (Swiss-Prot)"
    assert d["recommended_name"] == "Breast cancer type 2 susceptibility protein"
    assert d["sequence"]["length"] == 3418
    assert d["function"] and "Chromosome" in d["subcellular_locations"]
    chain = next(f for f in d["features"] if f["type"] == "Chain")
    assert (chain["start"], chain["end"]) == (0, 3418)  # 1..3418 inclusive -> 0-based half-open
    assert not any(f["type"] == "Natural variant" for f in d["features"])
    assert d["feature_counts"]["Natural variant"] == 3
    assert {"ENST00000380152.8"} <= {x["id"] for x in d["cross_references"]["Ensembl"]}


async def test_uniprot_merged_accession_is_followed_explicitly(
    router: Router, make_service
) -> None:
    router.json(
        "GET", r"uniprotkb/Q13879\.json", load_json("uniprot_Q13879_inactive.json"), status=303
    )
    router.json("GET", r"uniprotkb/P51587\.json", load_json("uniprot_P51587.json"))
    result = await make_service().lookup_protein(LookupProteinRequest(protein="Q13879"))
    assert result.protein["accession"] == "P51587"
    assert "accession_merged" in [t.operation for t in result.evidence[0].transformations]


async def test_uniprot_gene_search_selects_only_a_unique_reviewed_entry(
    router: Router, make_service
) -> None:
    router.json("GET", r"rest\.genenames\.org/info$", load_json("hgnc_info.json"))
    router.json(
        "GET",
        r"rest\.genenames\.org/fetch/symbol/",
        {
            "response": {
                "docs": [
                    {"hgnc_id": "HGNC:1", "symbol": "XYZ", "status": "Approved", "uniprot_ids": []}
                ]
            }
        },
    )
    router.json(
        "GET", r"rest\.genenames\.org/fetch/(alias|prev)_symbol/", load_json("hgnc_empty.json")
    )
    router.json("GET", r"uniprotkb/search", load_json("uniprot_search_BRCA2.json"))
    router.json("GET", r"uniprotkb/P51587\.json", load_json("uniprot_P51587.json"))
    result = await make_service().lookup_protein(LookupProteinRequest(protein="XYZ"))
    assert result.protein["accession"] == "P51587"
    assert len(result.candidates) > 1
    assert {c.match_type for c in result.candidates} == {"reviewed", "unreviewed"}


async def test_uniprot_404(router: Router, make_service) -> None:
    router.json("GET", r"uniprotkb/Q00000\.json", {"messages": ["Resource not found"]}, status=404)
    result = await make_service().lookup_protein(LookupProteinRequest(protein="Q00000"))
    assert result.status == "unresolved"
    assert result.errors[0].kind == "not_found"


# --------------------------------------------------------------- Ensembl


async def test_vep_consequences_are_versioned_and_labelled(router: Router, make_service) -> None:
    router.json(
        "GET",
        r"grch37\.rest\.ensembl\.org/vep/",
        load_json("ensembl_grch37_vep_7-140453136-T.json"),
    )
    router.json("GET", r"grch37\.rest\.ensembl\.org/info/software", {"release": 116})
    ev = await make_service().ensembl.vep(variant("GRCh37", pos=140453136))
    assert ev.source_release == "Ensembl REST release 116"
    first = ev.data["transcript_consequences"][0]
    assert first["transcript_id"] == "ENST00000288602" and first["canonical"] is True
    assert first["transcript_version"] == "ENST00000288602.6"
    assert first["hgvsp"] == "ENSP00000288602.6:p.Val600Glu"
    assert ev.data["most_severe_consequence"] == "missense_variant"
    assert any("predictions, not observations" in lim for lim in ev.limitations)
    assert router.called(r"vep/human/region/7:140453136-140453136:1/T")


async def test_vep_region_notation_for_indels() -> None:
    from genomics_mcp.references.adapters.ensembl import EnsemblClient

    ins = variant(pos=101, ref="", alt="GG", klass="insertion").model_copy(
        update={"start": 100, "end": 100}
    )
    dele = variant(pos=101, ref="CA", alt="", klass="deletion").model_copy(
        update={"start": 100, "end": 102}
    )
    assert EnsemblClient.vep_region(ins) == ("7:101-100:1", "GG")
    assert EnsemblClient.vep_region(dele) == ("7:101-102:1", "-")


async def test_ensembl_gene_lookup_is_zero_based_and_versioned(
    router: Router, make_service
) -> None:
    router.json("GET", r"rest\.ensembl\.org/info/software", {"release": 116})
    record = load_json("ensembl_grch38_lookup_BRCA2.json")
    service = make_service()
    ev = await service.ensembl.entity_evidence(record, "GRCh38", requested_id="ENSG00000139618.15")
    assert ev.data["id"] == "ENSG00000139618.19"
    assert (ev.data["start"], ev.data["end"]) == (32315085, 32400268)
    assert ev.data["transcripts"][0]["is_canonical"] is True
    assert any("differs from the current Ensembl version" in lim for lim in ev.limitations)


# ----------------------------------------------------------- Open Targets


async def test_open_targets_associations_and_release(router: Router, make_service) -> None:
    router.json("POST", r"api\.platform\.opentargets\.org", load_json("opentargets_BRCA2.json"))
    ev = await make_service().opentargets.associations("ENSG00000139618", size=3)
    assert ev.source_release == "Open Targets Platform data 26.09 (API 26.9.0)"
    assert ev.data["association_count"] == 1240
    top = ev.data["associations"][0]
    assert top["disease_id"] == "MONDO_0007254" and "genetic_association" in top["datatype_scores"]
    assert ev.truncation[0].available == 1240


async def test_open_targets_unknown_target(router: Router, make_service) -> None:
    router.json("POST", r"api\.platform\.opentargets\.org", {"data": {"meta": {}, "target": None}})
    with pytest.raises(SourceFailure) as exc:
        await make_service().opentargets.associations("ENSG00000000000")
    assert exc.value.error.kind == "not_found"


# ------------------------------------------------------------------ Atlas


class FakeAtlas:
    """Implements only the two named AtlasService operations; records calls."""

    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.error = error

    async def get_dense_variant_scores(self, **kwargs):
        self.calls.append(("get_dense_variant_scores", kwargs))
        if self.error:
            raise self.error
        genes = SimpleNamespace(
            metadata=[
                SimpleNamespace(
                    gene_id="ENSG00000157764", name="BRAF", HasField=lambda f: f == "name"
                )
            ]
        )
        tracks = SimpleNamespace(metadata=[SimpleNamespace(name="t0"), SimpleNamespace(name="t1")])
        return SimpleNamespace(
            variant=SimpleNamespace(
                chromosome="chr7", position=140753336, reference_bases="A", alternate_bases="T"
            ),
            HasField=lambda f: False,
            scores=[
                SimpleNamespace(
                    variant_scorer=SimpleNamespace(name="GENE_SCORER", is_signed=True),
                    shape=[1, 2],
                    scores=struct.pack("<2f", 0.25, -1.5),
                    calibrated_scores=struct.pack("<2f", 0.6, 0.99),
                    metadata=[
                        SimpleNamespace(payload="gene_scorers", gene_scorers=genes),
                        SimpleNamespace(payload="tracks", tracks=tracks),
                    ],
                )
            ],
        )

    async def list_variant_scores_metadata(self, **kwargs):
        self.calls.append(("list_variant_scores_metadata", kwargs))
        return SimpleNamespace(variant_scorer_metadata=[])


class FakeRpcError(Exception):
    def __init__(self, name: str, details: str):
        self._name, self._details = name, details

    def code(self):
        return SimpleNamespace(name=self._name)

    def details(self):
        return self._details


async def test_atlas_decodes_precomputed_scores_with_labels() -> None:
    fake = FakeAtlas()
    ev = await AtlasClient(fake).variant_scores(variant(), requested_scorers=["GENE_SCORER"])
    assert ev.evidence_type == "functional_prediction"
    assert ev.data["live_inference"] is False
    assert ev.source_release is None and any("does not report" in lim for lim in ev.limitations)
    s = ev.data["scorers"][0]
    assert s["values_row_major"] == [0.25, -1.5]
    assert s["calibrated_quantiles_row_major"] == pytest.approx([0.6, 0.99])
    assert s["top_by_abs_score"][0] == {
        "index": 1,
        "row": "ENSG00000157764 (BRAF)",
        "column": "t1",
        "score": -1.5,
        "quantile": pytest.approx(0.99),
    }
    assert fake.calls[0][1]["filter"] == '(scores.variant_scorer.name = "GENE_SCORER")'
    assert fake.calls[0][1]["chromosome"] == "chr7" and fake.calls[0][1]["position"] == 140753336
    assert any("alphagenome.google/terms" in lim for lim in ev.limitations)


async def test_atlas_requires_explicit_configuration_and_supported_input() -> None:
    with pytest.raises(SourceFailure) as exc:
        await AtlasClient(None).variant_scores(variant())
    assert exc.value.error.kind == "not_configured"
    fake = FakeAtlas()
    for v in (variant("GRCh37", pos=140453136), variant(ref="AC", alt="A", klass="deletion")):
        with pytest.raises(SourceFailure) as exc:
            await AtlasClient(fake).variant_scores(v)
        assert exc.value.error.kind == "unsupported"
    assert fake.calls == []
    with pytest.raises(SourceFailure):
        build_filter(['x" OR 1=1'])


async def test_atlas_grpc_errors_are_typed_and_redact_the_key() -> None:
    key = SecretStr("AIzaSECRETKEY")
    fake = FakeAtlas(FakeRpcError("UNAUTHENTICATED", "API key AIzaSECRETKEY not valid"))
    with pytest.raises(SourceFailure) as exc:
        await AtlasClient(fake, secret=key).variant_scores(variant())
    assert exc.value.error.kind == "unauthorized"
    assert "AIzaSECRETKEY" not in exc.value.error.model_dump_json()


async def test_atlas_not_called_by_default_and_unconfigured_when_requested(
    router: Router, make_service
) -> None:
    from genomics_mcp.references.schemas import LookupVariantRequest

    result = await make_service().lookup_variant(
        LookupVariantRequest(
            variant="7-140753336-A-T",
            assembly="GRCh38",
            use_remote_reference=False,
            sources=["alphagenome_atlas"],
        )
    )
    assert router.calls == []
    assert [(e.source, e.kind) for e in result.errors] == [("alphagenome_atlas", "not_configured")]


async def test_uniprot_401_is_unauthorized(router: Router, make_service) -> None:
    router.add("GET", r"uniprotkb/P51587\.json", httpx.Response(401, json={"messages": ["no"]}))
    result = await make_service().lookup_protein(LookupProteinRequest(protein="P51587"))
    assert result.errors[0].kind == "unauthorized"
