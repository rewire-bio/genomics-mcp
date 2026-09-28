"""Archive/catalogue and reference API responses through the real service and HTTP helpers.

Upstream is a counting mock transport serving recorded responses; the cache is the server's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import benchmark_cache as bench
import httpx
import pytest

from genomics_mcp import catalogs, evidence
from genomics_mcp.archives._common.errors import BudgetExceededError, InvalidInputError
from genomics_mcp.archives._common.http import SourceHttp as ArchiveHttp
from genomics_mcp.archives._common.http import SourcePolicy
from genomics_mcp.cache import MemoryCache, private_query
from genomics_mcp.config import CacheSettings, load_settings
from genomics_mcp.context import OperationContext
from genomics_mcp.models import FileRef, VariantSpec, Visibility
from genomics_mcp.public import Deadline, EgressContext
from genomics_mcp.references.http import RateLimiter, SourceFailure
from genomics_mcp.references.http import SourceHttp as ReferenceHttp
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import EffectiveLimits
from genomics_mcp.service import GenomicsService

FIXTURES = Path(__file__).parents[1] / "reference" / "fixtures"
ENCODE_FILE = "https://www.encodeproject.org/files/ENCFF792QDS/"
GNOMAD = "https://gnomad.broadinstitute.org/api"
JSON = {"content-type": "application/json"}


class Upstream:
    """Counting transport. `extra` routes override the recorded responses."""

    def __init__(self, extra: dict | None = None) -> None:
        self.extra = extra or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.extra.get(str(request.url.copy_with(query=None)))
        return route(request) if route else bench._recorded(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), trust_env=False)

    def count(self, prefix: str) -> int:
        return sum(str(r.url).startswith(prefix) for r in self.requests)


def service(tmp_path: Path, up: Upstream, **over) -> GenomicsService:
    reg = Registry()
    catalogs.register(reg, http_factory=up.client)
    evidence.register(reg, runtime=evidence.ReferenceRuntime(client=up.client()))
    s = load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path / "w")}, **over})
    return GenomicsService(s, reg, load_providers=False)


async def test_archive_response_is_reused_with_its_original_retrieval_time(tmp_path):
    up = Upstream()
    svc = service(tmp_path, up)
    args = {"source": "encode", "accession": "ENCFF792QDS"}
    first = await svc.call("list_files", args)
    before_second = datetime.now(UTC)
    second = await svc.call("list_files", args)
    assert up.count(ENCODE_FILE) == 1
    assert second.data == first.data
    t1, t2 = first.provenance[0].retrieved_at, second.provenance[0].retrieved_at
    assert t2 <= t1 < before_second  # the hit reports when upstream answered, not now
    assert not any(w.startswith("cache:") for w in first.warnings)
    assert any(w.startswith("cache: 1 upstream API response") for w in second.warnings)
    off = service(tmp_path, Upstream(), cache={"enabled": False})
    for _ in range(2):
        await off.call("list_files", args)
    assert off.registry.component("cache").metrics()["entries"] == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={**bench.ENCODE_FILE}, headers={"cache-control": "no-store"}),
        httpx.Response(200, json={**bench.ENCODE_FILE}, headers={"set-cookie": "s=1"}),
        httpx.Response(404, json={"status": "error"}),
        httpx.Response(200, content=b'{"accession": "ENCFF792Q', headers=JSON),
    ],
)
async def test_uncacheable_or_failed_archive_responses_are_refetched(tmp_path, response):
    up = Upstream({ENCODE_FILE: lambda r: response})
    svc = service(tmp_path, up)
    for _ in range(2):
        await svc.call("list_files", {"source": "encode", "accession": "ENCFF792QDS"})
    assert up.count(ENCODE_FILE) == 2


async def test_archive_hits_obey_current_host_policy_and_byte_budget():
    cache = MemoryCache(CacheSettings())
    up = Upstream({"https://api.example.org/x": lambda r: httpx.Response(200, json={"n": 1})})

    def http(hosts: set[str]) -> ArchiveHttp:
        h = ArchiveHttp(
            up.client(), SourcePolicy("demo", frozenset(hosts), min_interval_s=0, cacheable=True)
        )
        h.cache = cache
        return h

    assert (
        await http({"api.example.org"}).request("GET", "https://api.example.org/x")
    ).status == 200
    assert (
        await http({"api.example.org"}).request("GET", "https://api.example.org/x")
    ).status == 200
    assert len(up.requests) == 1
    with pytest.raises(InvalidInputError):  # allowlist changed: the hit is refused too
        await http({"other.example.org"}).request("GET", "https://api.example.org/x")
    with pytest.raises(BudgetExceededError):
        await http({"api.example.org"}).request("GET", "https://api.example.org/x", max_body=4)
    assert len(up.requests) == 2  # stored body is larger than this call's budget: refetched
    auth = http({"api.example.org"})
    for _ in range(2):
        await auth.request(
            "GET", "https://api.example.org/x", headers={"Authorization": "Bearer t"}
        )
    assert len(up.requests) == 4
    plain = ArchiveHttp(up.client(), SourcePolicy("demo", frozenset({"api.example.org"})))
    plain.cache = cache  # policy not marked cacheable (e.g. data/transfer hosts)
    for _ in range(2):
        await plain.request("GET", "https://api.example.org/x")
    assert len(up.requests) == 6


async def test_reference_lookup_is_reused_and_keeps_evidence_time(tmp_path):
    up = Upstream()
    svc = service(tmp_path, up)
    args = {"identifier": "FANCD1", "sources": ["hgnc"]}
    first = await svc.call("resolve_identifier", args)
    n = len(up.requests)
    second = await svc.call("resolve_identifier", args)
    assert n > 0 and len(up.requests) == n
    assert bench._scientific(second.data) == bench._scientific(first.data)
    (r1,), (r2,) = first.data["records"], second.data["records"]
    t1, t2 = r1["provenance"]["retrieved_at"], r2["provenance"]["retrieved_at"]
    assert t2 < t1  # the reused evidence keeps its original retrieval time
    assert len(second.provenance) == len(first.provenance) > 0
    for a, b in zip(first.provenance, second.provenance, strict=True):
        assert b.retrieved_at <= a.retrieved_at
    assert any(w.startswith("cache:") for w in second.warnings)


def graphql(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


async def test_read_only_graphql_queries_are_cached_but_errors_mutations_and_credentials_not():
    answers = iter(
        [graphql({"data": {"v": 1}}), graphql({"data": None, "errors": [{"message": "x"}]})]
        + [graphql({"data": {"v": 2}})] * 20
    )
    up = Upstream({GNOMAD: lambda r: next(answers)})
    cache = MemoryCache(CacheSettings())
    http = ReferenceHttp(
        up.client(), source="gnomad", limiter=RateLimiter(100, 1.0),
        allowed_hosts=["gnomad.broadinstitute.org"], cache=cache,
    )  # fmt: skip
    ct = {"Content-Type": "application/json"}

    async def post(query: str, n: int = 2, **kw) -> int:
        before = len(up.requests)
        for _ in range(n):
            await http.request("POST", GNOMAD, operation="t", headers=ct,
                               json_body={"query": query, "variables": {"id": query}}, **kw)  # fmt: skip
        return len(up.requests) - before

    assert await post("query A { v }", read_only=True) == 1
    assert await post("query B { v }", read_only=True) == 2  # first answer carried `errors`
    assert await post("query C { v }") == 2  # POST not declared read-only
    assert await post("mutation D { v }", read_only=True) == 2
    with private_query():
        assert await post("query E { v }", read_only=True) == 2
    with pytest.raises(SourceFailure, match="exceeds 5 bytes"):  # smaller budget: no hit
        await post("query A { v }", n=1, read_only=True, max_bytes=5)
    before = len(up.requests)
    for _ in range(2):
        await http.request("GET", GNOMAD, operation="t", params={"api_key": "secret"},
                           accept_status=(200,))  # fmt: skip
    assert len(up.requests) - before == 2
    gated = ReferenceHttp(
        up.client(), source="gnomad", limiter=RateLimiter(100, 1.0), cache=cache,
        allowed_hosts=["gnomad.broadinstitute.org"], gate=lambda s: False,
    )  # fmt: skip
    with pytest.raises(SourceFailure):
        await gated.request("POST", GNOMAD, operation="t", headers=ct, read_only=True,
                            json_body={"query": "query A { v }", "variables": {"id": "query A { v }"}})  # fmt: skip
    assert "secret" not in json.dumps(cache.metrics())


async def test_private_file_derived_lookups_are_never_cached(tmp_path):
    gnomad = json.loads((FIXTURES / "gnomad_variant_7-140753336-A-T.json").read_text())
    efetch = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    seq = "N" * 64 + "A" + "N" * 64
    up = Upstream({
        GNOMAD: lambda r: httpx.Response(200, json=gnomad),
        efetch: lambda r: httpx.Response(200, text=f">NC_000007.14:1-{len(seq)} t\n{seq}\n"),
    })  # fmt: skip
    svc = service(tmp_path, up)
    ctx = OperationContext(
        operation=Operation.INSPECT_LOCUS, settings=svc.settings,
        limits=EffectiveLimits.build(svc.settings.limits), deadline=Deadline(10.0),
        registry=svc.registry, http=svc.http, request_id="t",
    )  # fmt: skip
    facade = svc.registry.component("reference_evidence")
    spec = VariantSpec(assembly="GRCh38", contig="7", pos=140753336, ref="A", alt="T")
    private = FileRef(uri="/data/calls.vcf", visibility=Visibility.PRIVATE)
    for egress, expected in (
        (EgressContext.for_files([private], consent=True), 2),
        (EgressContext.public(), 1),
    ):
        before = up.count(GNOMAD)
        for _ in range(2):
            out = await facade.lookup_variant(ctx, spec, egress=egress, sources=["gnomad"])
            assert out.data["records"][0]["source"] == "gnomad"
        assert up.count(GNOMAD) - before == expected
