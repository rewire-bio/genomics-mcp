"""Open Targets Platform GraphQL adapter: target-disease associations."""

from __future__ import annotations

from typing import Any

from ..http import SourceHttp, failure
from ..models import Evidence, Truncation
from ..sources import SOURCES

API = "https://api.platform.opentargets.org/api/v4/graphql"
INFO = SOURCES["open_targets"]

ASSOCIATIONS_QUERY = """
query TargetAssociations($ensemblId: String!, $size: Int!) {
  meta { apiVersion { x y z } dataVersion { year month iteration } }
  target(ensemblId: $ensemblId) {
    id approvedSymbol approvedName biotype
    associatedDiseases(page: { index: 0, size: $size }) {
      count
      rows { score datatypeScores { id score } disease { id name } }
    }
  }
}
"""


def _version(meta: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(meta, dict):
        return None, None
    api = meta.get("apiVersion") or {}
    data = meta.get("dataVersion") or {}
    api_v = ".".join(str(api[k]) for k in ("x", "y", "z") if api.get(k) is not None) or None
    parts = [str(data[k]) for k in ("year", "month") if data.get(k) is not None]
    if data.get("iteration") is not None:
        parts.append(str(data["iteration"]))
    return api_v, (".".join(parts) or None)


class OpenTargetsClient:
    def __init__(self, http: SourceHttp):
        self.http = http

    async def associations(
        self, ensembl_gene_id: str, *, size: int = 25, deadline: float | None = None
    ) -> Evidence:
        response = await self.http.request(
            "POST",
            API,
            operation="target_associations",
            json_body={
                "query": ASSOCIATIONS_QUERY,
                "variables": {"ensemblId": ensembl_gene_id, "size": size},
            },
            headers={"Content-Type": "application/json"},
            deadline=deadline,
            accept_status=(200, 400),
        )
        body = self.http.decode_json(response, "target_associations")
        if not isinstance(body, dict):
            raise failure(
                "open_targets",
                "target_associations",
                "invalid_response",
                "GraphQL response is not an object",
            )
        if body.get("errors") and not body.get("data"):
            msg = "; ".join(
                str(e.get("message"))[:200] for e in body["errors"] if isinstance(e, dict)
            )
            raise failure("open_targets", "target_associations", "upstream", msg or "GraphQL error")
        data = body.get("data") or {}
        target = data.get("target")
        if target is None:
            raise failure(
                "open_targets",
                "target_associations",
                "not_found",
                f"no Open Targets target {ensembl_gene_id}",
            )
        api_v, data_v = _version(data.get("meta"))
        assoc = target.get("associatedDiseases") or {}
        rows = []
        for r in assoc.get("rows") or []:
            disease = r.get("disease") or {}
            rows.append(
                {
                    "disease_id": disease.get("id"),
                    "disease_name": disease.get("name"),
                    "association_score": r.get("score"),
                    "datatype_scores": {
                        d.get("id"): d.get("score") for d in r.get("datatypeScores") or []
                    },
                }
            )
        count = assoc.get("count")
        truncation = []
        if isinstance(count, int) and count > len(rows):
            truncation.append(
                Truncation(
                    field="data.associations",
                    returned=len(rows),
                    available=count,
                    reason="first page in Open Targets' default (score) order",
                )
            )
        return Evidence(
            source="open_targets",
            evidence_type="target_disease_association",
            source_record_id=target.get("id"),
            source_url=f"https://platform.opentargets.org/target/{target.get('id')}/associations",
            source_release=f"Open Targets Platform data {data_v}"
            + (f" (API {api_v})" if api_v else "")
            if data_v
            else (f"Open Targets API {api_v}" if api_v else None),
            terms_url=INFO.terms_url,
            data={
                "target": {
                    "id": target.get("id"),
                    "symbol": target.get("approvedSymbol"),
                    "name": target.get("approvedName"),
                    "biotype": target.get("biotype"),
                },
                "association_count": count,
                "associations": rows,
            },
            limitations=[
                "Association scores are Open Targets' own aggregate research scores, not clinical assertions or probabilities.",
            ],
            truncation=truncation,
        )
