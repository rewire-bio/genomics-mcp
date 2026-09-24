"""NCBI sequence (E-utilities nuccore) and Variation Services adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr

from ..assemblies import refseq_accession, refseq_to_contig
from ..http import SourceHttp, failure
from ..models import Assembly, Transformation
from ..variants import ReferenceWindow
from .base import require_dict

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
VARIATION = "https://api.ncbi.nlm.nih.gov/variation/v0"
TOOL = "rewire-genomics-mcp"


@dataclass(frozen=True)
class NcbiIdentity:
    """Explicitly configured NCBI parameters. Nothing is read from the environment here."""

    api_key: SecretStr | None = None
    email: str | None = None

    def params(self) -> dict[str, str]:
        out = {"tool": TOOL}
        if self.email:
            out["email"] = self.email
        if self.api_key is not None:
            out["api_key"] = self.api_key.get_secret_value()
        return out


def parse_fasta(text: str) -> tuple[str, str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith(">"):
        raise ValueError("not FASTA")
    return lines[0][1:], "".join(lines[1:]).upper()


class NcbiSequenceClient:
    def __init__(self, http: SourceHttp, identity: NcbiIdentity | None = None):
        self.http = http
        self.identity = identity or NcbiIdentity()

    async def sequence(
        self, assembly: Assembly, contig: str, start: int, end: int, *, deadline: float | None = None
    ) -> ReferenceWindow:
        accession = refseq_accession(assembly, contig)
        if accession is None:
            raise failure("ncbi_nuccore", "efetch/fasta", "unsupported", f"no RefSeq accession for {assembly} {contig}")
        if start < 0 or end <= start:
            raise failure("ncbi_nuccore", "efetch/fasta", "invalid_input", "empty or negative sequence interval")
        params = {
            "db": "nuccore",
            "id": accession,
            "rettype": "fasta",
            "retmode": "text",
            "seq_start": str(start + 1),
            "seq_stop": str(end),
            **self.identity.params(),
        }
        response = await self.http.request(
            "GET", f"{EUTILS}/efetch.fcgi", operation="efetch/fasta", params=params, deadline=deadline
        )
        try:
            header, seq = parse_fasta(response.text)
        except ValueError:
            raise failure("ncbi_nuccore", "efetch/fasta", "invalid_response", "response was not FASTA") from None
        if not header.startswith(accession):
            raise failure("ncbi_nuccore", "efetch/fasta", "invalid_response", f"FASTA header is not {accession}")
        if not seq:
            raise failure("ncbi_nuccore", "efetch/fasta", "not_found", "empty sequence returned")
        return ReferenceWindow(
            assembly=assembly,
            contig=contig,
            start=start,
            sequence=seq,
            source="ncbi_nuccore",
            at_contig_start=start == 0,
            at_contig_end=start + len(seq) < end,
        )


@dataclass
class SpdiPlacement:
    assembly: Assembly
    contig: str
    accession: str
    position: int
    deleted: str
    inserted: str
    hgvs: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def spdi(self) -> str:
        return f"{self.accession}:{self.position}:{self.deleted}:{self.inserted}"


@dataclass
class RefSnpResult:
    rsid: str
    placements: list[SpdiPlacement]
    transformations: list[Transformation]
    notes: list[str]
    last_update_build: str | None
    last_update_date: str | None


class NcbiVariationClient:
    def __init__(self, http: SourceHttp):
        self.http = http

    async def refsnp(self, rsid: str, assembly: Assembly, *, deadline: float | None = None, _hops: int = 0) -> RefSnpResult:
        number = rsid.lower().removeprefix("rs")
        data = await self.http.get_json(f"{VARIATION}/refsnp/{quote(number)}", operation="refsnp", deadline=deadline)
        body = require_dict(self.http, data, "refsnp")
        transformations: list[Transformation] = []
        if "merged_snapshot_data" in body and "primary_snapshot_data" not in body:
            merged = (body.get("merged_snapshot_data") or {}).get("merged_into") or []
            if len(merged) == 1 and _hops == 0:
                follow = await self.refsnp(f"rs{merged[0]}", assembly, deadline=deadline, _hops=1)
                follow.transformations.insert(
                    0,
                    Transformation(
                        operation="rsid_merged",
                        source="ncbi_variation",
                        detail=f"rs{number} was merged into rs{merged[0]} by dbSNP",
                        before={"rsid": f"rs{number}"},
                        after={"rsid": f"rs{merged[0]}"},
                    ),
                )
                return follow
            raise failure("ncbi_variation", "refsnp", "not_found", f"rs{number} is merged into {merged}; not followed")
        snapshot = body.get("primary_snapshot_data")
        if not isinstance(snapshot, dict):
            state = next((k for k in body if k.endswith("_snapshot_data")), "no snapshot")
            raise failure("ncbi_variation", "refsnp", "not_found", f"rs{number} has no current placement ({state})")
        placements: list[SpdiPlacement] = []
        for placement in snapshot.get("placements_with_allele") or []:
            seq_id = placement.get("seq_id", "")
            hits = [(a, c) for a, c in refseq_to_contig(seq_id) if a == assembly]
            if not hits:
                continue
            _, contig = hits[0]
            for allele in placement.get("alleles") or []:
                spdi = (allele.get("allele") or {}).get("spdi") or {}
                deleted = spdi.get("deleted_sequence", "")
                inserted = spdi.get("inserted_sequence", "")
                if deleted == inserted:
                    continue  # the reference allele
                placements.append(
                    SpdiPlacement(
                        assembly=assembly,
                        contig=contig,
                        accession=seq_id,
                        position=int(spdi.get("position")),
                        deleted=deleted,
                        inserted=inserted,
                        hgvs=allele.get("hgvs"),
                    )
                )
        return RefSnpResult(
            rsid=f"rs{body.get('refsnp_id', number)}",
            placements=placements,
            transformations=transformations,
            notes=[f"dbSNP variant_type: {snapshot.get('variant_type')}"] if snapshot.get("variant_type") else [],
            last_update_build=str(body["last_update_build_id"]) if body.get("last_update_build_id") else None,
            last_update_date=body.get("last_update_date"),
        )

    async def hgvs_to_genomic(self, hgvs: str, assembly: Assembly, *, deadline: float | None = None) -> tuple[list[SpdiPlacement], list[str]]:
        """Map a versioned RefSeq HGVS (g./c./n.) to SPDI placements on ``assembly`` chromosomes."""
        response = await self.http.request(
            "GET",
            f"{VARIATION}/hgvs/{quote(hgvs, safe='')}/contextuals",
            operation="hgvs/contextuals",
            deadline=deadline,
            accept_status=(200,),
        )
        body = require_dict(self.http, self.http.decode_json(response, "hgvs/contextuals"), "hgvs/contextuals")
        payload = body.get("data") or {}
        notes = [f"NCBI input_hgvs_validity: {payload['input_hgvs_validity']}"] if payload.get("input_hgvs_validity") else []
        for warning in payload.get("warnings") or []:
            if isinstance(warning, dict) and warning.get("message"):
                notes.append(f"NCBI warning: {' '.join(str(warning['message']).split())[:300]}")
        spdis = [s for s in payload.get("spdis") or [] if isinstance(s, dict)]
        genomic: list[dict[str, Any]] = []
        for s in spdis:
            if refseq_to_contig(s.get("seq_id", "")):
                genomic.append(s)
            else:
                equivalent = await self.http.get_json(
                    f"{VARIATION}/spdi/{quote(self._spdi(s), safe='')}/all_equivalent_contextual",
                    operation="spdi/all_equivalent_contextual",
                    deadline=deadline,
                )
                eq_body = require_dict(self.http, equivalent, "spdi/all_equivalent_contextual")
                genomic.extend(
                    e for e in (eq_body.get("data") or {}).get("spdis") or []
                    if isinstance(e, dict) and refseq_to_contig(e.get("seq_id", ""))
                )
        placements = []
        for s in genomic:
            hits = [(a, c) for a, c in refseq_to_contig(s["seq_id"]) if a == assembly]
            if hits:
                placements.append(
                    SpdiPlacement(
                        assembly=assembly,
                        contig=hits[0][1],
                        accession=s["seq_id"],
                        position=int(s["position"]),
                        deleted=s.get("deleted_sequence", ""),
                        inserted=s.get("inserted_sequence", ""),
                    )
                )
        return placements, notes

    @staticmethod
    def _spdi(s: dict[str, Any]) -> str:
        return f"{s['seq_id']}:{s['position']}:{s.get('deleted_sequence', '')}:{s.get('inserted_sequence', '')}"
