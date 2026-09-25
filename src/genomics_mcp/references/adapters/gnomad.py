"""gnomAD GraphQL adapter: allele counts with denominators, and gene constraint."""

from __future__ import annotations

from typing import Any, Literal

from ..http import SourceHttp, failure
from ..models import Assembly, CanonicalVariant, Evidence, Transformation
from ..sources import SOURCES

API = "https://gnomad.broadinstitute.org/api"
INFO = SOURCES["gnomad"]

GnomadDataset = Literal["gnomad_r4", "gnomad_r3", "gnomad_r2_1"]
DATASET_ASSEMBLY: dict[str, Assembly] = {
    "gnomad_r4": "GRCh38",
    "gnomad_r3": "GRCh38",
    "gnomad_r2_1": "GRCh37",
}
DEFAULT_DATASET: dict[str, GnomadDataset] = {"GRCh38": "gnomad_r4", "GRCh37": "gnomad_r2_1"}

VARIANT_QUERY = """
query GnomadVariant($variantId: String!, $dataset: DatasetId!) {
  variant(variantId: $variantId, dataset: $dataset) {
    variant_id reference_genome chrom pos ref alt rsids caid flags
    exome { ac an af homozygote_count hemizygote_count filters
            populations { id ac an homozygote_count hemizygote_count } }
    genome { ac an af homozygote_count hemizygote_count filters
             populations { id ac an homozygote_count hemizygote_count } }
    joint { ac an homozygote_count hemizygote_count filters
            populations { id ac an homozygote_count hemizygote_count } }
  }
}
"""

CONSTRAINT_QUERY = """
query GnomadConstraint($geneId: String!, $referenceGenome: ReferenceGenomeId!) {
  gene(gene_id: $geneId, reference_genome: $referenceGenome) {
    gene_id gene_version symbol canonical_transcript_id
    gnomad_constraint { exp_lof obs_lof oe_lof oe_lof_lower oe_lof_upper pLI lof_z
                        exp_mis obs_mis oe_mis oe_mis_lower oe_mis_upper mis_z
                        exp_syn obs_syn oe_syn syn_z flags }
  }
}
"""


def dataset_for(assembly: Assembly, dataset: str | None) -> GnomadDataset:
    chosen = dataset or DEFAULT_DATASET[assembly]
    if chosen not in DATASET_ASSEMBLY:
        raise failure(
            "gnomad", "variant", "invalid_input", f"unsupported gnomAD dataset {chosen!r}"
        )
    if DATASET_ASSEMBLY[chosen] != assembly:
        raise failure(
            "gnomad",
            "variant",
            "invalid_input",
            f"{chosen} is {DATASET_ASSEMBLY[chosen]}; variant is {assembly}. No liftover is applied.",
        )
    return chosen  # type: ignore[return-value]


def _frequency_block(
    block: dict[str, Any] | None,
    label: str,
    transformations: list[Transformation],
    limitations: list[str],
) -> dict[str, Any] | None:
    if block is None:
        return None
    ac, an = block.get("ac"), block.get("an")
    out: dict[str, Any] = {
        "ac": ac,
        "an": an,
        "af": block.get("af"),
        "homozygote_count": block.get("homozygote_count"),
        "hemizygote_count": block.get("hemizygote_count"),
        "filters": block.get("filters"),
    }
    if out["af"] is None:
        if isinstance(ac, int) and isinstance(an, int) and an > 0:
            out["af_derived"] = ac / an
            transformations.append(
                Transformation(
                    operation="af_derived",
                    detail=f"{label}: AF not reported by gnomAD; af_derived = AC/AN computed locally",
                )
            )
        else:
            limitations.append(
                f"{label}: allele number is missing or zero; no frequency can be computed."
            )
    seen: set[tuple[Any, ...]] = set()
    populations = []
    duplicates = 0
    for p in block.get("populations") or []:
        if not isinstance(p, dict):
            continue
        key = tuple(sorted(p.items()))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        populations.append({k: v for k, v in p.items() if v is not None})
    if duplicates:
        transformations.append(
            Transformation(
                operation="drop_duplicate_population_rows",
                detail=f"{label}: {duplicates} identical population row(s) returned twice were dropped",
            )
        )
    missing_an = [p.get("id") for p in populations if not p.get("an")]
    if missing_an:
        limitations.append(
            f"{label}: populations without allele number: {', '.join(str(x) for x in missing_an)}."
        )
    out["populations"] = populations
    return {k: v for k, v in out.items() if v is not None}


