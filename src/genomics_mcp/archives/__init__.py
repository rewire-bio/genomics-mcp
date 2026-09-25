"""Archive sources (E6): EGA and ENA.

`register(registry)` adds discovery handlers for `ega` and `ena`, the region-only `ega://`
resolver, and the `transfer_backend:ega` component for explicit whole-file transfers.
"""

from __future__ import annotations

import httpx

from genomics_mcp.archives._common.core import HttpFactory, SourceRuntime
from genomics_mcp.archives._common.handlers import DiscoverySource
from genomics_mcp.context import OperationContext
from genomics_mcp.registry import Registry, SourceInfo

PROVIDER = __name__


def _ega_parts(http_factory: HttpFactory | None):
    from genomics_mcp.archives.ega.access import EgaAccess, EgaAccessConfig
    from genomics_mcp.archives.ega.client import TERMS_URL, EgaClient

    runtime = SourceRuntime("ega", http_factory=http_factory)
    access = EgaAccess()

    async def make(http: httpx.AsyncClient, ctx: OperationContext, rt: SourceRuntime) -> EgaClient:
        cfg = EgaAccessConfig.from_settings(ctx.settings)
        # Discovery uses credentials only when configured (authorised listings add file names
        # and index links); anonymous calls use the public metadata API.
        return EgaClient(http, auth=await access.auth(cfg, http))

    return runtime, access, make, TERMS_URL


def register(registry: Registry, *, http_factory: HttpFactory | None = None) -> None:
    from genomics_mcp.archives.ega.integration import (
        TRANSFER_BACKEND_COMPONENT,
        EgaResolver,
        EgaTransferBackend,
    )
    from genomics_mcp.archives.ena.client import TERMS_URL as ENA_TERMS
    from genomics_mcp.archives.ena.client import EnaClient

    ega_rt, access, make_ega, ega_terms = _ega_parts(http_factory)
    DiscoverySource("ega", make_ega, ega_rt).register(registry, PROVIDER)

    async def make_ena(
        http: httpx.AsyncClient, ctx: OperationContext, rt: SourceRuntime
    ) -> EnaClient:
        return EnaClient(http)

    ena_rt = SourceRuntime("ena", http_factory=http_factory)
    DiscoverySource("ena", make_ena, ena_rt).register(registry, PROVIDER)

    registry.register_resolver("ega", EgaResolver(ega_rt, access), provider=PROVIDER)
    registry.provide(TRANSFER_BACKEND_COMPONENT, EgaTransferBackend(ega_rt, access))

    registry.register_source(
        SourceInfo(
            "ega",
            "European Genome-phenome Archive",
            "archive",
            "E6",
            homepage="https://ega-archive.org/",
            terms_url=ega_terms,
            auth="account",
            notes=(
                "Public metadata is anonymous (no free-text search: query by EGA accession). Controlled "
                "files need explicit credentials: [sources.ega].api_key_env (personal token), "
                "GENOMICS_MCP_EGA_USERNAME/GENOMICS_MCP_EGA_PASSWORD, or GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1 "
                "for EGA's documented public test data. ega:// files serve bounded htsget regions only; "
                "whole files go through the transfer backend with an explicit budget."
            ),
            operations=[
                "search_datasets",
                "describe_dataset",
                "list_files",
                "list_samples",
                "get_sample_metadata",
            ],
        )
    )
    registry.register_source(
        SourceInfo(
            "ena",
            "European Nucleotide Archive (incl. SRA/DDBJ accessions)",
            "archive",
            "E6",
            homepage="https://www.ebi.ac.uk/ena/browser/",
            terms_url=ENA_TERMS,
            auth="none",
            notes=(
                "Public. Files are listed with ENA's sizes/MD5s and HTTPS URLs where ENA serves them; "
                "FASTQ is not locus-queryable. Sequence accessions list a versioned FASTA record."
            ),
            operations=[
                "search_datasets",
                "describe_dataset",
                "list_files",
                "list_samples",
                "get_sample_metadata",
            ],
        )
    )
