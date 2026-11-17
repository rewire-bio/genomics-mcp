"""Typed records for the synthetic-data protocol catalogue and the truth-set catalogue.

Everything here is curated, versioned data. Nothing is generated or downloaded at call time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from genomics_mcp.models import FileRef

VariantType = Literal["snv", "indel", "sv"]
Method = Literal["spike_in", "read_mixture", "read_simulation", "sv_simulation"]
Setting = Literal["somatic", "germline"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Citation(_Record):
    key: str = Field(description="Short key used by parameters to cite this work.")
    doi: str | None = None
    url: str
    authors: str = Field(description="First author et al.")
    title: str
    venue: str
    year: int


class Simulator(_Record):
    name: str
    version: str = Field(description="Pinned release, tag or commit the parameters were read from.")
    homepage: str
    source_url: str = Field(description="Where the documented options were read.")
    license: str | None = None
    install: str | None = Field(default=None, description="Package name, e.g. bioconda::art.")


class Parameter(_Record):
    name: str
    option: str | None = Field(default=None, description="Command-line option or config key.")
    default: str | None = Field(default=None, description="Default in the pinned version.")
    published_value: str | None = Field(
        default=None, description="Value used in the cited study, if it reports one."
    )
    meaning: str
    source: str = Field(description="Citation key or documentation URL for this parameter.")


class Recipe(_Record):
    """The rewirebench recipe that will run this protocol. Not yet published."""

    id: str = Field(description="Reserved recipe identifier.")
    status: Literal["planned"] = Field(
        default="planned",
        description="planned: the ID is reserved but no runnable recipe exists yet.",
    )


class Protocol(_Record):
    id: str
    title: str
    method: Method
    setting: Setting | None = Field(
        default=None, description="Somatic or germline benchmarking, if the protocol is specific."
    )
    variant_types: list[VariantType] = Field(description="Variant types with simulated truth.")
    input: str = Field(description="What the protocol starts from.")
    output: str = Field(description="What it produces, including its truth file.")
    summary: str
    simulator: Simulator
    citations: list[Citation]
    parameters: list[Parameter]
    published_settings: list[str] = Field(
        default_factory=list, description="Settings reported by the cited studies, with keys."
    )
    truth_sets: list[str] = Field(
        default_factory=list,
        description="Truth-set IDs (get_truth_set) usable as the background sample or "
        "confident regions for this protocol.",
    )
    recipe: Recipe
    caveats: list[str] = Field(default_factory=list)

    def summary_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "method": self.method,
            "setting": self.setting,
            "variant_types": list(self.variant_types),
            "simulator": f"{self.simulator.name} {self.simulator.version}",
            "citation": self.citations[0].doi or self.citations[0].url,
            "truth_sets": list(self.truth_sets),
            "recipe_id": self.recipe.id,
            "recipe_status": self.recipe.status,
        }


FileRole = Literal[
    "benchmark_vcf",
    "benchmark_regions",
    "reference_fasta",
    "assembly_fasta",
]


class TruthFile(_Record):
    role: FileRole
    file: FileRef
    published_size: str | None = Field(
        default=None, description="Size as shown by the publisher's listing, when bytes unknown."
    )


class TruthSet(_Record):
    id: str
    title: str
    provider: Literal["giab", "hprc"]
    sample: str = Field(description="GIAB/Coriell sample name, e.g. HG002.")
    release: str = Field(description="Publisher's release name, pinned.")
    assembly: str = Field(description="Build of every file in the bundle, e.g. GRCh38.")
    file_assembly: str = Field(
        description="Assembly name exactly as the files declare it (VCF contig headers). "
        "FileRefs and returned intervals use it so readers accept them."
    )
    contig_style: Literal["chr", "plain", "assembly"] = Field(
        description="chr: chr1..chr22; plain: 1..22; assembly: haplotype contig names."
    )
    contigs: list[str] | None = Field(
        description="Contigs the benchmark covers, named as in its files; None for an assembly."
    )
    variant_types: list[VariantType] = Field(default_factory=list)
    files: list[TruthFile]
    citations: list[Citation]
    release_url: str
    notes: list[str] = Field(default_factory=list)

    def summary_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "provider": self.provider,
            "sample": self.sample,
            "release": self.release,
            "assembly": self.assembly,
            "file_assembly": self.file_assembly,
            "variant_types": list(self.variant_types),
            "roles": [f.role for f in self.files],
        }
