"""Static, non-secret metadata for reference sources (docs, terms, limits)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SourceInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    title: str
    base_urls: tuple[str, ...]
    docs_url: str
    terms_url: str
    auth: str
    rate_limit: str
    notes: tuple[str, ...] = ()


SOURCES: dict[str, SourceInfo] = {
    "hgnc": SourceInfo(
        name="hgnc",
        title="HGNC REST",
        base_urls=("https://rest.genenames.org",),
        docs_url="https://www.genenames.org/help/rest/",
        terms_url="https://www.genenames.org/about/license/",
        auth="none",
        rate_limit="10 requests/second (HGNC guidance)",
    ),
    "ensembl": SourceInfo(
        name="ensembl",
        title="Ensembl REST",
        base_urls=("https://rest.ensembl.org", "https://grch37.rest.ensembl.org"),
        docs_url="https://rest.ensembl.org/documentation/",
        terms_url="https://www.ensembl.org/info/about/legal/disclaimer.html",
        auth="none",
        rate_limit="10 requests/second locally (Ensembl allows 15/second)",
        notes=(
            "GRCh38 requests use rest.ensembl.org; GRCh37 requests use grch37.rest.ensembl.org.",
            "The GRCh37 gene set is frozen; the reported release is the REST API release.",
        ),
    ),
    "ncbi_variation": SourceInfo(
        name="ncbi_variation",
        title="NCBI Variation Services",
        base_urls=("https://api.ncbi.nlm.nih.gov/variation/v0",),
        docs_url="https://api.ncbi.nlm.nih.gov/variation/v0/",
        terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
        auth="none",
        rate_limit="shares the NCBI 3 requests/second unkeyed budget",
    ),
    "ncbi_nuccore": SourceInfo(
        name="ncbi_nuccore",
        title="NCBI E-utilities nuccore sequence",
        base_urls=("https://eutils.ncbi.nlm.nih.gov/entrez/eutils",),
        docs_url="https://www.ncbi.nlm.nih.gov/books/NBK25499/",
        terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
        auth="optional NCBI API key (explicit configuration only)",
        rate_limit="3 requests/second unkeyed, 10/second with an API key",
    ),
    "clinvar": SourceInfo(
        name="clinvar",
        title="ClinVar via NCBI E-utilities",
        base_urls=("https://eutils.ncbi.nlm.nih.gov/entrez/eutils",),
        docs_url="https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/",
        terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
        auth="optional NCBI API key (explicit configuration only)",
        rate_limit="3 requests/second unkeyed, 10/second with an API key",
        notes=("Full assertions come from EFetch rettype=vcv (VCV XML), not ESummary.",),
    ),
    "gnomad": SourceInfo(
        name="gnomad",
        title="gnomAD GraphQL API",
        base_urls=("https://gnomad.broadinstitute.org/api",),
        docs_url="https://github.com/broadinstitute/gnomad-browser",
        terms_url="https://gnomad.broadinstitute.org/policies",
        auth="none",
        rate_limit="10 requests/minute (public API)",
    ),
    "uniprot": SourceInfo(
        name="uniprot",
        title="UniProt REST",
        base_urls=("https://rest.uniprot.org",),
        docs_url="https://www.uniprot.org/help/api",
        terms_url="https://www.uniprot.org/help/license",
        auth="none",
        rate_limit="10 requests/second locally",
    ),
    "opentargets": SourceInfo(
        name="opentargets",
        title="Open Targets Platform GraphQL",
        base_urls=("https://api.platform.opentargets.org/api/v4/graphql",),
        docs_url="https://platform-docs.opentargets.org/data-access/graphql-api",
        terms_url="https://platform-docs.opentargets.org/licence",
        auth="none",
        rate_limit="5 requests/second locally",
    ),
    "alphagenome_atlas": SourceInfo(
        name="alphagenome_atlas",
        title="AlphaGenome Atlas (precomputed predictions)",
        base_urls=("dns:///gdmscience.googleapis.com:443",),
        docs_url="https://www.alphagenomedocs.com/api/atlas.html",
        terms_url="https://alphagenome.google/terms",
        auth="explicit AlphaGenome API key; disabled without one",
        rate_limit="provider-defined; 2 requests/second locally",
        notes=(
            "Only AtlasService GetDenseVariantScores and ListVariantScoresMetadata are used.",
            "No live AlphaGenome model inference is performed.",
            "Outputs are for non-commercial use, not for clinical decision-making, and must not be used to train other ML models (AlphaGenome terms).",
        ),
    ),
}
