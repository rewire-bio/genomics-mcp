"""Rate limits, deadlines, retries and error classification."""

from __future__ import annotations

import httpx
import pytest

from genomics_mcp.references.http import (
    RateBudgetExceeded,
    RateLimiter,
    SourceFailure,
    SourceHttp,
    redact_text,
    redact_url,
)

from .conftest import FakeClock, Router, html_500

pytestmark = pytest.mark.asyncio


def make_http(
    router: Router, clock: FakeClock, *, rate: int = 100, per: float = 1.0, retries: int = 2
) -> SourceHttp:
    client = httpx.AsyncClient(transport=httpx.MockTransport(router.handle))
    return SourceHttp(
        client,
        source="test",
        limiter=RateLimiter(rate, per, clock=clock, sleep=clock.sleep),
        timeout=5.0,
        max_retries=retries,
        clock=clock,
        sleep=clock.sleep,
    )


async def test_gnomad_style_limit_spaces_requests(clock: FakeClock) -> None:
    limiter = RateLimiter(10, 60.0, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        await limiter.acquire()
    assert clock.sleeps == []
    await limiter.acquire()
    assert clock.sleeps == [60.0]


async def test_limiter_fails_fast_when_wait_exceeds_deadline(clock: FakeClock) -> None:
    limiter = RateLimiter(3, 1.0, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await limiter.acquire()
    with pytest.raises(RateBudgetExceeded):
        await limiter.acquire(deadline=clock.now + 0.5)
    await limiter.acquire(deadline=clock.now + 2.0)
    assert clock.sleeps == [1.0]


async def test_429_is_retried_after_retry_after(router: Router, clock: FakeClock) -> None:
    router.add(
        "GET",
        r"example\.org/x",
        [
            httpx.Response(429, json={"error": "slow down"}, headers={"retry-after": "2"}),
            httpx.Response(200, json={"ok": True}),
        ],
    )
    http = make_http(router, clock)
    assert await http.get_json("https://example.org/x", operation="x") == {"ok": True}
    assert clock.sleeps == [2.0]
    assert len(router.calls) == 2


async def test_429_exhausting_retries_is_rate_limited(router: Router, clock: FakeClock) -> None:
    router.add("GET", r"example\.org/x", httpx.Response(429, json={"error": "slow down"}))
    http = make_http(router, clock, retries=1)
    with pytest.raises(SourceFailure) as exc:
        await http.get_json("https://example.org/x", operation="x")
    assert exc.value.error.kind == "rate_limited"
    assert exc.value.error.retryable is True
    assert len(router.calls) == 2


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "unauthorized"), (403, "forbidden"), (404, "not_found"), (400, "invalid_input")],
)
async def test_client_errors_are_typed_and_not_retried(
    router: Router, clock: FakeClock, status: int, kind: str
) -> None:
    router.add("GET", r"example\.org/x", httpx.Response(status, json={"error": "nope"}))
    http = make_http(router, clock)
    with pytest.raises(SourceFailure) as exc:
        await http.get_json("https://example.org/x", operation="x")
    assert exc.value.error.kind == kind
    assert exc.value.error.status_code == status
    assert len(router.calls) == 1


async def test_timeout_is_retried_then_reported(router: Router, clock: FakeClock) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    router.add("GET", r"example\.org/x", timeout)
    http = make_http(router, clock, retries=2)
    with pytest.raises(SourceFailure) as exc:
        await http.get_json("https://example.org/x", operation="x")
    assert exc.value.error.kind == "timeout"
    assert len(router.calls) == 3


async def test_html_500_body_is_not_echoed(router: Router, clock: FakeClock) -> None:
    router.add("GET", r"example\.org/x", html_500())
    http = make_http(router, clock, retries=0)
    with pytest.raises(SourceFailure) as exc:
        await http.get_json("https://example.org/x", operation="x")
    err = exc.value.error
    assert err.kind == "upstream" and err.status_code == 500
    assert err.message == "HTTP 500 with non-JSON body (text/html)"


async def test_errors_never_include_query_strings_or_keys(router: Router, clock: FakeClock) -> None:
    router.add(
        "GET", r"example\.org/x", httpx.Response(403, json={"error": "bad api_key=SECRET123"})
    )
    http = make_http(router, clock)
    with pytest.raises(SourceFailure) as exc:
        await http.get_json(
            "https://example.org/x", operation="x", params={"api_key": "SECRET123", "id": "1"}
        )
    err = exc.value.error
    assert err.url == "https://example.org/x"
    assert "SECRET123" not in err.model_dump_json()


async def test_deadline_bounds_retries(router: Router, clock: FakeClock) -> None:
    router.add("GET", r"example\.org/x", httpx.Response(503, json={"error": "busy"}))
    http = make_http(router, clock, retries=5)
    with pytest.raises(SourceFailure) as exc:
        await http.get_json("https://example.org/x", operation="x", deadline=clock.now + 1.0)
    assert exc.value.error.kind == "upstream"
    assert len(router.calls) <= 2


async def test_redaction_helpers() -> None:
    assert redact_url("https://h.org/p?api_key=abc&x=1") == "https://h.org/p"
    assert (
        redact_text("token=abc&x=1 X-Amz-Signature=zz")
        == "token=REDACTED&x=1 X-Amz-Signature=REDACTED"
    )
