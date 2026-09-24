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


# ---------------------------------------------------------------- redirects / boundary

SRC = SourcePolicy(
    name="src",
    base_url="https://source.test/api",
    allowed_hosts=frozenset({"mirror.test"}),
    backoff_s=0.001,
    default_headers={"X-Api-Key": "source-key-123456"},
)


def recorder(routes):
    """MockTransport handler that records every request actually sent."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return routes(req)

    return handler, seen


async def call(c, policy=SRC, path="data", method="GET", **kw):
    return await c.request(
        policy, method, path, deadline=Deadline(5), egress=EgressContext.public(), **kw
    )


async def test_redirect_to_unlisted_host_is_refused_before_sending():
    handler, seen = recorder(
        lambda r: (
            httpx.Response(302, headers={"Location": "https://other.test/data"})
            if r.url.host == "source.test"
            else httpx.Response(200, json={"leak": True})
        )
    )
    async with client(handler) as c:
        with pytest.raises(GenomicsError) as exc:
            await call(c)
    assert exc.value.info.code is ErrorCode.INVALID_INPUT
    assert [r.url.host for r in seen] == ["source.test"]


async def test_same_origin_and_mirror_redirects_are_followed_without_leaking_headers():
    def routes(r):
        if r.url.path == "/api/data":
            return httpx.Response(301, headers={"Location": "/api/moved"})
        if r.url.path == "/api/moved":
            return httpx.Response(307, headers={"Location": "https://mirror.test/copy"})
        return httpx.Response(200, json={"ok": True})

    handler, seen = recorder(routes)
    async with client(handler) as c:
        resp = await call(c, headers={"Authorization": "Bearer caller-token-123456"})
    assert resp.json() == {"ok": True}
    assert [str(r.url) for r in seen] == [
        "https://source.test/api/data",
        "https://source.test/api/moved",
        "https://mirror.test/copy",
    ]
    # Same origin keeps source headers; another origin gets none of them.
    assert seen[1].headers["x-api-key"] == "source-key-123456"
    assert "authorization" in seen[1].headers
    assert "x-api-key" not in seen[2].headers and "authorization" not in seen[2].headers
    assert seen[2].headers["user-agent"].startswith("rewire-genomics-mcp/")
    assert resp.provenance.url == "https://mirror.test/copy"


async def test_redirect_method_semantics():
    def routes(r):
        if r.url.path == "/api/a":
            return httpx.Response(307, headers={"Location": "/api/b"})
        if r.url.path == "/api/c":
            return httpx.Response(302, headers={"Location": "/api/b"})
        return httpx.Response(200, json={"method": r.method, "body": r.content.decode()})

    handler, _ = recorder(routes)
    async with client(handler) as c:
        kept = await call(c, path="a", method="POST", json_body={"q": 1}, idempotent=True)
        assert kept.json() == {"method": "POST", "body": '{"q":1}'}
        changed = await call(c, path="c", method="POST", json_body={"q": 1}, idempotent=True)
        assert changed.json() == {"method": "GET", "body": ""}


async def test_https_to_http_downgrade_is_refused():
    policy = SourcePolicy(
        name="dg", base_url="https://source.test", allowed_hosts=frozenset({"plain.test"})
    )
    handler, seen = recorder(
        lambda r: httpx.Response(302, headers={"Location": "http://plain.test/x"})
    )
    async with client(handler) as c:
        with pytest.raises(GenomicsError) as exc:
            await call(c, policy=policy)
    assert "https to http" in exc.value.info.message
    assert len(seen) == 1


async def test_redirect_loop_is_bounded():
    handler, seen = recorder(lambda r: httpx.Response(302, headers={"Location": "/api/data"}))
    async with client(handler) as c:
        with pytest.raises(GenomicsError) as exc:
            await call(c)
    assert exc.value.info.code is ErrorCode.UPSTREAM_ERROR
    assert len(seen) == 6  # original + 5 redirects


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://169.254.170.2/v2/credentials/x",
        "http://[fd00:ec2::254]/latest/api/token",
        "http://instance-data/latest/",
        "http://instance-data.ec2.internal./latest/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
async def test_metadata_destinations_never_contacted(target):
    host = httpx.URL(target).host
    # Even an explicit allowlist entry or http base URL cannot reach metadata services.
    direct = SourcePolicy(name="md", base_url=target, allowed_hosts=frozenset({host}))
    via_redirect = SourcePolicy(
        name="rd", base_url="https://source.test", allowed_hosts=frozenset({host})
    )
    handler, seen = recorder(lambda r: httpx.Response(302, headers={"Location": target}))
    async with client(handler) as c:
        with pytest.raises(GenomicsError) as exc:
            await call(c, policy=direct, path="")
        assert exc.value.info.code is ErrorCode.UNAUTHORIZED
        assert seen == []
        with pytest.raises(GenomicsError) as exc:
            await call(c, policy=via_redirect)
        assert exc.value.info.code is ErrorCode.UNAUTHORIZED
    assert [r.url.host for r in seen] == ["source.test"]


async def test_boundary_errors_never_contain_signed_url_values():
    signed = (
        "https://other.test/f.bam?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE&X-Amz-Signature=SECRETSIG"
    )
    handler, _ = recorder(lambda r: httpx.Response(302, headers={"Location": signed}))
    async with client(handler) as c:
        with pytest.raises(GenomicsError) as exc:
            await call(c)
    dumped = exc.value.info.model_dump_json()
    assert "SECRETSIG" not in dumped and "AKIA" not in dumped
    assert "other.test" in dumped


async def test_explicit_loopback_fixture_allowed():
    policy = SourcePolicy(name="fixture", base_url="http://127.0.0.1:8081")
    handler, seen = recorder(lambda r: httpx.Response(200, json={"ok": 1}))
    async with client(handler) as c:
        assert (await call(c, policy=policy)).json() == {"ok": 1}
    assert seen[0].url.host == "127.0.0.1"


# ---------------------------------------------------------------- Retry-After


@pytest.fixture
def sleeps(monkeypatch):
    import genomics_mcp.public as public

    recorded: list[float] = []

    async def fake_sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr(public, "_sleep", fake_sleep)
    return recorded


def throttled_then_ok(retry_after: str):
    calls = 0

    def handler(r):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"calls": calls})

    return handler


async def test_retry_after_seconds_is_honoured(sleeps):
    async with client(throttled_then_ok("1")) as c:
        resp = await call(c)
    assert resp.json() == {"calls": 2}
    assert sleeps == [1.0]  # not the 0.001 s backoff


async def test_retry_after_http_date_is_honoured(sleeps):
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = format_datetime(datetime.now(UTC) + timedelta(seconds=3), usegmt=True)
    async with client(throttled_then_ok(when)) as c:
        await call(c)
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= 3.0


async def test_retry_after_beyond_deadline_fails_without_waiting(sleeps):
    async with client(throttled_then_ok("120")) as c:
        with pytest.raises(GenomicsError) as exc:
            await call(c)
    assert exc.value.info.code is ErrorCode.UPSTREAM_ERROR
    assert sleeps == []
