"""Request and response models for the five reference tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    CanonicalVariant,
    Evidence,
    IdentifierCandidate,
    SourceError,
    ToolStatus,
    Transformation,
    Truncation,
    VariantCandidate,
)

VariantSource = Literal["ensembl", "clinvar", "gnomad", "alphagenome_atlas"]
GeneInclude = Literal["identifiers", "transcripts", "protein", "disease", "constraint"]


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenomicAlleleInput(_Req):
    """A structured allele. Give ``position`` (1-based VCF POS) or ``start`` (0-based), not both."""

    assembly: str = Field(description="GRCh38 or GRCh37 (hg38/hg19 accepted and reported)")
    contig: str = Field(description="1-22, X, Y, MT; chr prefix or NC_ accession accepted")
    position: int | None = Field(default=None, description="1-based VCF POS of the first REF base")
    start: int | None = Field(default=None, description="0-based start (0-based half-open)")
    ref: str
    alt: str = Field(description="ALT allele; comma-separated ALTs are split into separate alleles")

    @model_validator(mode="after")
    def _one_coordinate(self) -> GenomicAlleleInput:
        if (self.position is None) == (self.start is None):
            raise ValueError("give exactly one of position (1-based) or start (0-based)")
        return self


class ReferenceSequenceInput(_Req):
    """Caller-supplied actual reference sequence for [start, start+len(sequence)), 0-based."""

    assembly: str
    contig: str
    start: int = Field(ge=0)
    sequence: str = Field(min_length=1, max_length=100_000)
    source: str = Field(default="caller", description="Label reported as the reference source")
    at_contig_start: bool = False
    at_contig_end: bool = False


class _VariantRequest(_Req):
    variant: str | GenomicAlleleInput = Field(
        description="VCF string (7-140753336-A-T), SPDI, genomic/coding/protein HGVS with versioned accession, "
        "rsID, or a structured allele"
    )
    assembly: str | None = Field(default=None, description="Required unless implied by a versioned NC_ accession")
    reference: ReferenceSequenceInput | None = None
    use_remote_reference: bool = Field(
        default=True, description="Fetch reference bases from Ensembl (then NCBI) to verify REF and shift indels"
    )
    query_origin: Literal["user_supplied", "private_file"] = "user_supplied"
    allow_external_queries: bool = Field(
        default=False,
        description="Required when query_origin is private_file: explicit per-call consent to send the variant to public services",
    )


class NormalizeVariantRequest(_VariantRequest):
    pass


class LookupVariantRequest(_VariantRequest):
    sources: list[VariantSource] = Field(default_factory=lambda: ["ensembl", "clinvar", "gnomad"])
    gnomad_dataset: Literal["gnomad_r4", "gnomad_r3", "gnomad_r2_1"] | None = None
    atlas_scorers: list[str] = Field(default_factory=list, description="Atlas scorer names; empty returns all scorers")
    max_clinvar_records: int = Field(default=3, ge=1, le=10)


class LookupGeneRequest(_Req):
    gene: str = Field(description="HGNC symbol/alias/previous symbol, HGNC:ID, Ensembl gene ID or UniProt accession")
    id_type: Literal["symbol", "hgnc_id", "ensembl_gene_id", "entrez_id", "uniprot_ids"] | None = None
    assembly: Literal["GRCh38", "GRCh37"] = "GRCh38"
    include: list[GeneInclude] = Field(
        default_factory=lambda: ["identifiers", "transcripts", "protein", "disease", "constraint"]
    )
    max_associations: int = Field(default=25, ge=1, le=100)


class LookupProteinRequest(_Req):
    protein: str = Field(description="UniProt accession, Ensembl protein/transcript ID, RefSeq protein, or gene")
    assembly: Literal["GRCh38", "GRCh37"] = "GRCh38"
    include_all_features: bool = False


class ResolveIdentifierRequest(_Req):
    identifier: str
    id_type: Literal["symbol", "hgnc_id", "ensembl_gene_id", "entrez_id", "uniprot_ids"] | None = None
    assembly: str | None = Field(default=None, description="Needed for variant identifiers and Ensembl coordinates")


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    status: ToolStatus
    query: dict[str, Any]
    errors: list[SourceError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    transformations: list[Transformation] = Field(default_factory=list)
    truncation: list[Truncation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class NormalizeVariantResult(ToolResult):
    input_kind: str
    canonical_variant: CanonicalVariant | None = None
    alleles: list[CanonicalVariant] = Field(
        default_factory=list, description="Every normalized allele; more than one means the input was multi-allelic"
    )
    candidates: list[VariantCandidate] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class VariantEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consequence: list[Evidence] = Field(default_factory=list)
    clinical: list[Evidence] = Field(default_factory=list)
    population: list[Evidence] = Field(default_factory=list)
    functional_prediction: list[Evidence] = Field(default_factory=list)


class LookupVariantResult(ToolResult):
    normalization: NormalizeVariantResult
    canonical_variant: CanonicalVariant | None = None
    gene_context: list[dict[str, Any]] = Field(default_factory=list)
    evidence: VariantEvidence = Field(default_factory=VariantEvidence)


class LookupGeneResult(ToolResult):
    gene: dict[str, Any] | None = None
    candidates: list[IdentifierCandidate] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class LookupProteinResult(ToolResult):
    protein: dict[str, Any] | None = None
    candidates: list[IdentifierCandidate] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class ResolveIdentifierResult(ToolResult):
    identifier_type: str
    resolved: dict[str, Any] = Field(default_factory=dict)
    candidates: list[IdentifierCandidate] = Field(default_factory=list)
    variant_candidates: list[VariantCandidate] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
