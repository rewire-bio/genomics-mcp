"""Stable domain models shared by every epic.

Coordinate convention: every structured `Interval` is 0-based, half-open
(`start` inclusive, `end` exclusive), as in BED and pysam. VCF-style variants
(`VariantSpec`) keep VCF's 1-based `pos` and expose `.interval` for conversion.
Assemblies are always explicit; nothing here lifts over or guesses.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from genomics_mcp.errors import ErrorInfo, InvalidInputError, redact_url

_CONTIG = re.compile(r"^[^\s,]+$")
_REGION = re.compile(
    r"^(?:\{(?P<braced>[^}\s]+)\}|(?P<contig>[^\s:{}]+)):(?P<start>[\d,]+)-(?P<end>[\d,]+)$"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- coordinates


class Interval(_Model):
    """Genomic interval, 0-based half-open, on an explicit assembly."""

    contig: str = Field(description="Contig name exactly as used by the target file or source.")
    start: int = Field(ge=0, description="0-based inclusive start.")
    end: int = Field(gt=0, description="0-based exclusive end; must be greater than start.")
    assembly: str = Field(
        min_length=1,
        description="Assembly name or accession, e.g. GRCh38 or GCA_000001405.15. Never inferred.",
    )

    @field_validator("contig", "assembly")
    @classmethod
    def _no_whitespace(cls, v: str) -> str:
        v = v.strip()
        if not v or not _CONTIG.match(v):
            raise ValueError("must be non-empty and contain no whitespace or commas")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> Interval:
        if self.end <= self.start:
            raise ValueError("end must be greater than start (0-based half-open)")
        return self

    @property
    def length(self) -> int:
        return self.end - self.start

    @classmethod
    def from_one_based(cls, contig: str, start: int, end: int, assembly: str) -> Interval:
        """Build from a 1-based fully-closed range such as samtools `chr1:100-200`."""
        if start < 1 or end < start:
            raise InvalidInputError(
                "1-based closed range requires 1 <= start <= end",
                details={"start": start, "end": end},
            )
        return cls(contig=contig, start=start - 1, end=end, assembly=assembly)

    @classmethod
    def parse_region(cls, region: str, assembly: str) -> Interval:
        """Parse an htslib/samtools region string (`chr1:1,001-2,000`, 1-based closed).

        Contigs containing `:` must be braced (`{HLA-A*01:01}:1-100`).
        """
        m = _REGION.match(region.strip())
        if not m:
            raise InvalidInputError(
                "region must look like contig:start-end (1-based closed)",
                details={"region": region},
            )
        contig = m.group("braced") or m.group("contig")
        start = int(m.group("start").replace(",", ""))
        end = int(m.group("end").replace(",", ""))
        return cls.from_one_based(contig, start, end, assembly)

    def to_region(self) -> str:
        """htslib region string (1-based closed). Braces contigs that contain `:`."""
        contig = f"{{{self.contig}}}" if ":" in self.contig else self.contig
        return f"{contig}:{self.start + 1}-{self.end}"

    def overlaps(self, other: Interval) -> bool:
        return (
            self.assembly == other.assembly
            and self.contig == other.contig
            and self.start < other.end
            and other.start < self.end
        )


class VariantSpec(_Model):
    """A VCF-style allele. `pos` is VCF POS (1-based); `.interval` gives the 0-based span of REF."""

    assembly: str = Field(min_length=1)
    contig: str = Field(min_length=1)
    pos: int = Field(ge=1, description="VCF POS, 1-based position of the first REF base.")
    ref: str = Field(min_length=1, pattern=r"^[ACGTNacgtn]+$")
    alt: str = Field(
        min_length=1,
        description="Single ALT allele: bases, or a symbolic allele such as <DEL>.",
    )

    @field_validator("alt")
    @classmethod
    def _alt(cls, v: str) -> str:
        if not re.match(r"^([ACGTNacgtn]+|<[^<>\s]+>|\*)$", v):
            raise ValueError("alt must be bases, '*', or a symbolic allele like <DEL>")
        return v

    @property
    def interval(self) -> Interval:
        return Interval(
            contig=self.contig,
            start=self.pos - 1,
            end=self.pos - 1 + len(self.ref),
            assembly=self.assembly,
        )


# --------------------------------------------------------------------------- provenance / status


class Provenance(_Model):
    """Where a record came from and what this server did to it."""

    source: str = Field(description="Source name, e.g. ena, ega, clinvar, local.")
    source_record_id: str | None = None
    url: str | None = Field(default=None, description="Redacted request or record URL.")
    method: str | None = Field(
        default=None, description="Access method, e.g. 'ENA portal API search' or 'pysam fetch'."
    )
    retrieved_at: datetime = Field(default_factory=utcnow)
    source_version: str | None = Field(default=None, description="Release/version if reported.")
    source_updated_at: datetime | None = None
    terms_url: str | None = None
    transformations: list[str] = Field(
        default_factory=list, description="Ordered steps this server applied, e.g. 'left-aligned'."
    )

    @field_validator("url")
    @classmethod
    def _redact(cls, v: str | None) -> str | None:
        return redact_url(v) if v else v


class SourceState(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    NOT_IMPLEMENTED = "not_implemented"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    SKIPPED = "skipped"


class SourceStatus(_Model):
    source: str
    state: SourceState
    message: str | None = None
    checked_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- files


class FileFormat(StrEnum):
    BAM = "bam"
    CRAM = "cram"
    SAM = "sam"
    VCF = "vcf"
    BCF = "bcf"
    FASTA = "fasta"
    FASTQ = "fastq"
    BED = "bed"
    GFF3 = "gff3"
    GTF = "gtf"
    BIGWIG = "bigwig"
    BIGBED = "bigbed"
    TSV = "tsv"
    OTHER = "other"


class Compression(StrEnum):
    NONE = "none"
    BGZF = "bgzf"
    GZIP = "gzip"
    UNKNOWN = "unknown"


class AccessStatus(StrEnum):
    """Access as reported by the source or observed by this server."""

    OPEN = "open"
    CONTROLLED = "controlled"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    UNKNOWN = "unknown"


class Visibility(StrEnum):
    """Whether values derived from this file may leave the machine. Default private."""

    PUBLIC = "public"
    PRIVATE = "private"


class ReadinessState(StrEnum):
    READY = "ready"
    """Region queries can run now (indexed and range-readable, or local)."""
    DOWNLOAD_REQUIRED = "download_required"
    INDEX_REQUIRED = "index_required"
    REFERENCE_REQUIRED = "reference_required"
    NOT_LOCUS_READY = "not_locus_ready"
    """Downloadable but not queryable by locus, e.g. FASTQ or plain gzip."""
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class Readiness(_Model):
    state: ReadinessState = ReadinessState.UNKNOWN
    reasons: list[str] = Field(default_factory=list)
    checked_at: datetime | None = None


class Checksum(_Model):
    algorithm: Literal["md5", "sha1", "sha256", "sha512", "crc32c", "etag"]
    value: str = Field(min_length=1)


class EntityKind(StrEnum):
    STUDY = "study"
    DATASET = "dataset"
    SAMPLE = "sample"
    FILE = "file"
    RUN = "run"
    EXPERIMENT = "experiment"
    ANALYSIS = "analysis"
    REFERENCE = "reference"
    INDIVIDUAL = "individual"
    POLICY = "policy"


class EntityLink(_Model):
    """A relationship asserted by a source. Never inferred by this server."""

    relation: str = Field(description="e.g. part_of, derived_from, index_of, sample_of.")
    kind: EntityKind
    accession: str
    source: str = Field(description="Who asserted the relationship.")


_ALLOWED_SCHEMES = {"file", "http", "https", "s3", "ftp", "ega", "htsget", "drs"}


def _check_uri(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("uri must be non-empty")
    if v.startswith("/"):
        return v
    parts = urlsplit(v)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"unsupported URI scheme {scheme or '(none)'}; use an absolute path or one of "
            + ", ".join(sorted(_ALLOWED_SCHEMES))
        )
    if parts.username or parts.password:
        raise ValueError("credentials must not be embedded in URIs; configure a storage profile")
    return v


class FileRef(_Model):
    """A file descriptor used by every genomic tool and by archive listings.

    Readers must not guess missing fields: a CRAM without `reference_uri` must be
    self-contained or fail with `preparation_required`.
    """

    uri: str = Field(
        description="Absolute local path, file://, https://, s3://, ega:// or htsget://."
    )
    index_uri: str | None = Field(
        default=None, description="Explicit index (.bai/.crai/.tbi/.csi/.fai)."
    )
    reference_uri: str | None = Field(
        default=None, description="Reference FASTA for CRAM or sequence checks."
    )
    format: FileFormat | None = Field(
        default=None, description="Inferred from the name if omitted."
    )
    compression: Compression | None = None
    assembly: str | None = Field(default=None, description="Assembly of the file coordinates.")
    source: str | None = Field(default=None, description="Archive/source name, or 'local'.")
    accession: str | None = Field(default=None, description="Source-native file accession.")
    storage_profile: str | None = Field(
        default=None, description="Named storage profile from configuration for private S3."
    )
    access_status: AccessStatus = AccessStatus.UNKNOWN
    visibility: Visibility = Field(
        default=Visibility.PRIVATE,
        description="private (default) blocks external annotation of derived values without consent.",
    )
    size_bytes: int | None = Field(default=None, ge=0)
    checksums: list[Checksum] = Field(default_factory=list)
    relationships: list[EntityLink] = Field(default_factory=list)
    readiness: Readiness | None = None
    native: dict[str, Any] = Field(default_factory=dict, description="Source-native metadata.")

    @field_validator("uri", "index_uri", "reference_uri")
    @classmethod
    def _uri(cls, v: str | None) -> str | None:
        return None if v is None else _check_uri(v)

    @property
    def scheme(self) -> str:
        return "file" if self.uri.startswith("/") else urlsplit(self.uri).scheme.lower()

    @property
    def is_local(self) -> bool:
        return self.scheme == "file"

    def display_uri(self) -> str:
        return redact_url(self.uri)

    def effective_format(self) -> FileFormat | None:
        return self.format or infer_format(self.uri)


_SUFFIXES: list[tuple[tuple[str, ...], FileFormat]] = [
    ((".bam",), FileFormat.BAM),
    ((".cram",), FileFormat.CRAM),
    ((".sam",), FileFormat.SAM),
    ((".vcf", ".vcf.gz", ".vcf.bgz"), FileFormat.VCF),
    ((".bcf",), FileFormat.BCF),
    ((".fa", ".fasta", ".fna", ".fa.gz", ".fasta.gz", ".fna.gz"), FileFormat.FASTA),
    ((".fq", ".fastq", ".fq.gz", ".fastq.gz"), FileFormat.FASTQ),
    ((".bed", ".bed.gz", ".bed.bgz"), FileFormat.BED),
    ((".gff", ".gff3", ".gff.gz", ".gff3.gz"), FileFormat.GFF3),
    ((".gtf", ".gtf.gz"), FileFormat.GTF),
    ((".bw", ".bigwig"), FileFormat.BIGWIG),
    ((".bb", ".bigbed"), FileFormat.BIGBED),
]


def infer_format(uri: str) -> FileFormat | None:
    """Infer a format from the path suffix only. Returns None when unknown; never sniffs content."""
    path = urlsplit(uri).path if not uri.startswith("/") else uri
    name = PurePosixPath(path).name.lower()
    best: tuple[int, FileFormat] | None = None
    for suffixes, fmt in _SUFFIXES:
        for s in suffixes:
            if name.endswith(s) and (best is None or len(s) > best[0]):
                best = (len(s), fmt)
    return best[1] if best else None


# --------------------------------------------------------------------------- archive entities


class _Entity(_Model):
    accession: str = Field(min_length=1, description="Source-native accession, preserved verbatim.")
    source: str
    title: str | None = None
    description: str | None = None
    links: list[EntityLink] = Field(default_factory=list)
    native: dict[str, Any] = Field(default_factory=dict, description="Source-native metadata.")
    provenance: list[Provenance] = Field(default_factory=list)


class Study(_Entity):
    kind: Literal["study"] = "study"


class Dataset(_Entity):
    kind: Literal["dataset"] = "dataset"
    access_status: AccessStatus = AccessStatus.UNKNOWN
    policy_accession: str | None = None
    assemblies: list[str] = Field(default_factory=list)
    file_count: int | None = Field(default=None, ge=0)


class PhenotypeValue(_Model):
    """A phenotype/attribute value exactly as supplied by the archive."""

    name: str
    value: str | None = None
    unit: str | None = None
    ontology_term: str | None = Field(default=None, description="e.g. EFO:0000305, if supplied.")
    source: str


class Sample(_Entity):
    kind: Literal["sample"] = "sample"
    organism: str | None = None
    taxon_id: int | None = None
    phenotypes: list[PhenotypeValue] = Field(
        default_factory=list, description="Only values the archive actually supplies."
    )
    phenotype_files: list[FileRef] = Field(default_factory=list)


class Reference(_Entity):
    """A reference assembly or sequence set (not reference-database evidence)."""

    kind: Literal["reference"] = "reference"
    assembly: str
    fasta: FileRef | None = None
    sequence_checksums: dict[str, str] = Field(
        default_factory=dict, description="contig -> MD5 (as in CRAM M5/@SQ M5)."
    )


class EvidenceRecord(_Model):
    """One source-attributed evidence record from a reference database."""

    source: str
    source_record_id: str | None = None
    evidence_type: str = Field(description="e.g. clinical_assertion, population_frequency.")
    observed: bool = Field(description="False for computational predictions.")
    data: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    provenance: Provenance


class Page[T](_Model):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = Field(default=None, description="Total if the source reports it.")


# --------------------------------------------------------------------------- transfers


class TransferState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LocalArtifact(_Model):
    path: str
    size_bytes: int = Field(ge=0)
    checksums: list[Checksum] = Field(default_factory=list)
    checksum_verified: bool = False
    format: FileFormat | None = None
    index_path: str | None = None
    origin: FileRef
    provenance: Provenance


class TransferJob(_Model):
    transfer_id: str
    state: TransferState
    file: FileRef
    budget_bytes: int = Field(ge=0)
    bytes_done: int = Field(default=0, ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    resumable: bool = False
    artifact: LocalArtifact | None = None
    error: ErrorInfo | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
