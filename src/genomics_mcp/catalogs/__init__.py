"""Public catalogs (E7): ENCODE, GEO and NCBI Datasets.

`register(registry)` adds discovery handlers for `encode`, `geo` and `ncbi_datasets`, and the
`preparer:ncbi_genome_fasta` component (explicit ZIP -> FASTA + .fai preparation).
"""

from __future__ import annotations

from typing import Any

import httpx

from genomics_mcp.archives._common.core import HttpFactory, SourceRuntime
from genomics_mcp.archives._common.handlers import DiscoverySource
from genomics_mcp.context import OperationContext
from genomics_mcp.registry import Registry, SourceInfo
from genomics_mcp.requests import SearchDatasetsRequest

PROVIDER = __name__


def register(registry: Registry, *, http_factory: HttpFactory | None = None) -> None:
    from genomics_mcp.catalogs.encode import TERMS_URL as ENCODE_TERMS
    from genomics_mcp.catalogs.encode import EncodeClient
    from genomics_mcp.catalogs.geo import TERMS_URL as NCBI_TERMS
    from genomics_mcp.catalogs.geo import GeoClient
    from genomics_mcp.catalogs.ncbi_datasets import NcbiDatasetsClient
    from genomics_mcp.catalogs.preparation import PREPARER_COMPONENT, NcbiGenomePreparer

    async def make_encode(
        http: httpx.AsyncClient, ctx: OperationContext, rt: SourceRuntime
    ) -> EncodeClient:
        return EncodeClient(http)

    async def encode_search(c: EncodeClient, req: SearchDatasetsRequest, n: int, t: float) -> Any:
        return await c.search_datasets(
            req.query,
            assembly=req.assembly,
            organism=req.organism,
            limit=n,
            cursor=req.cursor,
            timeout_s=t,
        )

    async def make_geo(
        http: httpx.AsyncClient, ctx: OperationContext, rt: SourceRuntime
    ) -> GeoClient:
        return GeoClient(http, api_key=rt.api_key(ctx))

    async def geo_search(c: GeoClient, req: SearchDatasetsRequest, n: int, t: float) -> Any:
        return await c.search_datasets(
            req.query, organism=req.organism, limit=n, cursor=req.cursor, timeout_s=t
        )

    async def make_ncbi(
        http: httpx.AsyncClient, ctx: OperationContext, rt: SourceRuntime
    ) -> NcbiDatasetsClient:
        return NcbiDatasetsClient(http, api_key=rt.api_key(ctx))

    rts = {
        n: SourceRuntime(n, http_factory=http_factory) for n in ("encode", "geo", "ncbi_datasets")
    }
    DiscoverySource(
        "encode",
        make_encode,
        rts["encode"],
        max_page=100,
        search=encode_search,
        search_filters=frozenset({"assembly", "organism"}),
    ).register(registry, PROVIDER)
    DiscoverySource(
        "geo",
        make_geo,
        rts["geo"],
        max_page=100,
        search=geo_search,
        search_filters=frozenset({"organism"}),
    ).register(registry, PROVIDER)
    DiscoverySource("ncbi_datasets", make_ncbi, rts["ncbi_datasets"], max_page=100).register(
        registry, PROVIDER
    )
    registry.provide(PREPARER_COMPONENT, NcbiGenomePreparer(rts["ncbi_datasets"], make_ncbi))

    ops = [
        "search_datasets",
        "describe_dataset",
        "list_files",
        "list_samples",
        "get_sample_metadata",
    ]
    registry.register_source(
        SourceInfo(
            "encode",
            "ENCODE portal",
            "catalog",
            "E7",
            homepage="https://www.encodeproject.org/",
            terms_url=ENCODE_TERMS,
            auth="none",
            operations=ops,
            notes=(
                "Public. Files carry ENCODE's href, the unsigned public S3 URL, per-file assembly, size and "
                "MD5; readiness is 'ready' only after a range/magic check. Search filters: assembly, organism."
            ),
        )
    )
    registry.register_source(
        SourceInfo(
            "geo",
            "NCBI Gene Expression Omnibus",
            "catalog",
            "E7",
            homepage="https://www.ncbi.nlm.nih.gov/geo/",
            terms_url=NCBI_TERMS,
            auth="optional_key",
            operations=ops,
            notes=(
                "Public. GSE series, GSM samples (characteristics verbatim), GPL platforms kept separate; "
                "supplementary files over HTTPS. Optional key: [sources.geo].api_key_env (E-utilities)."
            ),
        )
    )
    registry.register_source(
        SourceInfo(
            "ncbi_datasets",
            "NCBI Datasets (assemblies, sequences, annotation)",
            "catalog",
            "E7",
            homepage="https://www.ncbi.nlm.nih.gov/datasets/",
            terms_url=NCBI_TERMS,
            auth="optional_key",
            operations=ops,
            notes=(
                "Public. Versioned assembly reports; genome packages are ZIP files (download_required, never "
                "FASTA-ready) and need explicit preparation. list_samples returns only a BioSample the "
                "assembly report names."
            ),
        )
    )
