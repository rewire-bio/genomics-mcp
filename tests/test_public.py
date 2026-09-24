import asyncio
import time

import httpx
import pytest

from genomics_mcp.config import load_settings
from genomics_mcp.errors import ErrorCode, GenomicsError
from genomics_mcp.models import FileRef, Visibility
from genomics_mcp.public import Deadline, EgressContext, PublicHttpClient, SourcePolicy

POLICY = SourcePolicy(
    name="demo", base_url="https://api.example.org/v1", backoff_s=0.01, terms_url="https://t"
)


def client(handler, **settings_overrides) -> PublicHttpClient:
    settings = load_settings(env={}, overrides=settings_overrides or None)
    return PublicHttpClient(settings, transport=httpx.MockTransport(handler))


async def get(c, path="records", *, policy=POLICY, egress=None, deadline=5.0, **kw):
    return await c.get_json(
        policy, path, deadline=Deadline(deadline), egress=egress or EgressContext.public(), **kw
    )


async def test_json_with_redacted_provenance():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["user-agent"].startswith("rewire-genomics-mcp/")
        return httpx.Response(200, json={"ok": True})

    async with client(handler) as c:
        data, prov = await get(c, params={"q": "BRCA1", "api_key": "k-123456"}, record_id="r1")
    assert data == {"ok": True}
    assert prov.source == "demo" and prov.source_record_id == "r1"
    assert "q=BRCA1" in prov.url and "k-123456" not in prov.url
    assert prov.terms_url == "https://t"


async def test_not_found_is_not_retried():
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with client(handler) as c:
        with pytest.raises(GenomicsError) as exc:
            await get(c)
    assert exc.value.info.code is ErrorCode.NOT_FOUND
    assert calls == 1


@pytest.mark.parametrize(("status", "code"), [(401, "unauthorized"), (403, "unauthorized")])
async def test_auth_failures_are_distinct(status, code):
    async with client(lambda r: httpx.Response(status)) as c:
        with pytest.raises(GenomicsError) as exc:
            await get(c)
    assert exc.value.info.code == code


async def test_transient_errors_retried_for_get_only():
    calls = {"GET": 0, "POST": 0}

    def handler(req):
        calls[req.method] += 1
        # GET: 503 then 200. POST: 503, 503 (first retry), then 200.
        if calls[req.method] == 1 or (req.method == "POST" and calls["POST"] == 2):
            return httpx.Response(503)
        return httpx.Response(200, json={"n": calls[req.method]})

    async with client(handler) as c:
        data, _ = await get(c)
        assert data == {"n": 2}
        with pytest.raises(GenomicsError) as exc:
            await c.request(
                POLICY, "POST", "graphql", deadline=Deadline(5), egress=EgressContext.public()
            )
        assert exc.value.info.code is ErrorCode.UPSTREAM_ERROR and exc.value.info.retryable
        # An explicitly idempotent POST (e.g. a GraphQL read) may be retried.
        resp = await c.request(
            POLICY,
            "POST",
            "graphql",
            deadline=Deadline(5),
            egress=EgressContext.public(),
            idempotent=True,
        )
        assert resp.status_code == 200
    assert calls["POST"] == 3


async def test_response_size_is_bounded():
    big = b"x" * 5000
    async with client(lambda r: httpx.Response(200, content=big)) as c:
        with pytest.raises(GenomicsError) as exc:
            await c.request(
                POLICY,
                "GET",
                "x",
                deadline=Deadline(5),
                egress=EgressContext.public(),
                max_response_bytes=1000,
            )
    assert exc.value.info.code is ErrorCode.BUDGET_EXCEEDED


async def test_foreign_hosts_are_refused():
    async with client(lambda r: httpx.Response(200, json={})) as c:
        with pytest.raises(GenomicsError) as exc:
            await get(c, "https://evil.example.com/x")
    assert exc.value.info.code is ErrorCode.INVALID_INPUT


async def test_private_derived_queries_need_consent():
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    private = FileRef(uri="/data/sample.vcf.gz")
    public = FileRef(uri="https://h/x.vcf.gz", visibility=Visibility.PUBLIC)
    async with client(handler) as c:
        with pytest.raises(GenomicsError) as exc:
            await get(c, egress=EgressContext.for_files([public, private], consent=False))
        assert exc.value.info.code is ErrorCode.CONSENT_REQUIRED
        assert calls == 0
        await get(c, egress=EgressContext.for_files([private], consent=True))
        await get(c, egress=EgressContext.for_files([public], consent=False))
    assert calls == 2


async def test_deadline_is_enforced():
    async def handler(req):
        await asyncio.sleep(2)
        return httpx.Response(200, json={})

    async with client(handler) as c:
        started = time.monotonic()
        with pytest.raises(GenomicsError) as exc:
            await get(c, deadline=0.2)
    assert exc.value.info.code is ErrorCode.TIMEOUT
    assert time.monotonic() - started < 1.5


async def test_rate_limit_spaces_requests():
    policy = SourcePolicy(name="slow", base_url="https://api.example.org", requests_per_minute=600)
    async with client(lambda r: httpx.Response(200, json={})) as c:
        started = time.monotonic()
        for _ in range(3):
            await get(c, policy=policy)
    assert time.monotonic() - started >= 0.18


async def test_concurrency_limit():
    active = peak = 0

    async def handler(req):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return httpx.Response(200, json={})

    policy = SourcePolicy(name="one", base_url="https://api.example.org", max_concurrency=1)
    async with client(handler) as c:
        await asyncio.gather(*(get(c, policy=policy) for _ in range(4)))
    assert peak == 1


async def test_config_can_disable_source_and_only_slow_it_down():
    async with client(
        lambda r: httpx.Response(200, json={}),
        sources={"demo": {"enabled": False}, "slow": {"requests_per_minute": 1000}},
    ) as c:
        with pytest.raises(GenomicsError) as exc:
            await get(c)
        assert exc.value.info.code is ErrorCode.UNSUPPORTED
        p = c.policy(
            SourcePolicy(name="slow", base_url="https://api.example.org", requests_per_minute=10)
        )
        assert p.requests_per_minute == 10
