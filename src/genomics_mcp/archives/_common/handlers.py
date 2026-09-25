"""Discovery handlers shared by the archive (E6) and catalog (E7) providers.

One `DiscoverySource` per source name builds that source's client for the current call and
exposes the five discovery operations. Search filters (`assembly`, `organism`) are applied only
where the source supports them; otherwise the call fails with `unsupported` rather than
returning unfiltered results.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from genomics_mcp.archives._common.core import (
    SourceRuntime,
    page_limit,
    page_output,
    share_limiters,
    to_core,
    to_core_error,
)
from genomics_mcp.archives._common.errors import SourceError
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import UnsupportedError, redact_obj
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.requests import (
    DescribeDatasetRequest,
    GetSampleMetadataRequest,
    ListFilesRequest,
    ListSamplesRequest,
    SearchDatasetsRequest,
)
from genomics_mcp.result import OperationOutput

ClientFactory = Callable[[httpx.AsyncClient, OperationContext, SourceRuntime], Awaitable[Any]]
SearchFn = Callable[[Any, SearchDatasetsRequest, int, float], Awaitable[Any]]


@dataclass
class DiscoverySource:
    name: str
    make_client: ClientFactory
    runtime: SourceRuntime
    max_page: int = 1000
    search: SearchFn | None = None
    """Custom search call (for source-specific filters); default passes query/limit/cursor."""
    search_filters: frozenset[str] = frozenset()
    unsupported: dict[Operation, str] = field(default_factory=dict)

    @asynccontextmanager
    async def client(self, ctx: OperationContext) -> AsyncIterator[Any]:
        async with self.runtime.client() as http:
            c = await self.make_client(http, ctx, self.runtime)
            share_limiters(
                self.runtime, ctx, *[v for v in vars(c).values() if hasattr(v, "limiter")]
            )
            yield c

    async def _call(self, ctx: OperationContext, fn: Callable[[Any, float], Awaitable[Any]]) -> Any:
        # Client construction reads source configuration and may authenticate or fetch public
        # configuration; its SourceErrors are converted like the operation's own.
        try:
            async with self.client(ctx) as c:
                return await fn(c, self.runtime.timeout(ctx))
        except SourceError as exc:
            raise to_core_error(exc) from None

    def _check_supported(self, op: Operation) -> None:
        if op in self.unsupported:
            raise UnsupportedError(self.unsupported[op], source=self.name)

    # -- operations -------------------------------------------------------------------
    async def search_datasets(
        self, req: SearchDatasetsRequest, ctx: OperationContext
    ) -> OperationOutput:
        self._check_supported(Operation.SEARCH_DATASETS)
        for name in ("assembly", "organism"):
            if getattr(req, name) and name not in self.search_filters:
                raise UnsupportedError(
                    f"{self.name} search cannot filter by {name}",
                    source=self.name,
                    hint="omit the filter or include it in the query text",
                )
        n = page_limit(req, ctx, self.max_page)
        if self.search is not None:
            page = await self._call(ctx, lambda c, t: self.search(c, req, n, t))
        else:
            page = await self._call(
                ctx,
                lambda c, t: c.search_datasets(req.query, limit=n, cursor=req.cursor, timeout_s=t),
            )
        return page_output(page)

    async def describe_dataset(
        self, req: DescribeDatasetRequest, ctx: OperationContext
    ) -> OperationOutput:
        self._check_supported(Operation.DESCRIBE_DATASET)
        d = await self._call(ctx, lambda c, t: c.describe_dataset(req.accession, timeout_s=t))
        data = {
            "dataset": to_core(d.dataset).model_dump(mode="json"),
            "studies": [to_core(s).model_dump(mode="json") for s in d.studies],
            "related": redact_obj(_jsonable(d.related)),
        }
        return OperationOutput(
            data=data, warnings=list(d.warnings), provenance=[to_core(p) for p in d.provenance]
        )

    async def list_files(self, req: ListFilesRequest, ctx: OperationContext) -> OperationOutput:
        self._check_supported(Operation.LIST_FILES)
        n = page_limit(req, ctx, self.max_page)
        page = await self._call(
            ctx, lambda c, t: c.list_files(req.accession, limit=n, cursor=req.cursor, timeout_s=t)
        )
        wanted = {f.value for f in req.formats} if req.formats else None
        filt = (lambda f: f.format is not None and f.format.value in wanted) if wanted else None
        return page_output(page, filter_fn=filt)

    async def list_samples(self, req: ListSamplesRequest, ctx: OperationContext) -> OperationOutput:
        self._check_supported(Operation.LIST_SAMPLES)
        n = page_limit(req, ctx, self.max_page)
        page = await self._call(
            ctx, lambda c, t: c.list_samples(req.accession, limit=n, cursor=req.cursor, timeout_s=t)
        )
        return page_output(page)

    async def get_sample_metadata(
        self, req: GetSampleMetadataRequest, ctx: OperationContext
    ) -> OperationOutput:
        self._check_supported(Operation.GET_SAMPLE_METADATA)
        s = await self._call(ctx, lambda c, t: c.get_sample_metadata(req.accession, timeout_s=t))
        return OperationOutput(
            data=to_core(s).model_dump(mode="json"), provenance=[to_core(p) for p in s.provenance]
        )

    def register(self, registry: Registry, provider: str) -> None:
        ops = {
            Operation.SEARCH_DATASETS: self.search_datasets,
            Operation.DESCRIBE_DATASET: self.describe_dataset,
            Operation.LIST_FILES: self.list_files,
            Operation.LIST_SAMPLES: self.list_samples,
            Operation.GET_SAMPLE_METADATA: self.get_sample_metadata,
        }
        for op, fn in ops.items():
            if op not in self.unsupported:
                registry.register(
                    op, self.name, fn, provider=provider, description=f"{op.value} via {self.name}"
                )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value
