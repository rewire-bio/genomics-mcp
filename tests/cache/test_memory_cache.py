"""MemoryCache bounds and the request/response policy shared by every cached path."""

from __future__ import annotations

import pytest

from genomics_mcp.cache import (
    Identity,
    MemoryCache,
    cacheable_request,
    lifetime,
    object_identity,
    private_query,
    storable_body,
)
from genomics_mcp.config import CacheSettings, load_settings


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def cache(**kw) -> tuple[MemoryCache, Clock]:
    clock = Clock()
    return MemoryCache(CacheSettings(**kw), clock=clock), clock


def test_lru_evicts_by_bytes_and_entries():
    c, _ = cache(max_bytes=3 * (1000 + 256), max_entries=10)
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
    ],
)
def test_cache_control_lifetime(headers, expected):
    assert lifetime(headers, 300) == expected


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


def test_request_policy_refuses_credentials_ranges_writes_and_private_queries():
    ok = cacheable_request("s", "GET", "https://h/x", {"q": "BRCA2"}, {"Accept": "a/json"})
    assert ok is not None
    assert cacheable_request("s", "GET", "https://h/x", {"q": "BRCA2"}, {"accept": "a/json"}) == ok
    refused = [
        ("GET", "https://h/x", {"api_key": "k"}, {}, None, False),
        ("GET", "https://h/x?token=t", None, {}, None, False),
        ("GET", "https://h/x?X-Amz-Signature=s", None, {}, None, False),
        ("GET", "https://u:p@h/x", None, {}, None, False),
        ("GET", "https://h/x", None, {"Authorization": "Bearer t"}, None, False),
        ("GET", "https://h/x", None, {"api-key": "k"}, None, False),
        ("GET", "https://h/x", None, {"Range": "bytes=0-9"}, None, False),
        ("HEAD", "https://h/x", None, {}, None, False),
        ("POST", "https://h/g", None, {}, {"query": "{ a }"}, False),
        ("POST", "https://h/g", None, {}, {"query": "mutation { a }"}, True),
    ]
    for method, url, params, headers, body, read_only in refused:
        key = cacheable_request("s", method, url, params, headers, body, read_only=read_only)
        assert key is None, (method, url, params, headers)
    assert cacheable_request(
        "s", "POST", "https://h/g", None, {}, {"query": "{ a }"}, read_only=True
    )
    with private_query(True):
        assert cacheable_request("s", "GET", "https://h/x", None, {}) is None
    with private_query(False):
        assert cacheable_request("s", "GET", "https://h/x", None, {}) is not None


def test_graphql_errors_and_invalid_json_are_not_storable():
    js = {"content-type": "application/json"}
    assert storable_body(js, b'{"data": {"a": 1}}', graphql=True)
    assert not storable_body(js, b'{"data": null, "errors": [{"message": "x"}]}', graphql=True)
    assert not storable_body(js, b'{"data": {"a": 1}, "errors": [{"m": 1}]}', graphql=True)
    assert not storable_body(js, b'{"truncated": ', graphql=False)
    assert storable_body({"content-type": "text/xml"}, b"<a/>", graphql=False)


def test_metrics_are_aggregate_only():
    c, _ = cache()
    secret_url = "https://h/x?q=PRIVATE_VALUE"
    c.put(("api", "s", "GET", secret_url), b"PRIVATE_BODY", 12, 60)
    text = repr(c.metrics())
    assert "PRIVATE" not in text and "https://" not in text


def test_off_switch_from_environment():
    assert load_settings(env={}).cache.enabled is True
    s = load_settings(env={"GENOMICS_MCP_CACHE_ENABLED": "0"})
    assert s.cache.enabled is False and s.public_view()["cache"]["enabled"] is False
