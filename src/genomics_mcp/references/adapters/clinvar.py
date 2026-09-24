"""ClinVar adapter using NCBI E-utilities.

Allele matching uses ESearch (position) and ESummary (canonical SPDI / location).
Assertions come from EFetch ``rettype=vcv`` XML: each SCV keeps its submitter,
review status, condition and classification type. Germline classification,
somatic clinical impact and oncogenicity are kept separate. ClinVar's own
aggregate review status and explanation are reported; no local reconciliation
or consensus is made.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..http import SourceHttp, failure
from ..models import CanonicalVariant, Evidence, Transformation, Truncation, compact_text
from ..sources import SOURCES
from .base import TtlValue, optional, require_dict
from .ncbi import EUTILS, NcbiIdentity

INFO = SOURCES["clinvar"]
MAX_ASSERTIONS = 60
MAX_CONDITIONS = 25
MAX_CITATIONS = 15
MAX_XML_CANDIDATES = 10
MAX_XML_BYTES = 25 * 1024 * 1024
_CLINVAR_TYPES: dict[str, set[str]] = {
    "SNV": {"single nucleotide variant"},
    "deletion": {"deletion"},
    "insertion": {"insertion", "duplication"},
    "MNV": {"indel"},
    "delins": {"indel"},
}
CLASSIFICATION_TYPES = {
    "GermlineClassification": "germline",
    "SomaticClinicalImpact": "somatic_clinical_impact",
    "OncogenicityClassification": "oncogenicity",
}


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    value = " ".join(el.text.split())
    return value or None


def _citations(el: ET.Element) -> list[str]:
    out: list[str] = []
    for c in el.findall("Citation"):
        for cid in c.findall("ID"):
            if cid.text:
                label = f"{cid.get('Source')}:{cid.text.strip()}"
                if label not in out:
                    out.append(label)
        url = _text(c.find("URL"))
        if url and url not in out:
            out.append(url)
    return out


def _trait_names(trait_set: ET.Element) -> list[dict[str, Any]]:
    traits = []
    for trait in trait_set.findall("Trait"):
        preferred = None
        for name in trait.findall("Name/ElementValue"):
            if name.get("Type") == "Preferred":
                preferred = _text(name)
                break
        xrefs = []
        for x in trait.findall("XRef"):
            if x.get("DB") in ("MedGen", "MONDO", "OMIM", "Orphanet", "HP", "MeSH") and x.get("ID"):
                xrefs.append(
                    f"{x.get('DB')}:{x.get('ID')}"
                    if not x.get("ID", "").startswith(x.get("DB", "") + ":")
                    else x.get("ID")
                )
        traits.append(
            {
                k: v
                for k, v in {
                    "name": preferred,
                    "type": trait.get("Type"),
                    "xrefs": xrefs[:6],
                }.items()
                if v
            }
        )
    return traits


@dataclass
class ClinvarMatch:
    matched_ids: list[str]
    other_records: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    transformations: list[Transformation] = field(default_factory=list)


class ClinvarClient:
    def __init__(self, http: SourceHttp, identity: NcbiIdentity | None = None):
        self.http = http
        self.identity = identity or NcbiIdentity()
        self._einfo = TtlValue()

    def _params(self, **extra: str) -> dict[str, str]:
        return {**extra, **self.identity.params()}

    async def release(self, deadline: float | None = None) -> str | None:
        async def load() -> str | None:
            data = await self.http.get_json(
                f"{EUTILS}/einfo.fcgi",
                operation="einfo",
                params=self._params(db="clinvar", retmode="json"),
                deadline=deadline,
            )
            try:
                info = data["einforesult"]["dbinfo"][0]
            except (KeyError, IndexError, TypeError):
                raise failure(
                    "clinvar", "einfo", "invalid_response", "missing einforesult.dbinfo"
                ) from None
            build, last = info.get("dbbuild"), info.get("lastupdate")
            return f"ClinVar Entrez {build} (lastupdate {last})" if build else None

        return await self._einfo.get(load)

    # --------------------------------------------------------------- matching
    async def esearch(
        self, term: str, *, retmax: int = 50, deadline: float | None = None
    ) -> tuple[list[str], int]:
        data = await self.http.get_json(
            f"{EUTILS}/esearch.fcgi",
            operation="esearch",
            params=self._params(db="clinvar", term=term, retmode="json", retmax=str(retmax)),
            deadline=deadline,
        )
        body = require_dict(self.http, data, "esearch").get("esearchresult")
        if not isinstance(body, dict):
            raise failure("clinvar", "esearch", "invalid_response", "missing esearchresult")
        if body.get("ERROR"):
            raise failure("clinvar", "esearch", "upstream", f"ESearch error: {body['ERROR']}")
        ids = [str(i) for i in body.get("idlist") or []]
        return ids, int(body.get("count") or 0)

    async def esummary(
        self, ids: list[str], *, deadline: float | None = None
    ) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        data = await self.http.get_json(
            f"{EUTILS}/esummary.fcgi",
            operation="esummary",
            params=self._params(db="clinvar", id=",".join(ids), retmode="json"),
            deadline=deadline,
        )
        result = require_dict(self.http, data, "esummary").get("result")
        if not isinstance(result, dict):
            raise failure("clinvar", "esummary", "invalid_response", "missing result")
        return {
            uid: result[uid] for uid in result.get("uids", []) if isinstance(result.get(uid), dict)
        }

    def search_term(self, variant: CanonicalVariant) -> str:
        field_name = "CHRPOS" if variant.assembly == "GRCh38" else "CHRPOS37"
        lo = max(1, variant.start)  # one base before the 1-based first changed position
        hi = variant.end + 1
        if variant.ncbi_canonical_spdi:
            _, pos, deleted, _ = variant.ncbi_canonical_spdi.rsplit(":", 3)
            hi = max(hi, int(pos) + len(deleted) + 1)
        return f"{variant.contig}[chr] AND {lo}:{hi}[{field_name}]"

    async def match_variant(
        self, variant: CanonicalVariant, *, deadline: float | None = None
    ) -> ClinvarMatch:
        term = self.search_term(variant)
        ids, count = await self.esearch(term, deadline=deadline)
        match = ClinvarMatch(matched_ids=[])
        match.transformations.append(
            Transformation(
                operation="clinvar_position_search",
                source="clinvar",
                detail=f"ESearch term {term!r} returned {count} record(s)",
            )
        )
        if count > len(ids):
            match.notes.append(
                f"Only the first {len(ids)} of {count} ClinVar records at this position were examined."
            )
        summaries = await self.esummary(ids, deadline=deadline)
        target_spdi = variant.ncbi_canonical_spdi if variant.assembly == "GRCh38" else None
        need_xml: list[tuple[tuple[int, int, int], str]] = []
        for uid in ids:
            doc = summaries.get(uid)
            if doc is None:
                continue
            sets = doc.get("variation_set") or []
            spdis = [v.get("canonical_spdi") for v in sets if isinstance(v, dict)]
            if len(sets) == 1 and target_spdi and spdis[0] == target_spdi:
                match.matched_ids.append(uid)
            elif (
                len(sets) == 1
                and not target_spdi
                and (rank := self._location_rank(sets[0], doc.get("obj_type"), variant)) is not None
            ):
                need_xml.append((rank, uid))
            else:
                match.other_records.append(
                    {
                        "variation_id": uid,
                        "accession": doc.get("accession_version"),
                        "title": doc.get("title"),
                        "canonical_spdi": [s for s in spdis if s],
                    }
                )
        if need_xml:
            if variant.vcf is None:
                match.notes.append(
                    "Allele matching needs a VCF representation; ClinVar records were not matched."
                )
                match.other_records.extend({"variation_id": uid} for _, uid in need_xml)
            else:
                need_xml.sort()
                examined = [uid for _, uid in need_xml[:MAX_XML_CANDIDATES]]
                if len(need_xml) > len(examined):
                    match.notes.append(
                        f"Only {len(examined)} of {len(need_xml)} candidate records were compared by allele."
                    )
                roots = await self.fetch_vcv(examined, deadline=deadline)
                for archive in roots:
                    uid = archive.get("VariationID", "")
                    if self._vcf_matches(archive, variant):
                        match.matched_ids.append(uid)
                    else:
                        match.other_records.append(
                            {
                                "variation_id": uid,
                                "accession": f"{archive.get('Accession')}.{archive.get('Version')}",
                                "title": archive.get("VariationName"),
                            }
                        )
                match.transformations.append(
                    Transformation(
                        operation="clinvar_vcf_match",
                        source="clinvar",
                        detail=f"records compared on {variant.assembly} positionVCF/referenceAlleleVCF/alternateAlleleVCF",
                    )
                )
        elif target_spdi:
            match.transformations.append(
                Transformation(
                    operation="clinvar_spdi_match",
                    source="clinvar",
                    detail=f"records matched on ClinVar canonical SPDI {target_spdi}",
                )
            )
        match.other_records = match.other_records[:20]
        return match

    @staticmethod
    def _location_rank(
        variation: dict[str, Any], obj_type: Any, variant: CanonicalVariant
    ) -> tuple[int, int, int] | None:
        """ESummary pre-filter on the requested assembly's 1-based start/stop.

        Returns None for incompatible records, otherwise a sort key so the most
        similar records are compared first (type, span length, distance).
        """
        type_ok = str(obj_type or "").lower() in _CLINVAR_TYPES.get(variant.variant_class, set())
        for loc in variation.get("variation_loc") or []:
            if loc.get("assembly_name") != variant.assembly:
                continue
            try:
                start, stop = int(loc.get("start")), int(loc.get("stop"))
            except (TypeError, ValueError):
                return (1, 0, 0)  # no usable location: let the XML comparison decide
            if variant.variant_class == "SNV":
                return (0, 0, 0) if start == stop == variant.start + 1 else None
            if start > variant.end + 1 or stop < variant.start:
                return None
            span = stop - start + 1
            return (
                0 if type_ok else 1,
                abs(span - max(1, len(variant.ref))),
                abs(start - (variant.start + 1)),
            )
        return None

    @staticmethod
    def _vcf_matches(archive: ET.Element, variant: CanonicalVariant) -> bool:
        assert variant.vcf is not None
        for loc in archive.iter("SequenceLocation"):
            if loc.get("Assembly") != variant.assembly or loc.get("positionVCF") is None:
                continue
            if (
                loc.get("Chr") == variant.contig
                and loc.get("positionVCF") == str(variant.vcf.pos)
                and (loc.get("referenceAlleleVCF") or "").upper() == variant.vcf.ref
                and (loc.get("alternateAlleleVCF") or "").upper() == variant.vcf.alt
            ):
                return True
        return False

    # ------------------------------------------------------------------ fetch
    async def fetch_vcv(
        self, variation_ids: list[str], *, deadline: float | None = None
    ) -> list[ET.Element]:
        response = await self.http.request(
            "GET",
            f"{EUTILS}/efetch.fcgi",
            operation="efetch/vcv",
            params=self._params(
                db="clinvar", rettype="vcv", is_variationid="true", id=",".join(variation_ids)
            ),
            deadline=deadline,
            max_bytes=MAX_XML_BYTES,
        )
        try:
            # NCBI HTTPS only, body size-bounded while streaming; ElementTree does not resolve
            # external entities and bundled expat (>= 2.4.1) limits entity amplification.
            root = ET.fromstring(response.content)  # noqa: S314
        except ET.ParseError:
            raise failure(
                "clinvar", "efetch/vcv", "invalid_response", "VCV response is not valid XML"
            ) from None
        archives = root.findall("VariationArchive")
        if not archives:
            raise failure(
                "clinvar",
                "efetch/vcv",
                "not_found",
                f"no VCV record for variation ID(s) {','.join(variation_ids)}",
            )
        return archives

    async def evidence_for_ids(
        self, variation_ids: list[str], *, deadline: float | None = None
    ) -> list[Evidence]:
        archives = await self.fetch_vcv(variation_ids, deadline=deadline)
        release, err = await optional(self.release(deadline))
        out: list[Evidence] = []
        for archive in archives:
            out.extend(parse_variation_archive(archive, release))
        if err:
            for ev in out[:1]:
                ev.limitations.append(f"ClinVar release lookup failed: {err.message}")
        return out


# ----------------------------------------------------------------------- parse


def parse_variation_archive(archive: ET.Element, release: str | None) -> list[Evidence]:
    variation_id = archive.get("VariationID")
    accession = archive.get("Accession")
    version = archive.get("Version")
    url = f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{variation_id}/"
    record = archive.find("ClassifiedRecord")
    included = archive.find("IncludedRecord")
    body = record if record is not None else included
    limitations = [
        "ClinVar classifications are submitter assertions; this service does not reconcile them into a verdict.",
    ]
    if body is None:
        raise failure(
            "clinvar",
            "efetch/vcv",
            "invalid_response",
            f"VCV {accession} has no ClassifiedRecord/IncludedRecord",
        )
    allele = body.find("SimpleAllele")
    locations = []
    if allele is not None:
        for loc in allele.findall("Location/SequenceLocation"):
            locations.append(
                {
                    k: loc.get(a)
                    for k, a in (
                        ("assembly", "Assembly"),
                        ("accession", "Accession"),
                        ("chr", "Chr"),
                        ("position_vcf", "positionVCF"),
                        ("ref_vcf", "referenceAlleleVCF"),
                        ("alt_vcf", "alternateAlleleVCF"),
                        ("start_1based", "start"),
                        ("stop_1based", "stop"),
                    )
                    if loc.get(a) is not None
                }
            )
    genes = []
    if allele is not None:
        for g in allele.findall("GeneList/Gene"):
            genes.append(
                {
                    k: g.get(a)
                    for k, a in (
                        ("symbol", "Symbol"),
                        ("gene_id", "GeneID"),
                        ("hgnc_id", "HGNC_ID"),
                        ("relationship", "RelationshipType"),
                    )
                    if g.get(a)
                }
            )
    xrefs = []
    if allele is not None:
        for x in allele.findall("XRefList/XRef"):
            db, xid = x.get("DB"), x.get("ID")
            if db == "dbSNP" and xid:
                xrefs.append(f"rs{xid}")
            elif db in ("ClinGen", "OMIM", "UniProtKB") and xid:
                xrefs.append(f"{db}:{xid}")
    aggregate: dict[str, Any] = {}
    classifications = body.find("Classifications")
    if classifications is not None:
        for child in classifications:
            ctype = CLASSIFICATION_TYPES.get(child.tag)
            if ctype is None:
                continue
            aggregate[ctype] = _aggregate(child)
    rcvs = []
    for rcv in body.findall("RCVList/RCVAccession"):
        conds = [
            {"name": _text(c), "xref": f"{c.get('DB')}:{c.get('ID')}" if c.get("DB") else None}
            for c in rcv.findall("ClassifiedConditionList/ClassifiedCondition")
        ]
        per_type = {}
        for child in (
            rcv.find("RCVClassifications") if rcv.find("RCVClassifications") is not None else []
        ):
            ctype = CLASSIFICATION_TYPES.get(child.tag)
            if ctype:
                desc = child.find("Description")
                per_type[ctype] = {
                    "review_status": _text(child.find("ReviewStatus")),
                    "description": _text(desc),
                    **(
                        {"clinical_impact_assertion_type": desc.get("ClinicalImpactAssertionType")}
                        if desc is not None and desc.get("ClinicalImpactAssertionType")
                        else {}
                    ),
                    **(
                        {
                            "clinical_impact_clinical_significance": desc.get(
                                "ClinicalImpactClinicalSignificance"
                            )
                        }
                        if desc is not None and desc.get("ClinicalImpactClinicalSignificance")
                        else {}
                    ),
                }
        rcvs.append(
            {
                "accession": f"{rcv.get('Accession')}.{rcv.get('Version')}",
                "conditions": [{k: v for k, v in c.items() if v} for c in conds],
                "classifications": per_type,
            }
        )
    trait_map: dict[str, list[dict[str, str]]] = {}
    for tm in body.findall("TraitMappingList/TraitMapping"):
        medgen = tm.find("MedGen")
        if medgen is not None:
            trait_map.setdefault(tm.get("ClinicalAssertionID", ""), []).append(
                {
                    "medgen": medgen.get("CUI", ""),
                    "name": medgen.get("Name", ""),
                    "mapped_from": f"{tm.get('MappingRef')}:{tm.get('MappingValue')}",
                }
            )
    assertions_all = [
        _assertion(ca, trait_map) for ca in body.findall("ClinicalAssertionList/ClinicalAssertion")
    ]
    tallies: dict[str, dict[str, Counter[str]]] = {}
    for a in assertions_all:
        if a.get("record_status") not in (None, "current"):
            continue
        key = a.get("classification") or "not provided"
        if a.get("classification_type") == "somatic_clinical_impact":
            key = " | ".join(
                x
                for x in (
                    a.get("classification"),
                    a.get("clinical_impact_assertion_type"),
                    a.get("clinical_impact_clinical_significance"),
                )
                if x
            )
        bucket = (
            "contributing_to_aggregate" if a.get("contributes_to_aggregate") else "not_contributing"
        )
        per_type = tallies.setdefault(a.get("classification_type") or "unspecified", {})
        per_type.setdefault(bucket, Counter())[key] += 1
    deleted_scvs = len(body.findall("DeletedSCVList/SCV"))
    evidence_record = Evidence(
        source="clinvar",
        evidence_type="clinical_variant_record",
        source_record_id=accession,
        source_record_version=version,
        source_url=url,
        source_release=release,
        source_updated_at=archive.get("DateLastUpdated"),
        terms_url=INFO.terms_url,
        data={
            "variation_id": variation_id,
            "vcv": f"{accession}.{version}",
            "name": archive.get("VariationName"),
            "variation_type": archive.get("VariationType"),
            "record_type": archive.get("RecordType"),
            "record_status": _text(archive.find("RecordStatus")),
            "number_of_submissions": archive.get("NumberOfSubmissions"),
            "number_of_submitters": archive.get("NumberOfSubmitters"),
            "date_created": archive.get("DateCreated"),
            "most_recent_submission": archive.get("MostRecentSubmission"),
            "canonical_spdi": _text(allele.find("CanonicalSPDI")) if allele is not None else None,
            "locations": locations,
            "genes": genes,
            "xrefs": xrefs,
            "protein_changes": [t for t in (_text(p) for p in allele.findall("ProteinChange"))]
            if allele is not None
            else [],
            "aggregate_classifications": aggregate,
            "rcv_records": rcvs[:40],
            "submitted_classification_counts": {
                t: {b: dict(c) for b, c in buckets.items()} for t, buckets in tallies.items()
            },
            "deleted_scv_count": deleted_scvs,
            "included_record_only": record is None,
        },
        transformations=[
            Transformation(
                operation="count_submitted_classifications",
                source="local",
                detail="submitted_classification_counts counts current SCVs per classification type and value, "
                "split by ClinVar's ContributesToAggregateClassification flag; descriptive only, no conflict resolution",
            )
        ],
        limitations=limitations
        + (
            ["IncludedRecord: this variant has no direct ClinVar classification of its own."]
            if record is None
            else []
        ),
        truncation=[
            Truncation(
                field="data.rcv_records",
                returned=40,
                available=len(rcvs),
                reason="compact response limit",
            )
        ]
        if len(rcvs) > 40
        else [],
    )
    evidence_record.data = {
        k: v for k, v in evidence_record.data.items() if v not in (None, [], {})
    }
    out = [evidence_record]
    ordered = sorted(
        assertions_all, key=lambda a: (a.get("classification_type") or "", a.get("scv") or "")
    )
    for a in ordered[:MAX_ASSERTIONS]:
        cut = a.pop("_comment_truncated", False)
        out.append(
            Evidence(
                source="clinvar",
                evidence_type="clinical_assertion",
                source_record_id=a.get("scv_accession"),
                source_record_version=a.get("scv_version"),
                source_url=f"https://www.ncbi.nlm.nih.gov/clinvar/?term={a.get('scv_accession')}",
                source_release=release,
                source_updated_at=a.get("date_updated"),
                terms_url=INFO.terms_url,
                data={k: v for k, v in a.items() if v not in (None, [], {})}
                | {"vcv": f"{accession}.{version}"},
                truncation=[
                    Truncation(field="data.comment", returned=600, reason="comment shortened")
                ]
                if cut
                else [],
            )
        )
    if len(ordered) > MAX_ASSERTIONS:
        evidence_record.truncation.append(
            Truncation(
                field="clinical_assertion evidence",
                returned=MAX_ASSERTIONS,
                available=len(ordered),
                reason="compact response limit; counts above include all current SCVs",
            )
        )
    return out


def _aggregate(el: ET.Element) -> dict[str, Any]:
    conditions = []
    cond_list = el.find("ConditionList")
    if cond_list is not None:
        for ts in cond_list.findall("TraitSet"):
            for t in _trait_names(ts):
                t["contributes_to_aggregate"] = (
                    ts.get("ContributesToAggregateClassification") == "true"
                )
                conditions.append(t)
    descriptions = []
    for d in el.findall("Description"):
        entry = {"value": _text(d)}
        for attr, key in (
            ("ClinicalImpactAssertionType", "clinical_impact_assertion_type"),
            ("ClinicalImpactClinicalSignificance", "clinical_impact_clinical_significance"),
        ):
            if d.get(attr):
                entry[key] = d.get(attr)
        descriptions.append(entry)
    explanation = _text(el.find("Explanation"))
    review = _text(el.find("ReviewStatus"))
    out = {
        "review_status": review,
        "description": descriptions[0]["value"]
        if len(descriptions) == 1 and len(descriptions[0]) == 1
        else descriptions,
        "explanation": explanation,
        "conflict_reported_by_clinvar": bool(review and "conflicting" in review.lower())
        or bool(
            descriptions
            and any("conflicting" in (d.get("value") or "").lower() for d in descriptions)
        ),
        "date_last_evaluated": el.get("DateLastEvaluated"),
        "number_of_submissions": el.get("NumberOfSubmissions"),
        "number_of_submitters": el.get("NumberOfSubmitters"),
        "conditions": conditions[:MAX_CONDITIONS],
    }
    if len(conditions) > MAX_CONDITIONS:
        out["conditions_truncated_from"] = len(conditions)
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _assertion(ca: ET.Element, trait_map: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    acc = ca.find("ClinVarAccession")
    cls = ca.find("Classification")
    out: dict[str, Any] = {
        "scv": f"{acc.get('Accession')}.{acc.get('Version')}" if acc is not None else None,
        "scv_accession": acc.get("Accession") if acc is not None else None,
        "scv_version": acc.get("Version") if acc is not None else None,
        "submitter": acc.get("SubmitterName") if acc is not None else None,
        "submitter_org_id": acc.get("OrgID") if acc is not None else None,
        "organization_category": acc.get("OrganizationCategory") if acc is not None else None,
        "date_updated": acc.get("DateUpdated") if acc is not None else None,
        "submission_date": ca.get("SubmissionDate"),
        "record_status": _text(ca.find("RecordStatus")),
        "contributes_to_aggregate": ca.get("ContributesToAggregateClassification") == "true",
        "assertion": _text(ca.find("Assertion")),
    }
    if cls is not None:
        out["review_status"] = _text(cls.find("ReviewStatus"))
        out["date_last_evaluated"] = cls.get("DateLastEvaluated")
        for tag, ctype in CLASSIFICATION_TYPES.items():
            node = cls.find(tag)
            if node is not None:
                out["classification_type"] = ctype
                out["classification"] = _text(node)
                if tag == "SomaticClinicalImpact":
                    out["clinical_impact_assertion_type"] = node.get("ClinicalImpactAssertionType")
                    out["clinical_impact_clinical_significance"] = node.get(
                        "ClinicalImpactClinicalSignificance"
                    )
                    out["drug_for_therapeutic_assertion"] = node.get("DrugForTherapeuticAssertion")
                break
        comments = [_text(c) for c in cls.findall("Comment")]
        comment = " ".join(c for c in comments if c) or None
        out["comment"], out["_comment_truncated"] = compact_text(comment)
        cites = _citations(cls)
        out["citations"] = cites[:MAX_CITATIONS]
    methods = [
        _text(a) for a in ca.findall("AttributeSet/Attribute") if a.get("Type") == "AssertionMethod"
    ]
    out["assertion_method"] = [m for m in methods if m]
    origins, affected, method_types = [], [], []
    for obs in ca.findall("ObservedInList/ObservedIn"):
        for value, bucket in (
            (_text(obs.find("Sample/Origin")), origins),
            (_text(obs.find("Sample/AffectedStatus")), affected),
            (_text(obs.find("Method/MethodType")), method_types),
        ):
            if value and value not in bucket:
                bucket.append(value)
    out["allele_origin"] = origins
    out["affected_status"] = affected
    out["method_type"] = method_types
    submitted_conditions = []
    for ts in ca.findall("TraitSet"):
        submitted_conditions.extend(_trait_names(ts))
    out["submitted_conditions"] = submitted_conditions[:10]
    out["mapped_conditions"] = trait_map.get(ca.get("ID", ""), [])[:10]
    hgvs = [
        _text(a)
        for a in ca.findall("SimpleAllele/AttributeSet/Attribute")
        if a.get("Type") == "HGVS"
    ]
    out["submitted_hgvs"] = [h for h in hgvs if h][:3]
    return out
