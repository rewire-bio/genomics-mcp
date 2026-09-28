"""MemoryCache bounds and the request/response policy shared by every cached path."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from genomics_mcp.cache import (
    Identity,
    MemoryCache,
    lifetime,
    object_identity,
    private_query,
    request_key,
    storable,
)
from genomics_mcp.config import CacheSettings, load_settings

DAY = "Mon, 28 Sep 2026"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def cache(**kw) -> tuple[MemoryCache, Clock]:
    clock = Clock()
    return MemoryCache(CacheSettings(**kw), clock=clock), clock


def test_lru_evicts_by_bytes_and_entries():
    c, _ = cache(max_bytes=3 * (1000 + 256 + len(repr(("api", 0)))), max_entries=10)
    for i in range(4):
        c.put(("api", i), b"x" * 1000, 1000, 60)
    assert c.get(("api", 0)) is None  # least recently used went first
    assert c.bytes <= c.settings.max_bytes and c.counts["api_evictions"] == 1
    c.get(("api", 1))  # now most recent
    c2, _ = cache(max_entries=2)
    for i in range(3):
        c2.put(("range", i), b"y", 1, 60)
    assert len(c2._items) == 2 and c2.counts["range_evictions"] == 1
    too_big, _ = cache(max_bytes=100)
    assert too_big.put(("api", "k"), b"z" * 500, 500, 60) is False and too_big.bytes == 0


def test_entries_expire_and_disabled_cache_stores_nothing():
    c, clock = cache(ttl_s=10)
    c.put(("api", "k"), b"v", 1, 10)
    clock.now += 9.9
    assert c.get(("api", "k")) == b"v"
    clock.now += 0.2
    assert c.get(("api", "k")) is None and c.counts["api_expired"] == 1
    off, _ = cache(enabled=False)
    assert off.put(("api", "k"), b"v", 1, 10) is False and off.get(("api", "k")) is None
    assert off.metrics()["entries"] == 0


def test_identity_needs_strong_etag_and_size():
    assert object_identity({"etag": '"a"'}, 10) == Identity(10, '"a"')
    assert object_identity({"etag": 'W/"a"'}, 10) is None
    assert object_identity({"last-modified": "Mon, 01 Jan 2026 00:00:00 GMT"}, 10) is None
    assert object_identity({"etag": '"a"'}, None) is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, 300),
        ({"cache-control": "public, max-age=60"}, 60),
        ({"cache-control": "max-age=600, s-maxage=30"}, 30),
        ({"cache-control": "max-age=99999"}, 300),
        ({"cache-control": "no-store"}, 0),
        ({"cache-control": "no-cache"}, 0),
        ({"cache-control": "private, max-age=60"}, 0),
        ({"cache-control": "max-age=bogus"}, 0),
        ({"set-cookie": "s=1"}, 0),
        ({"vary": "*"}, 0),
        ({"pragma": "no-cache"}, 0),
        ({"cache-control": "max-age=60", "age": "20"}, 40),
        ({"cache-control": "max-age=60", "age": "120"}, 0),  # already stale upstream
        ({"cache-control": "max-age=60", "age": "x"}, 0),
        ({"cache-control": "max-age=NaN"}, 0),
        ({"cache-control": "max-age=-5"}, 0),
        ({"cache-control": "max-age=1e3"}, 0),
        ({"cache-control": "max-age=60", "date": f"{DAY} 12:00:00 GMT"}, 60),
        ({"cache-control": "max-age=60", "date": f"{DAY} 11:59:30 GMT"}, 30),  # generated 30 s ago
        ({"cache-control": "max-age=60", "date": "garbage"}, 0),
        ({"date": f"{DAY} 12:00:00 GMT", "expires": f"{DAY} 12:01:00 GMT"}, 60),
        ({"date": f"{DAY} 12:00:00 GMT", "expires": "Sun, 27 Sep 2026 12:00:00 GMT"}, 0),
        ({"date": f"{DAY} 09:00:00 GMT", "expires": f"{DAY} 10:00:00 GMT"}, 0),  # stale Date
        ({"expires": "0"}, 0),
    ],
)
def test_cache_control_lifetime(headers, expected):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert lifetime(headers, 300, now=now) == expected


def test_object_identity_change_invalidates_old_blocks_and_signed_urls_are_ignored():
    c, clock = cache()
    url = "https://example.org/a.bw"
    c.observe(url, Identity(10, '"v1"'), 300)
    c.put(("range", url, Identity(10, '"v1"'), 0), b"0123456789", 10, 300)
    assert c.validated(url, since=clock.now) == (Identity(10, '"v1"'), 300)
    assert c.validated(url, since=clock.now + 1) is None  # not revalidated in this call
    c.observe(url, Identity(10, '"v2"'), 300)
    assert not c.contains(("range", url, Identity(10, '"v1"'), 0))
    assert c.counts["object_invalidations"] == 1 and c.counts["range_invalidations"] == 1
    c.observe(url, None, 300)  # validator disappeared: bypass, nothing reusable
    assert c.validated(url, since=0) is None
    for signed in (url + "?X-Amz-Signature=s", "https://u:p@example.org/a.bw"):
        c.observe(signed, Identity(10, '"v1"'), 300)
        assert c.validated(signed, since=0) is None


def key(method="GET", url="https://h/x", *, client=None, read_only=False, **kw):
    client = client or httpx.AsyncClient(trust_env=False)
    return request_key("s", client.build_request(method, url, **kw), client, read_only=read_only)


def test_request_key_uses_the_request_as_sent():
    ok = key(params={"q": "BRCA2"}, headers={"Accept": "a/json"})
    assert ok is not None and len(repr(ok)) < 200  # fixed-size digest, whatever the URL
    assert key(params={"q": "BRCA2"}, headers={"accept": "a/json"}) == ok
    assert key(params={"q": "BRCA1"}, headers={"accept": "a/json"}) != ok
    assert key(headers={"Accept-Language": "en"}) != key(headers={"Accept-Language": "fr"})
    refused = [
        {"params": {"api_key": "k"}},
        {"url": "https://h/x?token=t"},
        {"url": "https://h/x?X-Amz-Signature=s"},
        {"url": "https://u:p@h/x"},
        {"headers": {"Authorization": "Bearer synthetic"}},
        {"headers": {"api-key": "synthetic"}},
        {"headers": {"Range": "bytes=0-9"}},
        {"headers": {"X-Custom": "1"}},
        {"method": "HEAD"},
        {"method": "POST", "json": {"query": "{ a }"}},
        {"method": "POST", "json": {"query": "mutation { a }"}, "read_only": True},
        {"method": "POST", "content": b"not json", "read_only": True},
    ]
    for kw in refused:
        assert key(**kw) is None, kw
    assert key("POST", "https://h/g", json={"query": "{ a }"}, read_only=True) is not None
    with private_query(True):
        assert key() is None
    with private_query(False):
        assert key() is not None


def test_client_credentials_and_cookies_make_requests_uncacheable():
    for client in (
        httpx.AsyncClient(headers={"Authorization": "Bearer synthetic"}),
        httpx.AsyncClient(auth=("user", "synthetic")),
        httpx.AsyncClient(cookies={"session": "synthetic"}),
    ):
        assert key(client=client) is None


def test_only_accepted_formats_are_storable():
    def ok(
        body, ctype="application/json", status=200, accept="*/*", final="https://h/x", gql=False
    ):
        req = httpx.Request("GET", "https://h/x", headers={"Accept": accept})
        client = httpx.AsyncClient()
        return storable(req, status, {"content-type": ctype}, body, final, client, graphql=gql)

    assert ok(b'{"data": {"a": 1}}', gql=True)
    assert not ok(b'{"data": null, "errors": [{"message": "x"}]}', gql=True)
    assert not ok(b'{"data": {"a": 1}, "errors": [{"m": 1}]}', gql=True)
    assert not ok(b'{"truncated": ')
    assert not ok(b"<html>maintenance</html>", ctype="text/html")
    assert not ok(b"maintenance", ctype="text/plain", accept="application/json")
    assert not ok(b"{}", status=203)
    assert not ok(b"{}", final="https://h/x?X-Amz-Signature=s")
    assert ok(b"<a/>", ctype="text/xml", accept="application/xml")
    req = httpx.Request("GET", "https://h/x")
    jar = httpx.AsyncClient(cookies={"s": "synthetic"})  # cookie learned during the exchange
    assert not storable(req, 200, {}, b"x", "https://h/x", jar, graphql=False)


def test_keys_and_metadata_count_against_the_byte_bound():
    c, _ = cache(max_bytes=1024)
    assert c.put(("api", "x" * 1_000_000), b"x", 1, 300) is False
    assert c.metrics()["bytes"] == 0
    c.put(("api", "k"), b"x" * 100, 100, 300)
    assert c.metrics()["bytes"] >= 100 + len(repr(("api", "k")))
    c.observe("https://example.org/f", Identity(32, '"' + "x" * 1_000_000 + '"'), 300)
    assert c.validated("https://example.org/f", 0) is None and c.metrics()["bytes"] <= 1024


def test_metrics_are_aggregate_only():
    c, _ = cache()
    secret_url = "https://h/x?q=PRIVATE_VALUE"
    c.put(("api", "s", "GET", secret_url), b"PRIVATE_BODY", 12, 60)
    assert c.metrics()["entries"] == 1
    text = repr(c.metrics())
    assert "PRIVATE" not in text and "https://" not in text


def test_off_switch_from_environment():
    assert load_settings(env={}).cache.enabled is True
    s = load_settings(env={"GENOMICS_MCP_CACHE_ENABLED": "0"})
    assert s.cache.enabled is False and s.public_view()["cache"]["enabled"] is False
