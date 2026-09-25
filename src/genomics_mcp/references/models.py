"""Typed evidence, error and variant models for reference lookups.

Coordinates in every model here are 0-based half-open unless a field name says
otherwise (``vcf.pos`` is the 1-based VCF POS of the first REF base).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Assembly = Literal["GRCh37", "GRCh38"]

ErrorKind = Literal[
    "invalid_input",
    "not_found",
    "unauthorized",
    "forbidden",
    "rate_limited",
    "timeout",
    "upstream",
    "invalid_response",
    "unsupported",
    "not_configured",
]

ToolStatus = Literal["ok", "partial", "ambiguous", "unresolved", "error"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Transformation(_Model):
    """One explicit step applied to an input or a source record."""

    operation: str
    source: str = "local"
    detail: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class Truncation(_Model):
    field: str
    returned: int
    available: int | None = None
    reason: str


class SourceError(_Model):
    """A failure isolated to one source operation.

    ``url`` never contains query strings; secrets and signed parameters are not
    reported.
    """

    source: str
    operation: str
    kind: ErrorKind
    message: str
    status_code: int | None = None
    retryable: bool = False
    url: str | None = None


class Evidence(_Model):
    """A compact, source-attributed record.

    ``source_release`` is only set when the source reported it. Nothing here is
    a cross-source consensus; ``data`` holds source-native values.
    """

    source: str
    evidence_type: str
    source_record_id: str | None = None
    source_record_version: str | None = None
    source_url: str | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    source_release: str | None = None
    source_updated_at: str | None = None
    terms_url: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    transformations: list[Transformation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    truncation: list[Truncation] = Field(default_factory=list)


class SourceOutcome(_Model):
    """Result of one source operation: evidence and/or isolated errors."""

    source: str
    operation: str
    evidence: list[Evidence] = Field(default_factory=list)
    errors: list[SourceError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VcfRepresentation(_Model):
    contig: str
    pos: int = Field(description="1-based VCF POS of the first REF base")
    ref: str
    alt: str


class ReferenceCheck(_Model):
    status: Literal["verified", "mismatch", "not_checked", "unavailable"]
    source: str | None = None
    expected_ref: str | None = None
    observed_ref: str | None = None
    detail: str | None = None


NormalizationStatus = Literal[
    "reference_normalized",
    "trimmed_only",
    "reference_mismatch",
]


class CanonicalVariant(_Model):
    """A single genomic allele in 0-based half-open coordinates.

    ``ref``/``alt`` are minimal alleles (empty for pure insertions/deletions).
    When ``normalization_status`` is ``trimmed_only`` the alleles were trimmed
    locally but not shifted against reference sequence, so indels may not be in
    their left-aligned (VCF) or 3'-shifted (HGVS) positions.
    """

    assembly: Assembly
    contig: str
    refseq_accession: str | None = None
    start: int
    end: int
    ref: str
    alt: str
    coordinate_system: Literal["0-based half-open"] = "0-based half-open"
    variant_class: str
    normalization_status: NormalizationStatus
    reference_check: ReferenceCheck
    vcf: VcfRepresentation | None = None
    spdi: str | None = Field(
        default=None,
        description="Left-shifted minimal SPDI (0-based interbase). Not NCBI's fully-justified form.",
    )
    ncbi_canonical_spdi: str | None = Field(
        default=None,
        description="Fully justified SPDI as used by NCBI/ClinVar; only derived with reference sequence.",
    )
    hgvs_g: str | None = None


class VariantCandidate(_Model):
    """A possible allele that was not selected automatically."""

    description: str
    source: str
    assembly: Assembly | None = None
    contig: str | None = None
    vcf: VcfRepresentation | None = None
    spdi: str | None = None
    hgvs: list[str] = Field(default_factory=list)
    identifiers: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class IdentifierCandidate(_Model):
    entity_type: str
    identifier: str
    label: str | None = None
    source: str
    match_type: str
    notes: list[str] = Field(default_factory=list)


def compact_text(text: str | None, limit: int = 600) -> tuple[str | None, bool]:
    """Return text capped to ``limit`` characters and whether it was cut."""
    if text is None:
        return None, False
    text = " ".join(text.split())
    if len(text) <= limit:
        return text, False
    return text[: limit - 1] + "…", True
