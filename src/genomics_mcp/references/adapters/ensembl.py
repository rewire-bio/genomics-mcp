"""Ensembl REST adapter: versioned genes/transcripts, sequence, VEP and variant_recoder."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..http import SourceHttp, failure
from ..models import Assembly, CanonicalVariant, Evidence, Transformation, Truncation
from ..sources import SOURCES
from ..variants import ReferenceWindow
from .base import TtlValue, optional, require_dict, require_list

SERVERS: dict[str, str] = {
    "GRCh38": "https://rest.ensembl.org",
    "GRCh37": "https://grch37.rest.ensembl.org",
}
INFO = SOURCES["ensembl"]
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
ENSEMBL_ID_RE = re.compile(r"^(ENS[GTPE]\d{11})(?:\.(\d+))?$", re.IGNORECASE)
MAX_TRANSCRIPTS = 60
MAX_CONSEQUENCES = 40
MAX_COLOCATED = 20


def split_version(identifier: str) -> tuple[str, int | None]:
    m = ENSEMBL_ID_RE.match(identifier.strip())
    if not m:
        return identifier.strip(), None
    return m.group(1).upper(), int(m.group(2)) if m.group(2) else None


def _half_open(record: dict[str, Any]) -> dict[str, Any]:
    """Ensembl 1-based inclusive start/end -> 0-based half-open."""
    out: dict[str, Any] = {}
    if isinstance(record.get("start"), int) and isinstance(record.get("end"), int):
        out = {"start": record["start"] - 1, "end": record["end"]}
    return out


class EnsemblClient:
    def __init__(self, http: SourceHttp):
        self.http = http
        self._release = {a: TtlValue() for a in SERVERS}

    def server(self, assembly: Assembly) -> str:
        return SERVERS[assembly]

    async def release(self, assembly: Assembly, deadline: float | None = None) -> str | None:
        async def load() -> str | None:
            data = await self.http.get_json(
                f"{self.server(assembly)}/info/software",
                operation="info/software",
                headers=JSON_HEADERS,
                deadline=deadline,
            )
            body = require_dict(self.http, data, "info/software")
            return f"Ensembl REST release {body['release']}" if body.get("release") else None

        return await self._release[assembly].get(load)

    async def _release_or_none(
        self, assembly: Assembly, deadline: float | None
    ) -> tuple[str | None, list[str]]:
        release, err = await optional(self.release(assembly, deadline))
        return release, ([f"Ensembl release lookup failed: {err.message}"] if err else [])

    # ------------------------------------------------------------------ lookup
    async def lookup_id(
        self,
        identifier: str,
        assembly: Assembly,
        *,
        expand: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        stable_id, _ = split_version(identifier)
        params = {"expand": "1"} if expand else None
        data = await self.http.get_json(
            f"{self.server(assembly)}/lookup/id/{quote(stable_id)}",
            operation="lookup/id",
            params=params,
            headers=JSON_HEADERS,
            deadline=deadline,
        )
        return require_dict(self.http, data, "lookup/id")

    async def lookup_symbol(
        self,
        symbol: str,
        assembly: Assembly,
        *,
        expand: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        params = {"expand": "1"} if expand else None
        data = await self.http.get_json(
            f"{self.server(assembly)}/lookup/symbol/homo_sapiens/{quote(symbol)}",
            operation="lookup/symbol",
            params=params,
            headers=JSON_HEADERS,
            deadline=deadline,
        )
        return require_dict(self.http, data, "lookup/symbol")

    async def entity_evidence(
        self,
        record: dict[str, Any],
        assembly: Assembly,
        *,
        requested_id: str | None = None,
        deadline: float | None = None,
    ) -> Evidence:
        release, notes = await self._release_or_none(assembly, deadline)
        return self.entity_to_evidence(
            record, assembly, release, requested_id=requested_id, notes=notes
        )

    def entity_to_evidence(
        self,
        record: dict[str, Any],
        assembly: Assembly,
        release: str | None,
        *,
        requested_id: str | None = None,
        notes: list[str] | None = None,
    ) -> Evidence:
        object_type = record.get("object_type")
        stable = record.get("id")
        version = record.get("version")
        versioned = f"{stable}.{version}" if stable and version is not None else stable
        limitations = list(notes or [])
        transformations = (
            [
                Transformation(
                    operation="ensembl_to_zero_based",
                    source="ensembl",
                    detail="Ensembl 1-based inclusive start/end reported as 0-based half-open",
                )
            ]
            if "start" in record
            else []
        )
        truncation: list[Truncation] = []
        returned_assembly = record.get("assembly_name")
        if returned_assembly and returned_assembly != assembly:
            limitations.append(
                f"Ensembl returned assembly {returned_assembly}, requested {assembly}."
            )
        if requested_id:
            _, requested_version = split_version(requested_id)
            if (
                requested_version is not None
                and version is not None
                and requested_version != version
            ):
                limitations.append(
                    f"Requested version {requested_id} differs from the current Ensembl version {versioned}; "
                    "the record describes the current version, not the requested one."
                )
        data: dict[str, Any] = {
            "object_type": object_type,
            "id": versioned,
            "display_name": record.get("display_name"),
            "biotype": record.get("biotype"),
            "assembly": returned_assembly,
            "contig": record.get("seq_region_name"),
            **_half_open(record),
            "strand": record.get("strand"),
        }
        for key in (
            "description",
            "Parent",
            "is_canonical",
            "canonical_transcript",
            "gencode_primary",
            "length",
            "source",
            "logic_name",
        ):
            if key in record:
                data[key] = record[key]
        if object_type == "Transcript" and isinstance(record.get("Translation"), dict):
            data["translation"] = self._translation(record["Translation"])
        if object_type == "Transcript" and isinstance(record.get("Exon"), list):
            data["exon_count"] = len(record["Exon"])
        if isinstance(record.get("Transcript"), list):
            transcripts = [self._transcript(t) for t in record["Transcript"] if isinstance(t, dict)]
            transcripts.sort(key=lambda t: (not t.get("is_canonical"), t.get("id") or ""))
            data["transcript_count"] = len(transcripts)
            if len(transcripts) > MAX_TRANSCRIPTS:
                truncation.append(
                    Truncation(
                        field="data.transcripts",
                        returned=MAX_TRANSCRIPTS,
                        available=len(transcripts),
                        reason="compact response limit; canonical transcript listed first",
                    )
                )
            data["transcripts"] = transcripts[:MAX_TRANSCRIPTS]
        data = {k: v for k, v in data.items() if v is not None}
        return Evidence(
            source="ensembl",
            evidence_type=f"ensembl_{(object_type or 'record').lower()}",
            source_record_id=stable,
            source_record_version=str(version) if version is not None else None,
            source_url=f"{self.server(assembly)}/lookup/id/{stable}",
            source_release=release,
            terms_url=INFO.terms_url,
            data=data,
            transformations=transformations,
            limitations=limitations,
            truncation=truncation,
        )

    @staticmethod
    def _translation(t: dict[str, Any]) -> dict[str, Any]:
        tid = t.get("id")
        version = t.get("version")
        return {
            "id": f"{tid}.{version}" if tid and version is not None else tid,
            "length": t.get("length"),
            **_half_open(t),
        }

    def _transcript(self, t: dict[str, Any]) -> dict[str, Any]:
        tid = t.get("id")
        version = t.get("version")
        out: dict[str, Any] = {
            "id": f"{tid}.{version}" if tid and version is not None else tid,
            "display_name": t.get("display_name"),
            "biotype": t.get("biotype"),
            "is_canonical": bool(t.get("is_canonical")),
            **_half_open(t),
        }
        if "gencode_primary" in t:
            out["gencode_primary"] = bool(t["gencode_primary"])
        if isinstance(t.get("Translation"), dict):
            out["translation"] = self._translation(t["Translation"])
        return {k: v for k, v in out.items() if v is not None}

    # ---------------------------------------------------------------- sequence
    async def sequence(
        self,
        assembly: Assembly,
        contig: str,
        start: int,
        end: int,
        *,
        deadline: float | None = None,
    ) -> ReferenceWindow:
        if start < 0 or end <= start:
            raise failure(
                "ensembl", "sequence/region", "invalid_input", "empty or negative sequence interval"
            )
        region = f"{contig}:{start + 1}..{end}:1"
        data = await self.http.get_json(
            f"{self.server(assembly)}/sequence/region/human/{region}",
            operation="sequence/region",
            headers=JSON_HEADERS,
            deadline=deadline,
        )
        body = require_dict(self.http, data, "sequence/region")
        seq = body.get("seq")
        seq_id = body.get("id", "")
        if not isinstance(seq, str):
            raise failure("ensembl", "sequence/region", "invalid_response", "missing seq")
        parts = str(seq_id).split(":")
        if len(parts) >= 2 and parts[1] and parts[1] != assembly:
            raise failure(
                "ensembl",
                "sequence/region",
                "invalid_response",
                f"sequence reported for {parts[1]}, requested {assembly}",
            )
        returned_end = start + len(seq)
        return ReferenceWindow(
            assembly=assembly,
            contig=contig,
            start=start,
            sequence=seq.upper(),
            source="ensembl",
            at_contig_start=start == 0,
            at_contig_end=returned_end < end,
        )

    # --------------------------------------------------------------------- VEP
    @staticmethod
    def vep_region(variant: CanonicalVariant) -> tuple[str, str]:
        if not variant.ref:
            return f"{variant.contig}:{variant.start + 1}-{variant.start}:1", variant.alt
        allele = variant.alt or "-"
        return f"{variant.contig}:{variant.start + 1}-{variant.end}:1", allele

    async def vep(self, variant: CanonicalVariant, *, deadline: float | None = None) -> Evidence:
        region, allele = self.vep_region(variant)
        params = {
            "canonical": "1",
            "mane": "1",
            "hgvs": "1",
            "protein": "1",
            "uniprot": "1",
            "variant_class": "1",
        }
        data = await self.http.get_json(
            f"{self.server(variant.assembly)}/vep/human/region/{region}/{allele}",
            operation="vep/region",
            params=params,
            headers=JSON_HEADERS,
            deadline=deadline,
        )
        items = require_list(self.http, data, "vep/region")
        if len(items) != 1 or not isinstance(items[0], dict):
            raise failure(
                "ensembl",
                "vep/region",
                "invalid_response",
                f"expected one VEP result, got {len(items)}",
            )
        release, notes = await self._release_or_none(variant.assembly, deadline)
        return self.vep_to_evidence(items[0], variant, region, allele, release, notes)

    def vep_to_evidence(
        self,
        item: dict[str, Any],
        variant: CanonicalVariant,
        region: str,
        allele: str,
        release: str | None,
        notes: list[str] | None = None,
    ) -> Evidence:
        limitations = list(notes or [])
        limitations.append(
            "Consequences are for Ensembl/GENCODE transcripts only; RefSeq transcript consequences were not requested."
        )
        limitations.append(
            "SIFT/PolyPhen values are Ensembl-reported predictions, not observations."
        )
        if item.get("assembly_name") and item["assembly_name"] != variant.assembly:
            raise failure(
                "ensembl",
                "vep/region",
                "invalid_response",
                f"VEP answered for {item['assembly_name']}, requested {variant.assembly}",
            )
        consequences = [
            self._consequence(tc)
            for tc in item.get("transcript_consequences") or []
            if isinstance(tc, dict)
        ]
        consequences.sort(
            key=lambda c: (
                not c.get("mane_select"),
                not c.get("canonical"),
                c.get("transcript_id") or "",
            )
        )
        truncation = []
        if len(consequences) > MAX_CONSEQUENCES:
            truncation.append(
                Truncation(
                    field="data.transcript_consequences",
                    returned=MAX_CONSEQUENCES,
                    available=len(consequences),
                    reason="compact response limit; MANE/canonical transcripts listed first",
                )
            )
        colocated = [
            {k: c.get(k) for k in ("id", "allele_string", "start", "end") if c.get(k) is not None}
            for c in item.get("colocated_variants") or []
            if isinstance(c, dict)
        ]
        if len(colocated) > MAX_COLOCATED:
            truncation.append(
                Truncation(
                    field="data.colocated_variants",
                    returned=MAX_COLOCATED,
                    available=len(colocated),
                    reason="compact response limit",
                )
            )
        data = {
            "input": item.get("input"),
            "assembly": item.get("assembly_name"),
            "most_severe_consequence": item.get("most_severe_consequence"),
            "variant_class": item.get("variant_class"),
            "transcript_consequences": consequences[:MAX_CONSEQUENCES],
            "intergenic": bool(item.get("intergenic_consequences")) and not consequences,
            "colocated_variants": colocated[:MAX_COLOCATED],
        }
        return Evidence(
            source="ensembl",
            evidence_type="variant_consequence",
            source_record_id=f"{region}/{allele}",
            source_url=f"{self.server(variant.assembly)}/vep/human/region/{region}/{allele}",
            source_release=release,
            terms_url=INFO.terms_url,
            data={k: v for k, v in data.items() if v not in (None, [], "")},
            transformations=[
                Transformation(
                    operation="to_ensembl_region",
                    source="local",
                    detail="0-based half-open allele written as Ensembl 1-based region notation",
                    after={"region": region, "allele": allele},
                )
            ],
            limitations=limitations,
            truncation=truncation,
        )

    @staticmethod
    def _consequence(tc: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "transcript_id",
            "gene_id",
            "gene_symbol",
            "hgnc_id",
            "biotype",
            "consequence_terms",
            "impact",
            "hgvsc",
            "hgvsp",
            "mane_select",
            "mane_plus_clinical",
            "strand",
            "protein_start",
            "protein_end",
            "amino_acids",
            "codons",
            "cds_start",
            "cds_end",
            "swissprot",
            "sift_prediction",
            "sift_score",
            "polyphen_prediction",
            "polyphen_score",
            "flags",
        )
        out = {k: tc[k] for k in keys if k in tc and tc[k] not in (None, "", [])}
        if "canonical" in tc:
            out["canonical"] = bool(tc["canonical"])
        hgvsc = tc.get("hgvsc")
        if isinstance(hgvsc, str) and ":" in hgvsc:
            out["transcript_version"] = hgvsc.split(":", 1)[0]
        return out

    # ---------------------------------------------------------- variant_recoder
    async def variant_recoder(
        self, text: str, assembly: Assembly, *, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        data = await self.http.get_json(
            f"{self.server(assembly)}/variant_recoder/human/{quote(text, safe='')}",
            operation="variant_recoder",
            params={"vcf_string": "1"},
            headers=JSON_HEADERS,
            deadline=deadline,
        )
        items = require_list(self.http, data, "variant_recoder")
        return [i for i in items if isinstance(i, dict)]
