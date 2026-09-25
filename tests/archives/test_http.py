"""SourceHttp: status mapping, retries, body bounds, redirects and budgets."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from archive_helpers import Router, json_response

from genomics_mcp.archives._common.errors import (
    BudgetExceededError,
    InvalidInputError,
    NotFoundError,
    UnauthorizedError,
    UpstreamError,
)
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy, make_client

A = "https://a.example.org"
B = "https://b.example.org"


def http(router: Router, hosts=("a.example.org",), **kw) -> SourceHttp:
    policy = SourcePolicy("test", frozenset(hosts), min_interval_s=0, max_retry_after_s=0.05, **kw)
    return SourceHttp(router.client(), policy)


async def test_429_retry_after_then_success(router: Router) -> None:
    calls = iter([httpx.Response(429, headers={"retry-after": "0"}), json_response({"ok": 1})])
    router.add("GET", f"{A}/x", lambda r: next(calls))
    data, _ = await http(router).get_json(f"{A}/x")
    assert data == {"ok": 1} and len(router.requests) == 2


async def test_persistent_500_is_retryable_upstream_error(router: Router) -> None:
    router.add("GET", f"{A}/x", httpx.Response(500, text="boom"))
    with pytest.raises(UpstreamError) as ei:
        await http(router, max_retries=1).get_json(f"{A}/x")
    assert ei.value.retryable and ei.value.details["http_status"] == 500
    assert len(router.requests) == 2


@pytest.mark.parametrize(
    ("status", "exc", "source_error"),
    [
        (401, UnauthorizedError, "InvalidAuthentication"),
        (403, UnauthorizedError, "PermissionDenied"),
        (404, NotFoundError, "NotFound"),
    ],
)
async def test_status_mapping_keeps_source_distinction(
    router: Router, status, exc, source_error
) -> None:
    router.add(
        "GET", f"{A}/x", json_response({"htsget": {"error": source_error, "message": "m"}}, status)
    )
    with pytest.raises(exc) as ei:
        await http(router).get_json(f"{A}/x")
    assert ei.value.details["http_status"] == status
    assert ei.value.details["source_error"] == source_error
    assert not isinstance(ei.value, NotFoundError) or status == 404


async def test_redirect_following_injected_client_cannot_bypass_allowlist(router: Router) -> None:
    router.add(
        "GET", f"{A}/x", httpx.Response(302, headers={"location": "https://unapproved.invalid/p"})
    )
    router.add("GET", "https://unapproved.invalid/p", json_response({"leak": True}))
    policy = SourcePolicy("test", frozenset({"a.example.org"}), min_interval_s=0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(router), follow_redirects=True)
    with pytest.raises(InvalidInputError):
        await SourceHttp(client, policy).get_json(f"{A}/x")
    assert not router.hits("https://unapproved.invalid")


async def test_redirect_forbidden_on_download_path(router: Router, tmp_path: Path) -> None:
    router.add(
        "GET", f"{A}/f", httpx.Response(307, headers={"location": "https://unapproved.invalid/f"})
    )
    with pytest.raises(InvalidInputError):
        await http(router).download(f"{A}/f", tmp_path / "f", budget_bytes=100)
    assert not router.hits("https://unapproved.invalid") and not list(tmp_path.iterdir())  # noqa: ASYNC240 - small local test file


async def test_allowed_cross_host_redirect_drops_authorization(router: Router) -> None:
    router.add("GET", f"{A}/x", httpx.Response(302, headers={"location": f"{B}/y"}))
    router.add("GET", f"{B}/y", json_response({"ok": 1}))
    await http(router, hosts=("a.example.org", "b.example.org")).get_json(
        f"{A}/x", headers={"Authorization": "Bearer synthetic-token-value"}
    )
    assert "authorization" in router.requests[0].headers
    assert "authorization" not in router.requests[1].headers


async def test_zero_body_budget_makes_no_request(router: Router) -> None:
    router.add("GET", f"{A}/x", json_response({}))
    with pytest.raises(BudgetExceededError):
        await http(router).request("GET", f"{A}/x", max_body=0)
    assert router.requests == []


async def test_body_over_cap_is_budget_error(router: Router) -> None:
    router.add("GET", f"{A}/x", httpx.Response(200, content=b"x" * 2048))
    with pytest.raises(BudgetExceededError):
        await http(router).request("GET", f"{A}/x", max_body=1024)


async def test_non_https_and_metadata_hosts_rejected(router: Router) -> None:
    h = http(router, hosts=("a.example.org", "169.254.169.254"))
    for url in ("http://a.example.org/x", "https://169.254.169.254/latest/meta-data/"):
        with pytest.raises(InvalidInputError):
            await h.get_json(url)
    assert router.requests == []


async def test_download_budget_enforced_and_partial_removed(router: Router, tmp_path: Path) -> None:
    router.add("GET", f"{A}/f", httpx.Response(200, content=b"y" * 5000))
    with pytest.raises(BudgetExceededError):
        await http(router).download(f"{A}/f", tmp_path / "f", budget_bytes=1000)
    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240 - small local test file


async def test_make_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    async with make_client() as c:
        assert c._trust_env is False  # no proxies, no .netrc
        assert c.follow_redirects is False
