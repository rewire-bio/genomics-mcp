"""Archive/catalogue and reference API responses through the real service and HTTP helpers.

Upstream is a counting mock transport serving recorded responses; the cache is the server's.
"""

from __future__ import annotations

import json
import time
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
        httpx.Response(200, text="<html>maintenance</html>", headers={"content-type": "text/html"}),
        httpx.Response(
            200, json={**bench.ENCODE_FILE}, headers={"cache-control": "max-age=60", "age": "90"}
        ),
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


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("lookup_protein", {"protein": "P51587", "sources": ["uniprot"]}),  # one request
        ("resolve_identifier", {"identifier": "FANCD1", "sources": ["hgnc"]}),  # several
    ],
)
async def test_reference_lookup_is_reused_with_upstream_retrieval_time(tmp_path, op, args):
    up = Upstream()
    svc = service(tmp_path, up)
    first = await svc.call(op, args)
    n = len(up.requests)
    before_second = datetime.now(UTC)
    second = await svc.call(op, args)
    assert n > 0 and len(up.requests) == n
    assert bench._scientific(second.data) == bench._scientific(first.data)
    times = [
        [datetime.fromisoformat(r["provenance"]["retrieved_at"]) for r in res.data["records"]]
        for res in (first, second)
    ]
    for t1, t2 in zip(*times, strict=True):
        # Source-level time: the earliest upstream response of that source used by the call.
        # The warm call used cold-call responses only, so its time is from the cold call.
        assert t1 <= t2 < before_second
        if op == "lookup_protein":
            assert t1 == t2  # the same single response, reported identically
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


def ref_http(up: Upstream, client: httpx.AsyncClient | None = None, **kw) -> ReferenceHttp:
    return ReferenceHttp(
        client or up.client(), source="uniprot", limiter=RateLimiter(100, 1.0),
        allowed_hosts=["rest.uniprot.org"], cache=MemoryCache(CacheSettings()), **kw,
    )  # fmt: skip


UNIPROT = "https://rest.uniprot.org/uniprotkb/P51587.json"


async def test_client_credentials_and_learned_cookies_are_never_shared():
    up = Upstream()
    for client in (
        httpx.AsyncClient(transport=httpx.MockTransport(up), headers={"Authorization": "Bearer synthetic"}),
        httpx.AsyncClient(transport=httpx.MockTransport(up), auth=("user", "synthetic")),
    ):  # fmt: skip
        http = ref_http(up, client)
        before = len(up.requests)
        for _ in range(2):
            await http.request("GET", UNIPROT, operation="t")
        assert len(up.requests) - before == 2 and http.cache.metrics()["entries"] == 0

    # A long-lived client that is sent a cookie stops using the cache from then on.
    def set_cookie(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"n": 1}, headers={"set-cookie": "session=synthetic"})

    up = Upstream({"https://rest.uniprot.org/start": set_cookie})
    http = ref_http(up)
    await http.request("GET", "https://rest.uniprot.org/start", operation="t")
    for _ in range(2):
        await http.request("GET", UNIPROT, operation="t")
    assert up.count(UNIPROT) == 2 and http.cache.metrics()["entries"] == 0
    assert "Cookie" in up.requests[-1].headers


async def test_representation_headers_are_part_of_the_key():
    def by_language(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"lang": request.headers.get("accept-language")})

    up = Upstream({UNIPROT: by_language})
    http = ref_http(up)
    for lang in ("en", "fr", "en", "fr"):
        res = await http.request("GET", UNIPROT, operation="t", headers={"Accept-Language": lang})
        assert res.json() == {"lang": lang}
    assert up.count(UNIPROT) == 2


async def test_cache_hits_obey_the_current_deadline():
    up = Upstream()
    http = ref_http(up)
    await http.request("GET", UNIPROT, operation="t")
    with pytest.raises(SourceFailure) as exc:
        await http.request("GET", UNIPROT, operation="t", deadline=time.monotonic() - 1)
    assert exc.value.error.kind == "timeout" and up.count(UNIPROT) == 1


async def test_redirect_to_a_signed_url_is_not_stored():
    signed = "https://api.example.org/z?X-Amz-Signature=synthetic"
    up = Upstream({
        "https://api.example.org/y": lambda r: httpx.Response(302, headers={"location": signed}),
        "https://api.example.org/z": lambda r: httpx.Response(200, json={"n": 1}),
    })  # fmt: skip
    h = ArchiveHttp(
        up.client(),
        SourcePolicy("demo", frozenset({"api.example.org"}), min_interval_s=0, cacheable=True),
    )
    h.cache = MemoryCache(CacheSettings())
    for _ in range(2):
        assert (await h.request("GET", "https://api.example.org/y")).json() == {"n": 1}
    assert len(up.requests) == 4 and h.cache.metrics()["entries"] == 0


def cookie_redirect(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/public":
        return httpx.Response(302, headers={"location": "/data", "set-cookie": "s=synthetic"})
    return httpx.Response(200, json={"cookie_sent": "cookie" in request.headers})


def cookie_retry(request: httpx.Request) -> httpx.Response:
    if "cookie" not in request.headers:
        return httpx.Response(429, headers={"set-cookie": "s=synthetic", "retry-after": "0"})
    return httpx.Response(200, json={"cookie_sent": True})


@pytest.mark.parametrize(
    ("kind", "handler"),  # the reference helper never follows redirects
    [("archive", cookie_redirect), ("archive", cookie_retry), ("reference", cookie_retry)],
)
async def test_cookie_learned_during_the_exchange_is_never_shared(kind, handler):
    cache = MemoryCache(CacheSettings())
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    url = (
        "https://api.example.org/public"
        if handler is cookie_redirect
        else "https://api.example.org/data"
    )
    for _ in range(2):  # a fresh client per call, as the archive handlers use
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            if kind == "archive":
                h = ArchiveHttp(
                    client,
                    SourcePolicy(
                        "demo", frozenset({"api.example.org"}), min_interval_s=0, cacheable=True
                    ),
                )
                h.cache = cache
                assert (await h.request("GET", url)).json()["cookie_sent"] is True
            else:
                r = ReferenceHttp(client, source="demo", limiter=RateLimiter(100, 1.0),
                                  allowed_hosts=["api.example.org"], cache=cache)  # fmt: skip
                assert (await r.request("GET", url, operation="t")).json()["cookie_sent"] is True
    assert cache.metrics()["entries"] == 0 and len(seen) == 4