class GnomadClient:
    def __init__(self, http: SourceHttp):
        self.http = http

    async def _graphql(
        self, query: str, variables: dict[str, Any], operation: str, deadline: float | None
    ) -> dict[str, Any]:
        response = await self.http.request(
            "POST",
            API,
            operation=operation,
            json_body={"query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
            deadline=deadline,
            accept_status=(200, 400),
        )
        body = self.http.decode_json(response, operation)
        if not isinstance(body, dict):
            raise failure(
                "gnomad", operation, "invalid_response", "GraphQL response is not an object"
            )
        return body

    async def variant(
        self,
        variant: CanonicalVariant,
        *,
        dataset: str | None = None,
        deadline: float | None = None,
    ) -> Evidence:
        chosen = dataset_for(variant.assembly, dataset)
        if variant.vcf is None:
            raise failure(
                "gnomad",
                "variant",
                "unsupported",
                "gnomAD variant IDs need an anchored VCF representation; reference sequence was unavailable",
            )
        vid = f"{variant.contig}-{variant.vcf.pos}-{variant.vcf.ref}-{variant.vcf.alt}"
        body = await self._graphql(
            VARIANT_QUERY, {"variantId": vid, "dataset": chosen}, "variant", deadline
        )
        errors = body.get("errors") or []
        data = (body.get("data") or {}).get("variant")
        if data is None:
            messages = (
                "; ".join(str(e.get("message")) for e in errors if isinstance(e, dict)) or "no data"
            )
            kind = "not_found" if "not found" in messages.lower() else "upstream"
            raise failure("gnomad", "variant", kind, f"{vid} in {chosen}: {messages}")
        if data.get("reference_genome") and data["reference_genome"] != variant.assembly:
            raise failure(
                "gnomad",
                "variant",
                "invalid_response",
                f"gnomAD returned {data['reference_genome']} for a {variant.assembly} query",
            )
        if (str(data.get("chrom")), data.get("pos"), data.get("ref"), data.get("alt")) != (
            variant.contig,
            variant.vcf.pos,
            variant.vcf.ref,
            variant.vcf.alt,
        ):
            raise failure(
                "gnomad",
                "variant",
                "invalid_response",
                "gnomAD returned a different allele than requested",
            )
        transformations = [
            Transformation(
                operation="to_gnomad_variant_id", detail=f"queried {vid} (left-aligned VCF form)"
            )
        ]
        limitations = [
            "Frequencies are gnomAD aggregate counts; AN varies by site and population and must be used as the denominator.",
        ]
        if variant.normalization_status != "reference_normalized" and variant.variant_class not in (
            "SNV",
            "MNV",
        ):
            limitations.append(
                "The indel was not reference-normalized; gnomAD may store it at a different position."
            )
        payload: dict[str, Any] = {
            "dataset": chosen,
            "variant_id": data.get("variant_id"),
            "reference_genome": data.get("reference_genome"),
            "rsids": data.get("rsids"),
            "caid": data.get("caid"),
            "flags": data.get("flags"),
        }
        for label in ("exome", "genome", "joint"):
            block = _frequency_block(data.get(label), label, transformations, limitations)
            payload[label] = block
            if block is None:
                limitations.append(f"No {label} data for this variant in {chosen}.")
        if errors:
            limitations.append(
                "gnomAD returned partial GraphQL errors: "
                + "; ".join(str(e.get("message"))[:200] for e in errors if isinstance(e, dict))
            )
        return Evidence(
            source="gnomad",
            evidence_type="population_frequency",
            source_record_id=data.get("variant_id"),
            source_url=f"https://gnomad.broadinstitute.org/variant/{data.get('variant_id')}?dataset={chosen}",
            source_release=f"dataset {chosen}",
            terms_url=INFO.terms_url,
            data={k: v for k, v in payload.items() if v not in (None, [])},
            transformations=transformations,
            limitations=limitations,
        )

    async def constraint(
        self, gene_id: str, assembly: Assembly, *, deadline: float | None = None
    ) -> Evidence:
        body = await self._graphql(
            CONSTRAINT_QUERY,
            {"geneId": gene_id, "referenceGenome": assembly},
            "gene_constraint",
            deadline,
        )
        gene = (body.get("data") or {}).get("gene")
        if gene is None:
            messages = "; ".join(
                str(e.get("message")) for e in body.get("errors") or [] if isinstance(e, dict)
            )
            kind = "not_found" if "not found" in messages.lower() else "upstream"
            raise failure(
                "gnomad",
                "gene_constraint",
                kind,
                f"{gene_id} ({assembly}): {messages or 'no data'}",
            )
        constraint = gene.get("gnomad_constraint")
        limitations = [
            "The API query does not report the constraint release; values are those served for this reference genome.",
        ]
        if constraint is None:
            limitations.append("gnomAD has no constraint metrics for this gene.")
        return Evidence(
            source="gnomad",
            evidence_type="gene_constraint",
            source_record_id=gene.get("gene_id"),
            source_record_version=str(gene["gene_version"]) if gene.get("gene_version") else None,
            source_url=f"https://gnomad.broadinstitute.org/gene/{gene.get('gene_id')}",
            source_release=f"reference genome {assembly}",
            terms_url=INFO.terms_url,
            data={
                "gene_id": gene.get("gene_id"),
                "symbol": gene.get("symbol"),
                "canonical_transcript_id": gene.get("canonical_transcript_id"),
                "constraint": constraint,
            },
            limitations=limitations,
        )
