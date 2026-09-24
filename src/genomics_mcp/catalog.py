"""Planned sources. Listed so callers can see what exists and what is not implemented yet.

Providers call `registry.register_source(...)` to replace an entry with current details.
A source is reported available only when a handler or resolver is registered for it.
"""

from __future__ import annotations

from genomics_mcp.registry import SourceInfo

PLANNED_SOURCES: list[SourceInfo] = [
    SourceInfo("local", "Local files under allowed roots", "local", "E2", auth="none"),
    SourceInfo("https", "Public HTTP(S) byte-range files", "storage", "E2", auth="none"),
    SourceInfo(
        "s3",
        "Anonymous public S3 or explicitly configured S3-compatible profiles",
        "storage",
        "E2",
        auth="explicit_profile",
        notes="No ambient AWS credentials; requester-pays off unless a profile enables it.",
    ),
    SourceInfo(
        "ega",
        "European Genome-phenome Archive",
        "archive",
        "E6",
        homepage="https://ega-archive.org/",
        auth="account",
        notes="Public metadata; controlled file access needs an EGA account with DAC approval.",
    ),
    SourceInfo(
        "ena",
        "European Nucleotide Archive",
        "archive",
        "E6",
        homepage="https://www.ebi.ac.uk/ena/browser/",
        terms_url="https://www.ebi.ac.uk/about/terms-of-use",
    ),
    SourceInfo(
        "encode", "ENCODE portal", "catalog", "E7", homepage="https://www.encodeproject.org/"
    ),
    SourceInfo(
        "geo",
        "NCBI Gene Expression Omnibus",
        "catalog",
        "E7",
        homepage="https://www.ncbi.nlm.nih.gov/geo/",
    ),
    SourceInfo(
        "ncbi_datasets",
        "NCBI Datasets (assemblies, sequences, annotation)",
        "catalog",
        "E7",
        homepage="https://www.ncbi.nlm.nih.gov/datasets/",
        auth="optional_key",
    ),
    SourceInfo(
        "ensembl",
        "Ensembl REST",
        "reference",
        "E8",
        homepage="https://rest.ensembl.org/",
        terms_url="https://www.ensembl.org/info/about/legal/",
    ),
    SourceInfo(
        "hgnc", "HGNC gene nomenclature", "reference", "E8", homepage="https://www.genenames.org/"
    ),
    SourceInfo(
        "clinvar",
        "ClinVar",
        "reference",
        "E8",
        homepage="https://www.ncbi.nlm.nih.gov/clinvar/",
        auth="optional_key",
    ),
    SourceInfo(
        "gnomad",
        "gnomAD",
        "reference",
        "E8",
        homepage="https://gnomad.broadinstitute.org/",
        notes="Public API is rate limited (about 10 requests/minute).",
    ),
    SourceInfo(
        "uniprot",
        "UniProt",
        "reference",
        "E8",
        homepage="https://www.uniprot.org/",
        terms_url="https://www.uniprot.org/help/license",
    ),
    SourceInfo(
        "open_targets",
        "Open Targets Platform",
        "reference",
        "E8",
        homepage="https://platform.opentargets.org/",
    ),
    SourceInfo(
        "alphagenome_atlas",
        "AlphaGenome Atlas (precomputed predictions)",
        "reference",
        "E8",
        homepage="https://www.alphagenomedocs.com/api/atlas.html",
        auth="required_key",
        notes="Optional. Predictions only; no live model inference.",
    ),
]

# Storage sources map to URI schemes rather than handler keys.
SOURCE_SCHEMES: dict[str, tuple[str, ...]] = {
    "local": ("file",),
    "https": ("https", "http"),
    "s3": ("s3",),
}
