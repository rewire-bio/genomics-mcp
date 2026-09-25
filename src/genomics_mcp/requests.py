"""Typed request models, one per operation. Handlers receive these, already validated.

Field names match the MCP tool parameters in server.py one-to-one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from genomics_mcp.models import FileFormat, FileRef, Interval, VariantSpec
from genomics_mcp.registry import Operation

# samtools flag bits: UNMAP 0x4, SECONDARY 0x100, QCFAIL 0x200, DUP 0x400.
DEPTH_DEFAULT_EXCLUDE_FLAGS = 0x704


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Bounded(_Request):
    max_records: int | None = Field(
        default=None, ge=1, description="Lower the record limit for this call (cannot raise it)."
    )
    max_response_bytes: int | None = Field(default=None, ge=4096)


class _SingleFile(_Bounded):
    file: FileRef
    interval: Interval

    @model_validator(mode="after")
    def _same_assembly(self) -> _SingleFile:
        if self.file.assembly and self.file.assembly != self.interval.assembly:
            raise ValueError(
                f"interval assembly {self.interval.assembly!r} does not match file assembly "
                f"{self.file.assembly!r}; no liftover is performed"
            )
        return self


# --- discovery ---------------------------------------------------------------------


class ListSourcesRequest(_Request):
    kind: Literal["archive", "catalog", "reference", "storage", "local"] | None = None


class SearchDatasetsRequest(_Bounded):
    source: str = Field(description="Source name from list_sources, e.g. ega, ena, encode.")
    query: str = Field(min_length=1, max_length=500)
    assembly: str | None = None
    organism: str | None = None
    cursor: str | None = None


class DescribeDatasetRequest(_Request):
    source: str
    accession: str = Field(min_length=1, description="Native study/dataset/experiment accession.")


class ListFilesRequest(_Bounded):
    source: str
    accession: str = Field(min_length=1)
    formats: list[FileFormat] | None = None
    cursor: str | None = None
    storage_profile: str | None = Field(
        default=None,
        description="Configured storage profile for private S3 listings (source 's3'). "
        "Omitted: anonymous public S3.",
    )


class ListSamplesRequest(_Bounded):
    source: str
    accession: str = Field(min_length=1)
    cursor: str | None = None


class GetSampleMetadataRequest(_Request):
    source: str
    accession: str = Field(min_length=1, description="Native sample accession.")


# --- transfers ---------------------------------------------------------------------


class FetchFileRequest(_Request):
    file: FileRef
    include_index: bool = True
    budget_bytes: int | None = Field(
        default=None,
        ge=1,
        description="Explicit byte budget. Required when the file exceeds the default transfer limit.",
    )
    verify_checksum: bool = True
    prepare: bool = Field(
        default=False,
        description="Build missing indexes on the local copy in the work dir (FASTA .fai/.gzi, "
        "tabix, BAM/BCF index; ordinary gzip is recompressed to BGZF). Sources are never modified.",
    )


class TransferStatusRequest(_Request):
    transfer_id: str = Field(min_length=1)


class CancelTransferRequest(_Request):
    transfer_id: str = Field(min_length=1)


# --- genomics ----------------------------------------------------------------------


class ReadsRequest(_SingleFile):
    reference: FileRef | None = Field(default=None, description="Reference FASTA for CRAM.")
    min_mapping_quality: int = Field(default=0, ge=0, le=255)
    require_flags: int = Field(default=0, ge=0, le=0xFFF, description="samtools -f")
    exclude_flags: int = Field(default=0, ge=0, le=0xFFF, description="samtools -F")
    include_sequence: bool = False


class CoverageRequest(_SingleFile):
    reference: FileRef | None = None
    min_mapping_quality: int = Field(default=0, ge=0, le=255)
    min_base_quality: int = Field(default=0, ge=0, le=93)
    exclude_flags: int = Field(default=DEPTH_DEFAULT_EXCLUDE_FLAGS, ge=0, le=0xFFF)
    bin_size: int | None = Field(default=None, ge=1, description="Per-base when omitted.")


class PileupRequest(_SingleFile):
    reference: FileRef | None = None
    min_mapping_quality: int = Field(default=0, ge=0, le=255)
    min_base_quality: int = Field(default=13, ge=0, le=93)
    exclude_flags: int = Field(default=DEPTH_DEFAULT_EXCLUDE_FLAGS, ge=0, le=0xFFF)
    max_depth: int = Field(default=8000, ge=1)


class VariantsRequest(_SingleFile):
    samples: list[str] | None = Field(default=None, description="Subset of sample IDs.")
    include_genotypes: bool = True
    pass_only: bool = False


class SequenceRequest(_SingleFile):
    pass


class FeaturesRequest(_SingleFile):
    feature_types: list[str] | None = None


class SignalRequest(_SingleFile):
    bins: int | None = Field(default=None, ge=1, description="Summarize into N bins.")
    summary: Literal["mean", "min", "max", "coverage", "std", "sum"] = "mean"


# --- composition -------------------------------------------------------------------


class InspectLocusRequest(_Bounded):
    interval: Interval
    files: list[FileRef] = Field(default_factory=list)
    reference: FileRef | None = None
    reference_sources: list[str] | None = Field(
        default=None, description="Public evidence sources to consult for the locus."
    )
    allow_external_annotation: bool = Field(
        default=False,
        description="Permit sending values derived from private files to external sources.",
    )


class CompareSamplesRequest(_Bounded):
    interval: Interval
    files: list[FileRef] = Field(min_length=1)
    samples: list[str] | None = None
    reference: FileRef | None = None


# --- reference ---------------------------------------------------------------------


class _SourceSelect(_Request):
    sources: list[str] | None = Field(default=None, description="Default: all available.")


class _VariantInput(_SourceSelect):
    variant: VariantSpec | None = None
    hgvs: str | None = None
    rsid: str | None = Field(default=None, pattern=r"^rs\d+$")
    assembly: str | None = None

    @model_validator(mode="after")
    def _one(self) -> _VariantInput:
        given = [x for x in (self.variant, self.hgvs, self.rsid) if x is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of variant, hgvs, rsid")
        if self.variant is not None and self.assembly and self.assembly != self.variant.assembly:
            raise ValueError("assembly conflicts with variant.assembly")
        return self


class ResolveIdentifierRequest(_SourceSelect):
    identifier: str = Field(min_length=1, max_length=200)
    target_types: list[str] | None = None
    assembly: str | None = None


class NormalizeVariantRequest(_VariantInput):
    reference: FileRef | None = Field(default=None, description="Local FASTA for ref checks.")


class LookupVariantRequest(_VariantInput):
    include: list[str] | None = None


class LookupGeneRequest(_SourceSelect):
    gene: str = Field(min_length=1, max_length=100)
    assembly: str | None = None
    include: list[str] | None = None


class LookupProteinRequest(_SourceSelect):
    protein: str = Field(min_length=1, max_length=100)


REQUEST_MODELS: dict[Operation, type[BaseModel]] = {
    Operation.LIST_SOURCES: ListSourcesRequest,
    Operation.SEARCH_DATASETS: SearchDatasetsRequest,
    Operation.DESCRIBE_DATASET: DescribeDatasetRequest,
    Operation.LIST_FILES: ListFilesRequest,
    Operation.LIST_SAMPLES: ListSamplesRequest,
    Operation.GET_SAMPLE_METADATA: GetSampleMetadataRequest,
    Operation.FETCH_FILE: FetchFileRequest,
    Operation.GET_TRANSFER_STATUS: TransferStatusRequest,
    Operation.CANCEL_TRANSFER: CancelTransferRequest,
    Operation.GET_READS: ReadsRequest,
    Operation.GET_COVERAGE: CoverageRequest,
    Operation.GET_PILEUP: PileupRequest,
    Operation.GET_VARIANTS: VariantsRequest,
    Operation.GET_SEQUENCE: SequenceRequest,
    Operation.GET_FEATURES: FeaturesRequest,
    Operation.GET_SIGNAL: SignalRequest,
    Operation.INSPECT_LOCUS: InspectLocusRequest,
    Operation.COMPARE_SAMPLES: CompareSamplesRequest,
    Operation.RESOLVE_IDENTIFIER: ResolveIdentifierRequest,
    Operation.NORMALIZE_VARIANT: NormalizeVariantRequest,
    Operation.LOOKUP_VARIANT: LookupVariantRequest,
    Operation.LOOKUP_GENE: LookupGeneRequest,
    Operation.LOOKUP_PROTEIN: LookupProteinRequest,
}
