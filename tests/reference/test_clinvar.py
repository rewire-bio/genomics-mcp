"""ClinVar VCV/SCV parsing and allele matching."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx
import pytest

from genomics_mcp.references.adapters.clinvar import parse_variation_archive
from genomics_mcp.references.http import SourceFailure
from genomics_mcp.references.models import CanonicalVariant, ReferenceCheck, VcfRepresentation

from .conftest import Router, load_bytes, load_json

pytestmark = pytest.mark.asyncio
EUTILS = r"eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils"


def braf_archive() -> ET.Element:
    return ET.fromstring(load_bytes("clinvar_vcv_13961_trimmed.xml")).find("VariationArchive")


def braf_variant(assembly: str = "GRCh38") -> CanonicalVariant:
    pos = 140753336 if assembly == "GRCh38" else 140453136
    acc = "NC_000007.14" if assembly == "GRCh38" else "NC_000007.13"
    return CanonicalVariant(
        assembly=assembly, contig="7", refseq_accession=acc, start=pos - 1, end=pos, ref="A", alt="T",
        variant_class="SNV", normalization_status="reference_normalized",
        reference_check=ReferenceCheck(status="verified", source="test"),
        vcf=VcfRepresentation(contig="7", pos=pos, ref="A", alt="T"),
        spdi=f"{acc}:{pos - 1}:A:T", ncbi_canonical_spdi=f"{acc}:{pos - 1}:A:T",
    )


async def test_real_vcv_keeps_classification_types_separate() -> None:
    evidence = parse_variation_archive(braf_archive(), "ClinVar Entrez Build260924-0125.1")
    record = evidence[0]
    assert record.evidence_type == "clinical_variant_record"
    assert (record.source_record_id, record.source_record_version) == ("VCV000013961", "143")
    agg = record.data["aggregate_classifications"]
    assert set(agg) == {"germline", "somatic_clinical_impact", "oncogenicity"}
    germline = agg["germline"]
    assert germline["review_status"] == "criteria provided, conflicting classifications"
    assert germline["description"] == "Conflicting classifications of pathogenicity"
    assert germline["explanation"] == "Pathogenic (4); Likely pathogenic (1); Uncertain significance (1)"
    assert germline["conflict_reported_by_clinvar"] is True
    assert agg["somatic_clinical_impact"]["description"] == "Tier I - Strong"
    assert agg["somatic_clinical_impact"]["conflict_reported_by_clinvar"] is False
    assert agg["oncogenicity"]["description"] == "Oncogenic"
    assert record.data["canonical_spdi"] == "NC_000007.14:140753335:A:T"
    assert "rs113488022" in record.data["xrefs"]
    assert any(loc["assembly"] == "GRCh37" and loc["position_vcf"] == "140453136" for loc in record.data["locations"])


async def test_real_scvs_keep_submitter_review_status_condition_and_type() -> None:
    evidence = parse_variation_archive(braf_archive(), None)
    scvs = {e.source_record_id: e for e in evidence if e.evidence_type == "clinical_assertion"}
    assert len(scvs) == 11
    ambry = scvs["SCV005022010"].data
    assert ambry["submitter"] == "Ambry Genetics"
    assert ambry["classification_type"] == "germline"
    assert ambry["classification"] == "Likely pathogenic"
    assert ambry["review_status"] == "criteria provided, single submitter"
    assert ambry["date_last_evaluated"] == "2022-05-23"
    assert ambry["allele_origin"] == ["germline"]
    assert ambry["mapped_conditions"][0]["name"] == "Cardiovascular phenotype"
    assert ambry["submitted_hgvs"] == ["NM_004333.4:c.1799T>A"]
    somatic = [e.data for e in scvs.values() if e.data["classification_type"] == "somatic_clinical_impact"]
    assert {s["clinical_impact_assertion_type"] for s in somatic} >= {"diagnostic"}
    assert all("clinical_impact_clinical_significance" in s for s in somatic)
    onco = [e.data for e in scvs.values() if e.data["classification_type"] == "oncogenicity"]
    assert {o["classification"] for o in onco} == {"Oncogenic"}


async def test_counts_split_by_aggregate_contribution_without_resolution() -> None:
    record = parse_variation_archive(braf_archive(), None)[0]
    counts = record.data["submitted_classification_counts"]
    assert counts["germline"]["contributing_to_aggregate"] == {
        "Likely pathogenic": 1, "Pathogenic": 1, "Uncertain significance": 1}
    assert "not_contributing" in counts["germline"]
    assert "contributing_to_aggregate" in counts["oncogenicity"]
    assert record.transformations[0].operation == "count_submitted_classifications"
    assert any("does not reconcile" in lim for lim in record.limitations)


SYNTHETIC = """<?xml version="1.0"?><ClinVarResult-Set>
<VariationArchive VariationID="1" VariationName="synthetic" VariationType="single nucleotide variant"
  Accession="VCV000000001" Version="2" RecordType="classified" DateLastUpdated="2026-01-01">
 <RecordStatus>current</RecordStatus>
 <ClassifiedRecord>
  <SimpleAllele AlleleID="9" VariationID="1"><CanonicalSPDI>NC_000001.11:99:A:G</CanonicalSPDI></SimpleAllele>
  <Classifications>
   <SomaticClinicalImpact DateLastEvaluated="2025-01-01" NumberOfSubmitters="2">
    <ReviewStatus>criteria provided, conflicting classifications</ReviewStatus>
    <Description ClinicalImpactAssertionType="therapeutic" ClinicalImpactClinicalSignificance="sensitivity/response">Tier I - Strong</Description>
    <Description ClinicalImpactAssertionType="therapeutic" ClinicalImpactClinicalSignificance="resistance">Tier II - Potential</Description>
   </SomaticClinicalImpact>
   <OncogenicityClassification><ReviewStatus>criteria provided, single submitter</ReviewStatus><Description>Likely oncogenic</Description></OncogenicityClassification>
  </Classifications>
  <ClinicalAssertionList>
   <ClinicalAssertion ID="11" ContributesToAggregateClassification="true">
    <ClinVarAccession Accession="SCV000000011" Version="1" SubmitterName="Lab A" OrgID="1"/>
    <RecordStatus>current</RecordStatus>
    <Classification DateLastEvaluated="2025-01-01"><ReviewStatus>criteria provided, single submitter</ReviewStatus>
     <SomaticClinicalImpact ClinicalImpactAssertionType="therapeutic" ClinicalImpactClinicalSignificance="sensitivity/response" DrugForTherapeuticAssertion="DrugX">Tier I - Strong</SomaticClinicalImpact></Classification>
    <ObservedInList><ObservedIn><Sample><Origin>somatic</Origin></Sample></ObservedIn></ObservedInList>
   </ClinicalAssertion>
   <ClinicalAssertion ID="12" ContributesToAggregateClassification="true">
    <ClinVarAccession Accession="SCV000000012" Version="3" SubmitterName="Lab B" OrgID="2"/>
    <RecordStatus>current</RecordStatus>
    <Classification><ReviewStatus>criteria provided, single submitter</ReviewStatus>
     <SomaticClinicalImpact ClinicalImpactAssertionType="therapeutic" ClinicalImpactClinicalSignificance="resistance" DrugForTherapeuticAssertion="DrugX">Tier II - Potential</SomaticClinicalImpact></Classification>
   </ClinicalAssertion>
   <ClinicalAssertion ID="13" ContributesToAggregateClassification="true">
    <ClinVarAccession Accession="SCV000000013" Version="1" SubmitterName="Lab C" OrgID="3"/>
    <RecordStatus>current</RecordStatus>
    <Classification><ReviewStatus>criteria provided, single submitter</ReviewStatus>
     <OncogenicityClassification>Likely oncogenic</OncogenicityClassification></Classification>
   </ClinicalAssertion>
  </ClinicalAssertionList>
 </ClassifiedRecord>
