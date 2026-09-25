"""UniProt REST adapter: entry identity, review status, function and features."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from ..http import SourceHttp, failure
from ..models import Evidence, IdentifierCandidate, Transformation, Truncation, compact_text
from ..sources import SOURCES
from .base import require_dict

BASE = "https://rest.uniprot.org/uniprotkb"
INFO = SOURCES["uniprot"]
ACCESSION_RE = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-(\d+))?$"
)
DEFAULT_FEATURE_TYPES = (
    "Chain",
    "Signal",
    "Transit peptide",
    "Propeptide",
    "Domain",
    "Region",
    "Motif",
    "Repeat",
    "Zinc finger",
    "DNA binding",
    "Coiled coil",
    "Compositional bias",
    "Active site",
    "Binding site",
    "Site",
    "Transmembrane",
    "Topological domain",
    "Intramembrane",
    "Modified residue",
    "Glycosylation",
    "Disulfide bond",
    "Cross-link",
    "Lipidation",
)
MAX_FEATURES = 150
SEARCH_FIELDS = "accession,reviewed,id,protein_name,gene_primary,organism_id,length"


@dataclass
class ProteinSearch:
    candidates: list[IdentifierCandidate]
    reviewed: list[str]
    total: int | None
    release: str | None
    notes: list[str] = field(default_factory=list)


def _release(response: httpx.Response) -> str | None:
    rel = response.headers.get("x-uniprot-release")
    date = response.headers.get("x-uniprot-release-date")
    if rel:
        return f"UniProt {rel}" + (f" ({date})" if date else "")
    return None


def _value(obj: Any) -> str | None:
    if isinstance(obj, dict):
        v = obj.get("value")
        return v if isinstance(v, str) else None
    return None


def _position(loc: dict[str, Any], key: str) -> int | None:
    v = (loc.get(key) or {}).get("value")
    return v if isinstance(v, int) else None


class UniprotClient:
    def __init__(self, http: SourceHttp):
        self.http = http

    async def entry(
        self, accession: str, *, deadline: float | None = None, _hops: int = 0
    ) -> tuple[dict[str, Any], str | None, list[Transformation]]:
        m = ACCESSION_RE.match(accession.strip().upper())
        if not m:
            raise failure(
                "uniprot", "entry", "invalid_input", f"{accession!r} is not a UniProtKB accession"
            )
        base_acc = m.group(1)
        transformations: list[Transformation] = []
        if m.group(2):
            transformations.append(
                Transformation(
                    operation="isoform_to_entry",
                    source="uniprot",
                    detail=f"isoform {accession} reported through its entry {base_acc}; isoform-specific sequence not selected",
                )
            )
        response = await self.http.request(
            "GET",
            f"{BASE}/{quote(base_acc)}.json",
            operation="entry",
            deadline=deadline,
            accept_status=(200, 303),
        )
        body = require_dict(self.http, self.http.decode_json(response, "entry"), "entry")
        release = _release(response)
        if body.get("entryType") == "Inactive":
            reason = body.get("inactiveReason") or {}
            targets = reason.get("mergeDemergeTo") or []
            rtype = reason.get("inactiveReasonType")
            if rtype == "MERGED" and len(targets) == 1 and _hops == 0:
                entry, release2, steps = await self.entry(targets[0], deadline=deadline, _hops=1)
                return (
                    entry,
                    release2,
                    [
                        *transformations,
                        Transformation(
                            operation="accession_merged",
                            source="uniprot",
                            detail=f"{base_acc} is inactive (merged) in UniProtKB; reporting {targets[0]}",
                            before={"accession": base_acc},
                            after={"accession": targets[0]},
                        ),
                        *steps,
                    ],
                )
            raise failure(
                "uniprot",
                "entry",
                "not_found",
                f"{base_acc} is inactive ({rtype}); successor accession(s): {targets or 'none'}",
            )
        returned = body.get("primaryAccession")
        if returned and returned != base_acc:
            # Happens when the injected client follows UniProt's redirect for a merged accession.
            transformations.append(
                Transformation(
                    operation="accession_redirected",
                    source="uniprot",
                    detail=f"UniProt answered {base_acc} with entry {returned}",
                    before={"accession": base_acc},
                    after={"accession": returned},
                )
            )
        return body, release, transformations

    def entry_evidence(
        self,
        entry: dict[str, Any],
        release: str | None,
        transformations: list[Transformation],
        *,
        include_all_features: bool = False,
    ) -> Evidence:
        accession = entry.get("primaryAccession")
        reviewed = entry.get("entryType", "").startswith("UniProtKB reviewed")
        desc = entry.get("proteinDescription") or {}
        rec = _value((desc.get("recommendedName") or {}).get("fullName"))
        submitted = [_value((n or {}).get("fullName")) for n in desc.get("submissionNames") or []]
        alt = [_value((n or {}).get("fullName")) for n in desc.get("alternativeNames") or []]
        genes = []
        for g in entry.get("genes") or []:
            genes.append(
                {
                    "name": _value(g.get("geneName")),
                    "synonyms": [_value(s) for s in g.get("synonyms") or [] if _value(s)],
                }
            )
        function_texts: list[str] = []
        locations: list[str] = []
        disease: list[dict[str, Any]] = []
        truncation: list[Truncation] = []
        for c in entry.get("comments") or []:
            ctype = c.get("commentType")
            if ctype == "FUNCTION":
                for t in c.get("texts") or []:
                    text, cut = compact_text(_value(t), 1500)
                    if text:
                        function_texts.append(text)
                    if cut:
                        truncation.append(
                            Truncation(
                                field="data.function", returned=1500, reason="text shortened"
                            )
                        )
            elif ctype == "SUBCELLULAR LOCATION":
                for loc in c.get("subcellularLocations") or []:
                    v = _value(loc.get("location"))
                    if v and v not in locations:
                        locations.append(v)
            elif ctype == "DISEASE" and isinstance(c.get("disease"), dict):
                d = c["disease"]
                disease.append(
                    {
                        k: v
                        for k, v in {
                            "id": d.get("diseaseId"),
                            "acronym": d.get("acronym"),
                            "xref": f"{(d.get('diseaseCrossReference') or {}).get('database')}:{(d.get('diseaseCrossReference') or {}).get('id')}"
                            if d.get("diseaseCrossReference")
                            else None,
                        }.items()
                        if v
                    }
                )
        features_all = [f for f in entry.get("features") or [] if isinstance(f, dict)]
        counts = Counter(f.get("type") for f in features_all)
        selected = (
            features_all
            if include_all_features
            else [f for f in features_all if f.get("type") in DEFAULT_FEATURE_TYPES]
        )
        features = []
        for f in selected[:MAX_FEATURES]:
            loc = f.get("location") or {}
            start, end = _position(loc, "start"), _position(loc, "end")
            item = {
                "type": f.get("type"),
                "description": f.get("description") or None,
                "start": start - 1 if start is not None else None,
                "end": end,
                "feature_id": f.get("featureId"),
            }
            if f.get("alternativeSequence"):
                a = f["alternativeSequence"]
                item["change"] = (
                    f"{a.get('originalSequence')}>{'/'.join(a.get('alternativeSequences') or [])}"
                )
            features.append({k: v for k, v in item.items() if v is not None})
        if len(selected) > MAX_FEATURES:
            truncation.append(
                Truncation(
                    field="data.features",
                    returned=MAX_FEATURES,
                    available=len(selected),
                    reason="compact response limit",
                )
            )
        xrefs: dict[str, list[Any]] = {}
        for x in entry.get("uniProtKBCrossReferences") or []:
            db = x.get("database")
            if db in ("Ensembl", "RefSeq", "MANE-Select", "HGNC", "MIM", "GeneID", "CCDS"):
                props = {p.get("key"): p.get("value") for p in x.get("properties") or []}
                row = {
                    "id": x.get("id"),
                    **{
                        k: v
                        for k, v in props.items()
                        if k
                        in (
                            "ProteinId",
                            "GeneId",
                            "NucleotideSequenceId",
                            "RefSeqNucleotideId",
                            "RefSeqProteinId",
                        )
                    },
                }
                xrefs.setdefault(db, []).append(row if len(row) > 1 else x.get("id"))
        pdb_count = sum(
            1 for x in entry.get("uniProtKBCrossReferences") or [] if x.get("database") == "PDB"
        )
        seq = entry.get("sequence") or {}
        audit = entry.get("entryAudit") or {}
        data = {
            "accession": accession,
            "secondary_accessions": entry.get("secondaryAccessions"),
            "uniprotkb_id": entry.get("uniProtkbId"),
            "entry_type": entry.get("entryType"),
            "reviewed": reviewed,
            "protein_existence": entry.get("proteinExistence"),
            "annotation_score": entry.get("annotationScore"),
            "recommended_name": rec,
            "submitted_names": [s for s in submitted if s] or None,
            "alternative_names": [a for a in alt if a] or None,
            "genes": genes,
            "organism": {
                "taxon_id": (entry.get("organism") or {}).get("taxonId"),
                "name": (entry.get("organism") or {}).get("scientificName"),
            },
            "sequence": {
                "length": seq.get("length"),
                "mass_da": seq.get("molWeight"),
                "crc64": seq.get("crc64"),
                "md5": seq.get("md5"),
                "version": audit.get("sequenceVersion"),
            },
            "function": function_texts,
            "subcellular_locations": locations,
            "diseases": disease,
            "feature_counts": dict(counts),
            "features": features,
            "cross_references": xrefs,
            "pdb_structure_count": pdb_count,
        }
        limitations = []
        if not reviewed:
            limitations.append("Unreviewed (TrEMBL) entry: annotations are largely automatic.")
        if not include_all_features:
            limitations.append(
                "Natural variant, mutagenesis, conflict and secondary-structure features are counted but not listed."
            )
        limitations.append(
            "Feature coordinates are 0-based half-open on the canonical UniProt sequence."
        )
        return Evidence(
            source="uniprot",
            evidence_type="protein_entry",
            source_record_id=accession,
            source_record_version=str(audit.get("entryVersion"))
            if audit.get("entryVersion")
            else None,
            source_url=f"https://www.uniprot.org/uniprotkb/{accession}/entry",
            source_release=release,
            source_updated_at=audit.get("lastAnnotationUpdateDate"),
            terms_url=INFO.terms_url,
            data={k: v for k, v in data.items() if v not in (None, [], {})},
            transformations=[
                *transformations,
                Transformation(
                    operation="uniprot_to_zero_based",
                    source="local",
                    detail="UniProt 1-based feature start written as 0-based half-open start",
                ),
            ],
            limitations=limitations,
            truncation=truncation,
        )

    async def search(
        self, query: str, *, size: int = 25, deadline: float | None = None
    ) -> ProteinSearch:
        response = await self.http.request(
            "GET",
            f"{BASE}/search",
            operation="search",
            params={"query": query, "fields": SEARCH_FIELDS, "format": "json", "size": str(size)},
            deadline=deadline,
        )
        body = require_dict(self.http, self.http.decode_json(response, "search"), "search")
        results = [r for r in body.get("results") or [] if isinstance(r, dict)]
        total_header = response.headers.get("x-total-results")
        candidates, reviewed = [], []
        for r in results:
            acc = r.get("primaryAccession", "")
            is_reviewed = str(r.get("entryType", "")).startswith("UniProtKB reviewed")
            if is_reviewed:
                reviewed.append(acc)
            desc = r.get("proteinDescription") or {}
            name = _value((desc.get("recommendedName") or {}).get("fullName")) or next(
                (_value(n.get("fullName")) for n in desc.get("submissionNames") or []), None
            )
            candidates.append(
                IdentifierCandidate(
                    entity_type="protein",
                    identifier=acc,
                    label=name,
                    source="uniprot",
                    match_type="reviewed" if is_reviewed else "unreviewed",
                    notes=[str(r.get("uniProtkbId"))] if r.get("uniProtkbId") else [],
                )
            )
        total = int(total_header) if total_header and total_header.isdigit() else None
        notes = []
        if total is not None and total > len(results):
            notes.append(f"UniProt reported {total} matches; {len(results)} returned.")
        return ProteinSearch(
            candidates=candidates,
            reviewed=reviewed,
            total=total,
            release=_release(response),
            notes=notes,
        )
