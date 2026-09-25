"""HGNC REST adapter: approved symbols, aliases, previous symbols and cross-references."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

from ..http import SourceHttp, failure
from ..models import Evidence, IdentifierCandidate, SourceError, Transformation
from ..sources import SOURCES
from .base import TtlValue, optional, require_dict

BASE = "https://rest.genenames.org"
INFO = SOURCES["hgnc"]

HGNC_ID_RE = re.compile(r"^HGNC:(\d+)$", re.IGNORECASE)
ENSG_RE = re.compile(r"^(ENSG\d{11})(?:\.(\d+))?$", re.IGNORECASE)
UNIPROT_RE = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-\d+)?$"
)

GeneIdType = Literal["symbol", "hgnc_id", "ensembl_gene_id", "entrez_id", "uniprot_ids"]

_FIELDS = (
    "hgnc_id",
    "symbol",
    "name",
    "status",
    "locus_group",
    "locus_type",
    "location",
    "alias_symbol",
    "prev_symbol",
    "alias_name",
    "prev_name",
    "ensembl_gene_id",
    "entrez_id",
    "uniprot_ids",
    "refseq_accession",
    "mane_select",
    "omim_id",
    "ccds_id",
    "date_modified",
    "date_symbol_changed",
    "date_approved_reserved",
)


@dataclass
class GeneResolution:
    status: Literal["resolved", "ambiguous", "not_found"]
    query: str
    id_type: GeneIdType
    record: dict[str, Any] | None = None
    match_type: str | None = None
    candidates: list[IdentifierCandidate] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    transformations: list[Transformation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[SourceError] = field(default_factory=list)


def detect_gene_id_type(query: str) -> tuple[GeneIdType, str]:
    value = query.strip()
    if m := HGNC_ID_RE.match(value):
        return "hgnc_id", f"HGNC:{m.group(1)}"
    if m := ENSG_RE.match(value):
        return "ensembl_gene_id", m.group(1).upper()
    if UNIPROT_RE.match(value) and not value.isalpha():
        return "uniprot_ids", value.split("-")[0]
    return "symbol", value


class HgncClient:
    def __init__(self, http: SourceHttp):
        self.http = http
        self._info = TtlValue()

    async def info(self, deadline: float | None = None) -> dict[str, Any]:
        async def load() -> dict[str, Any]:
            data = await self.http.get_json(
                f"{BASE}/info",
                operation="info",
                headers={"Accept": "application/json"},
                deadline=deadline,
            )
            return require_dict(self.http, data, "info")

        return await self._info.get(load)

    async def fetch(
        self, field_name: str, value: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        url = f"{BASE}/fetch/{field_name}/{quote(value, safe=':')}"
        data = await self.http.get_json(
            url,
            operation=f"fetch/{field_name}",
            headers={"Accept": "application/json"},
            deadline=deadline,
        )
        body = require_dict(self.http, data, f"fetch/{field_name}")
        response = body.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("docs"), list):
            raise failure(
                "hgnc", f"fetch/{field_name}", "invalid_response", "missing response.docs"
            )
        return [d for d in response["docs"] if isinstance(d, dict)]

    def evidence(
        self, doc: dict[str, Any], release: str | None, *, match_type: str, query: str
    ) -> Evidence:
        data = {k: doc[k] for k in _FIELDS if k in doc}
        data["match_type"] = match_type
        data["query"] = query
        hgnc_id = doc.get("hgnc_id")
        limitations = []
        if doc.get("status") != "Approved":
            limitations.append(f"HGNC status is {doc.get('status')!r}, not 'Approved'.")
        return Evidence(
            source="hgnc",
            evidence_type="gene_nomenclature",
            source_record_id=hgnc_id,
            source_url=f"https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/{hgnc_id}"
            if hgnc_id
            else None,
            source_release=release,
            source_updated_at=doc.get("date_modified"),
            terms_url=INFO.terms_url,
            data=data,
            limitations=limitations,
        )

    async def _release(self, deadline: float | None) -> tuple[str | None, SourceError | None]:
        info, err = await optional(self.info(deadline))
        if info and info.get("lastModified"):
            return f"HGNC index lastModified {info['lastModified']}", None
        return None, err

    async def resolve(
        self, query: str, *, id_type: GeneIdType | None = None, deadline: float | None = None
    ) -> GeneResolution:
        detected, value = detect_gene_id_type(query)
        kind = id_type or detected
        if kind == "entrez_id":
            value = query.strip()
        result = GeneResolution(status="not_found", query=query, id_type=kind)
        if kind == "ensembl_gene_id" and (m := ENSG_RE.match(query.strip())) and m.group(2):
            result.transformations.append(
                Transformation(
                    operation="drop_version_for_lookup",
                    source="hgnc",
                    detail="HGNC stores unversioned Ensembl gene IDs; the version was dropped for this lookup only",
                    before={"ensembl_gene_id": query.strip()},
                    after={"ensembl_gene_id": value},
                )
            )
        release_task = asyncio.create_task(self._release(deadline))
        try:
            if kind != "symbol":
                docs = await self.fetch(kind, value, deadline)
                release, rel_err = await release_task
                if rel_err:
                    result.errors.append(rel_err)
                return self._finish_direct(result, docs, kind, release)
            approved_r, (alias, alias_err), (prev, prev_err) = await asyncio.gather(
                self.fetch("symbol", value, deadline),
                optional(self.fetch("alias_symbol", value, deadline)),
                optional(self.fetch("prev_symbol", value, deadline)),
                return_exceptions=False,
            )
            approved = approved_r
            release, rel_err = await release_task
        except BaseException:
            release_task.cancel()
            raise
        for err in (alias_err, prev_err, rel_err):
            if err:
                result.errors.append(err)
        return self._finish_symbol(result, value, approved, alias or [], prev or [], release)

    def _finish_direct(
        self, result: GeneResolution, docs: list[dict[str, Any]], kind: str, release: str | None
    ) -> GeneResolution:
        if len(docs) == 1:
            result.status = "resolved"
            result.record = docs[0]
            result.match_type = kind
            result.evidence.append(
                self.evidence(docs[0], release, match_type=kind, query=result.query)
            )
        elif len(docs) > 1:
            result.status = "ambiguous"
            result.candidates = [self._candidate(d, kind) for d in docs]
            result.evidence = [
                self.evidence(d, release, match_type=kind, query=result.query) for d in docs
            ]
            result.warnings.append(
                f"{len(docs)} HGNC records match {kind} {result.query!r}; none was selected"
            )
        return result

    def _finish_symbol(
        self,
        result: GeneResolution,
        value: str,
        approved: list[dict[str, Any]],
        alias: list[dict[str, Any]],
        prev: list[dict[str, Any]],
        release: str | None,
    ) -> GeneResolution:
        approved_ok = [d for d in approved if d.get("status") == "Approved"]
        by_id: dict[str, tuple[dict[str, Any], str]] = {}
        for d in prev:
            by_id.setdefault(d.get("hgnc_id", ""), (d, "previous_symbol"))
        for d in alias:
            by_id.setdefault(d.get("hgnc_id", ""), (d, "alias_symbol"))
        if len(approved_ok) == 1:
            doc = approved_ok[0]
            result.status = "resolved"
            result.record = doc
            result.match_type = "approved_symbol"
            if doc.get("symbol") != value:
                result.transformations.append(
                    Transformation(
                        operation="symbol_case",
                        source="hgnc",
                        detail=f"{value!r} matched approved symbol {doc.get('symbol')!r}",
                    )
                )
            result.evidence.append(
                self.evidence(doc, release, match_type="approved_symbol", query=value)
            )
            others = [(d, m) for hid, (d, m) in by_id.items() if hid != doc.get("hgnc_id")]
            for d, m in others:
                result.candidates.append(self._candidate(d, m))
                result.warnings.append(
                    f"{value!r} is also a {m.replace('_', ' ')} of {d.get('symbol')} ({d.get('hgnc_id')})"
                )
            return result
        pool = list(by_id.values())
        pool += [(d, "non_approved_symbol") for d in approved if d.get("status") != "Approved"]
        if len(pool) == 1 and pool[0][1] != "non_approved_symbol":
            doc, match = pool[0]
            result.status = "resolved"
            result.record = doc
            result.match_type = match
            result.transformations.append(
                Transformation(
                    operation=f"{match}_to_approved",
                    source="hgnc",
                    detail=f"{value!r} is an HGNC {match.replace('_', ' ')}; approved symbol is {doc.get('symbol')!r}",
                    before={"symbol": value},
                    after={"symbol": doc.get("symbol"), "hgnc_id": doc.get("hgnc_id")},
                )
            )
            result.evidence.append(self.evidence(doc, release, match_type=match, query=value))
            return result
        if pool:
            result.status = "ambiguous"
            result.candidates = [self._candidate(d, m) for d, m in pool]
            result.evidence = [
                self.evidence(d, release, match_type=m, query=value) for d, m in pool
            ]
            result.warnings.append(
                f"{value!r} is not a unique approved HGNC symbol; {len(pool)} candidate records were returned and none was selected"
            )
        return result

    @staticmethod
    def _candidate(doc: dict[str, Any], match: str) -> IdentifierCandidate:
        return IdentifierCandidate(
            entity_type="gene",
            identifier=doc.get("hgnc_id", ""),
            label=doc.get("symbol"),
            source="hgnc",
            match_type=match,
            notes=[f"status: {doc.get('status')}"] if doc.get("status") != "Approved" else [],
        )