</VariationArchive>
<VariationArchive VariationID="2" VariationName="haplotype member" Accession="VCV000000002" Version="1" RecordType="included">
 <IncludedRecord><SimpleAllele AlleleID="10" VariationID="2"/></IncludedRecord>
</VariationArchive>
</ClinVarResult-Set>"""


async def test_somatic_conflict_and_oncogenicity_are_preserved_per_submitter() -> None:
    root = ET.fromstring(SYNTHETIC)
    evidence = parse_variation_archive(root.findall("VariationArchive")[0], None)
    agg = evidence[0].data["aggregate_classifications"]
    somatic = agg["somatic_clinical_impact"]
    assert somatic["conflict_reported_by_clinvar"] is True
    assert [d["clinical_impact_clinical_significance"] for d in somatic["description"]] == ["sensitivity/response", "resistance"]
    assert "germline" not in agg
    assert agg["oncogenicity"]["description"] == "Likely oncogenic"
    by_scv = {e.source_record_id: e.data for e in evidence[1:]}
    assert by_scv["SCV000000011"]["drug_for_therapeutic_assertion"] == "DrugX"
    assert by_scv["SCV000000012"]["clinical_impact_clinical_significance"] == "resistance"
    assert by_scv["SCV000000013"]["classification_type"] == "oncogenicity"
    assert evidence[0].data["submitted_classification_counts"]["somatic_clinical_impact"]["contributing_to_aggregate"] == {
        "Tier I - Strong | therapeutic | sensitivity/response": 1,
        "Tier II - Potential | therapeutic | resistance": 1,
    }


async def test_included_record_has_no_classification() -> None:
    root = ET.fromstring(SYNTHETIC)
    evidence = parse_variation_archive(root.findall("VariationArchive")[1], None)
    assert evidence[0].data["included_record_only"] is True
    assert "aggregate_classifications" not in evidence[0].data
    assert any("IncludedRecord" in lim for lim in evidence[0].limitations)


def clinvar_routes(router: Router) -> None:
    router.json("GET", EUTILS + r"/esearch\.fcgi", load_json("clinvar_esearch_braf_grch38.json"))
    router.json("GET", EUTILS + r"/esummary\.fcgi", load_json("clinvar_esummary_braf.json"))
    router.json("GET", EUTILS + r"/einfo\.fcgi", load_json("clinvar_einfo.json"))
    router.add("GET", EUTILS + r"/efetch\.fcgi", httpx.Response(200, content=load_bytes("clinvar_vcv_13961_trimmed.xml")))


async def test_grch38_allele_is_matched_by_canonical_spdi_not_position(router: Router, make_service) -> None:
    clinvar_routes(router)
    service = make_service()
    match = await service.clinvar.match_variant(braf_variant())
    # 12 records share the position; only VCV 13961 is A>T. 40389 is the A>G allele of the same rsID.
    assert match.matched_ids == ["13961"]
    assert "40389" in {r["variation_id"] for r in match.other_records}
    search = router.called(r"esearch")[0]
    assert search.url.params["term"] == "7[chr] AND 140753335:140753337[CHRPOS]"
    evidence = await service.clinvar.evidence_for_ids(match.matched_ids)
    assert evidence[0].source_release == "ClinVar Entrez Build260924-0125.1 (lastupdate 2026/09/24 04:57)"
    fetch = router.called(r"efetch")[0]
    assert fetch.url.params["rettype"] == "vcv" and fetch.url.params["is_variationid"] == "true"
    assert "api_key" not in fetch.url.params


async def test_grch37_allele_is_matched_on_vcf_fields_from_vcv_xml(router: Router, make_service) -> None:
    summary = load_json("clinvar_esummary_braf.json")
    router.json("GET", EUTILS + r"/esearch\.fcgi", {"esearchresult": {"count": "2", "idlist": ["13961", "40389"]}})
    router.json("GET", EUTILS + r"/esummary\.fcgi", {"result": {"uids": ["13961", "40389"],
                                                                 "13961": summary["result"]["13961"],
                                                                 "40389": summary["result"]["40389"]}})
    router.add("GET", EUTILS + r"/efetch\.fcgi", httpx.Response(200, content=load_bytes("clinvar_vcv_13961_trimmed.xml")))
    match = await make_service().clinvar.match_variant(braf_variant("GRCh37"))
    assert match.matched_ids == ["13961"]
    assert [t.operation for t in match.transformations] == ["clinvar_position_search", "clinvar_vcf_match"]


async def test_empty_efetch_is_not_found(router: Router, make_service) -> None:
    router.add("GET", EUTILS + r"/efetch\.fcgi",
               httpx.Response(200, content=b'<?xml version="1.0" ?><ClinVarResult-Set><set/></ClinVarResult-Set>'))
    with pytest.raises(SourceFailure) as exc:
        await make_service().clinvar.fetch_vcv(["999999999"])
    assert exc.value.error.kind == "not_found"


async def test_full_real_vcv_maps_every_scv_with_types_separated() -> None:
    """Actual VCV000013961.143 (BRAF V600E, EFetch rettype=vcv, retrieved 2026-09-24, 483,549 bytes)."""
    import gzip

    raw = gzip.decompress(load_bytes("clinvar_vcv_13961.143_full.xml.gz"))
    archive = ET.fromstring(raw).find("VariationArchive")
    assert len(archive.findall("ClassifiedRecord/ClinicalAssertionList/ClinicalAssertion")) == 45
    evidence = parse_variation_archive(archive, None)
    record, scvs = evidence[0], evidence[1:]
    assert record.source_record_version == "143"
    assert len(scvs) == 45 and not record.truncation
    types = [e.data["classification_type"] for e in scvs]
    assert (types.count("germline"), types.count("somatic_clinical_impact"), types.count("oncogenicity")) == (22, 21, 2)
    assert len({e.source_record_id for e in scvs}) == 45
    counts = record.data["submitted_classification_counts"]["germline"]["contributing_to_aggregate"]
    # Matches ClinVar's own explanation "Pathogenic (4); Likely pathogenic (1); Uncertain significance (1)".
    assert counts == {"Pathogenic": 4, "Likely pathogenic": 1, "Uncertain significance": 1}
