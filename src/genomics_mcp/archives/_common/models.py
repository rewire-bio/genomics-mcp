"""Domain models used by the archive (E6) and catalog (E7) clients.

Field names and enum values deliberately match core `genomics_mcp.models`
(Interval, Provenance, FileRef, Study, Dataset, Sample, PhenotypeValue, Reference,
EntityLink, Readiness, Checksum). Integration converts with
`core.Model.model_validate(obj.model_dump())`; the types here are not a second
public contract. Intervals are 0-based half-open on an explicit assembly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from genomics_mcp.archives._common.redact import redact_url


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Interval(_Model):
    """0-based half-open interval (`start` inclusive, `end` exclusive)."""

    contig: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    assembly: str = Field(min_length=1)

    @field_validator("contig", "assembly")
    @classmethod
    def _no_whitespace(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v) or "," in v:
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


class Provenance(_Model):
    source: str
    source_record_id: str | None = None
    url: str | None = None
    method: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    source_version: str | None = None
    source_updated_at: datetime | None = None
    terms_url: str | None = None
    transformations: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _redact(cls, v: str | None) -> str | None:
        return redact_url(v) if v else v


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
    OPEN = "open"
    CONTROLLED = "controlled"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    UNKNOWN = "unknown"


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class ReadinessState(StrEnum):
    READY = "ready"
    DOWNLOAD_REQUIRED = "download_required"
    INDEX_REQUIRED = "index_required"
    REFERENCE_REQUIRED = "reference_required"
    NOT_LOCUS_READY = "not_locus_ready"
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
    """A relationship asserted by `source`. Never inferred by this server."""

    relation: str
    kind: EntityKind
    accession: str
    source: str


_ALLOWED_SCHEMES = {"file", "http", "https", "s3", "ftp", "ega", "htsget", "drs"}


def _check_uri(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("uri must be non-empty")
    if v.startswith("/"):
        return v
    parts = urlsplit(v)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URI scheme {parts.scheme or '(none)'}")
    if parts.username or parts.password:
        raise ValueError("credentials must not be embedded in URIs")
    return v


class FileRef(_Model):
    uri: str
    index_uri: str | None = None
    reference_uri: str | None = None
    format: FileFormat | None = None
    compression: Compression | None = None
    assembly: str | None = None
    source: str | None = None
    accession: str | None = None
    storage_profile: str | None = None
    access_status: AccessStatus = AccessStatus.UNKNOWN
    visibility: Visibility = Visibility.PRIVATE
    size_bytes: int | None = Field(default=None, ge=0)
    checksums: list[Checksum] = Field(default_factory=list)
    relationships: list[EntityLink] = Field(default_factory=list)
    readiness: Readiness | None = None
    native: dict[str, Any] = Field(default_factory=dict)

    @field_validator("uri", "index_uri", "reference_uri")
    @classmethod
    def _uri(cls, v: str | None) -> str | None:
        return None if v is None else _check_uri(v)


class _Entity(_Model):
    accession: str = Field(min_length=1)
    source: str
    title: str | None = None
    description: str | None = None
    links: list[EntityLink] = Field(default_factory=list)
    native: dict[str, Any] = Field(default_factory=dict)
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
    """A value exactly as supplied by the archive (name and value verbatim)."""

    name: str
    value: str | None = None
    unit: str | None = None
    ontology_term: str | None = None
    source: str


class Sample(_Entity):
    kind: Literal["sample"] = "sample"
    organism: str | None = None
    taxon_id: int | None = None
    phenotypes: list[PhenotypeValue] = Field(default_factory=list)
    phenotype_files: list[FileRef] = Field(default_factory=list)


class Reference(_Entity):
    kind: Literal["reference"] = "reference"
    assembly: str
    fasta: FileRef | None = None
    sequence_checksums: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- client results


class SourcePage[T](_Model):
    """One page from a source. `next_cursor` is opaque; `total` only when the source reports it."""

    items: list[T]
    next_cursor: str | None = None
    total: int | None = None
    truncated: bool = False
    truncation_reason: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DatasetDetail(_Model):
    """describe_dataset result: the dataset plus source-asserted related entities."""

    dataset: Dataset
    studies: list[Study] = Field(default_factory=list)
    related: dict[str, Any] = Field(
        default_factory=dict, description="Other source records (policy, DAC, platform, counts)."
    )
    provenance: list[Provenance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Artifact(_Model):
    """A local file written by a client. Mirrors core `LocalArtifact`."""

    path: str
    size_bytes: int = Field(ge=0)
    checksums: list[Checksum] = Field(default_factory=list)
    checksum_verified: bool = False
    format: FileFormat | None = None
    index_path: str | None = None
    origin: FileRef
    provenance: Provenance


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
    ((".tsv", ".tsv.gz"), FileFormat.TSV),
]

INDEX_SUFFIXES = (".bai", ".crai", ".tbi", ".csi", ".fai")


def infer_format(name: str) -> FileFormat | None:
    """Format from a file name suffix only; None when unknown. Never sniffs content."""
    path = urlsplit(name).path if "://" in name else name
    base = PurePosixPath(path).name.lower()
    best: tuple[int, FileFormat] | None = None
    for suffixes, fmt in _SUFFIXES:
        for s in suffixes:
            if base.endswith(s) and (best is None or len(s) > best[0]):
                best = (len(s), fmt)
    return best[1] if best else None


def is_index_name(name: str) -> bool:
    return (
        PurePosixPath(urlsplit(name).path if "://" in name else name)
        .name.lower()
        .endswith(INDEX_SUFFIXES)
    )


def compression_from_name(name: str) -> Compression | None:
    """A `.gz`/`.bgz` name is `unknown` until content proves BGZF (see `sniff_compression`)."""
    lower = urlsplit(name).path.lower() if "://" in name else name.lower()
    if lower.endswith((".gz", ".bgz")):
        return Compression.UNKNOWN
    return None


def sniff_compression(head: bytes) -> Compression:
    """Classify from the first bytes: BGZF needs the gzip FEXTRA `BC` subfield."""
    if len(head) >= 16 and head[:4] == b"\x1f\x8b\x08\x04" and head[12:14] == b"BC":
        return Compression.BGZF
    if head[:2] == b"\x1f\x8b":
        return Compression.GZIP
    return Compression.NONE if head else Compression.UNKNOWN
