"""Block cache for public remote files, measured with the counting range server.

Scientific results are compared with the cache off; request/byte counts come from the server,
not from the cache's own counters.
"""

from __future__ import annotations

import asyncio
import shutil
import time

import benchmark_cache as bench
import httpx
import pyBigWig
import pytest
from gm_test_support import FixtureServer, iv, make_settings

from genomics_mcp.cache import BLOCK_BYTES, MemoryCache
from genomics_mcp.config import CacheSettings
from genomics_mcp.errors import UpstreamError
from genomics_mcp.service import GenomicsService
from genomics_mcp.storage.http import HttpAccess
from genomics_mcp.storage.proxy import RangeProxy


def service(tmp_path, name: str, **cache) -> GenomicsService:
    return GenomicsService(make_settings(tmp_path / name, [], cache=cache))


async def call(svc: GenomicsService, op: str, args: dict):
    res = await svc.call(op, args)
    assert res.status.value == "ok", res.error
    return res


def science(res) -> dict:
    return bench._scientific(res.model_dump(mode="json")["data"])


def ranges(server: FixtureServer, since: int) -> list[str]:
    return [
        {k.lower(): v for k, v in e["headers"].items()}.get("range", "") for e in server.log[since:]
    ]


def signal_args(url: str, span: tuple[int, int], **file) -> dict:
    return {
        "file": {"uri": url, "visibility": "public", **file},
        "interval": bench._iv(span),
        "bins": 50,
    }


async def test_every_file_family_reuses_ranges_with_equal_results(perf, tmp_path):
    """Signal, reads/coverage/pileup, variants, sequence, features and inspect_locus."""
    rows = await bench.run_file_workloads(perf["bam"].parent, tmp_path / "work", samples=1)
    assert {r["name"] for r in rows} == {
        "signal", "reads", "coverage", "pileup", "variants", "sequence", "features",
        "inspect_locus",
    }  # fmt: skip
    for r in rows:
        assert r["results_equal"], r["name"]
        assert r["warm"]["bytes"][0] < r["cold"]["bytes"][0], r["name"]
        assert r["warm"]["requests"] <= r["cold"]["requests"], r["name"]
        nb = r["neighbour"]
        assert nb["after_warm"]["bytes"] < nb["cold"]["bytes"], r["name"]
        assert r["cache_after_warm"]["counts"].get("range_hits", 0) > 0


async def test_bigwig_different_interval_does_not_refetch_header_or_index(perf_server, tmp_path):
    url = perf_server.url("perf.bw")
    off = service(tmp_path, "off", enabled=False)
    on = service(tmp_path, "on")
    try:
        expected = science(await call(off, "get_signal", signal_args(url, bench.B)))
        await call(on, "get_signal", signal_args(url, bench.A))
        before = len(perf_server.log)
        res = await call(on, "get_signal", signal_args(url, bench.B))
        seen = ranges(perf_server, before)
        assert seen[0] == "bytes=0-0"  # identity revalidated for this call
        assert all(not r.startswith("bytes=0-") for r in seen[1:]), seen  # header block reused
        assert science(res) == expected
        assert any(w.startswith("cache:") for w in res.warnings)
        assert on.status()["cache"]["counts"]["range_hits"] > 0
    finally:
        await off.aclose()
        await on.aclose()


async def test_changed_file_is_revalidated_not_mixed(perf, tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    shutil.copy(perf["bigwig"], root / "track.bw")
    srv = FixtureServer(root)
    srv.httpd.handle_error = lambda *a: None
    on = service(tmp_path, "on")
    off = service(tmp_path, "off", enabled=False)
    try:
        args = signal_args(srv.url("track.bw"), bench.A)
        first = science(await call(on, "get_signal", args))
        w = pyBigWig.open(str(root / "track.tmp"), "w")
        w.addHeader([(bench.CONTIG, bench.LENGTH)], maxZooms=6)
        w.addEntries(bench.CONTIG, 0, values=[7.0] * (bench.LENGTH // 10), span=10, step=10)
        w.close()
        (root / "track.tmp").replace(root / "track.bw")
        changed = science(await call(on, "get_signal", args))
        assert changed == science(await call(off, "get_signal", args)) != first
        assert on.cache.counts["object_invalidations"] >= 1
    finally:
        await on.aclose()
        await off.aclose()
        srv.close()


async def test_block_reader_never_combines_versions(gsettings, tmp_path):
    """Same-size change between the call's validation and a later block miss."""
    root = tmp_path / "obj"
    root.mkdir()
    path = root / "data.bin"
    old = bytes(range(256)) * 1024  # 256 KiB, four blocks
    path.write_bytes(old)
    srv = FixtureServer(root)
    cache = MemoryCache(CacheSettings())
    http = HttpAccess(gsettings, cache=cache)
    try:
        since = time.monotonic()
        url = (
            await http.preflight(srv.url("data.bin"), source="https", timeout_s=5, observe=True)
        ).final_url
        valid = cache.validated(url, since)
        assert valid is not None
        got = bytearray()

        async def sink(piece: bytes, cached: bool) -> None:
            got.extend(piece)

        await http.read_blocks(url, 0, 70_000, valid, sink, source="https")
        assert bytes(got) == old[:70_001]
        path.write_bytes(bytes(reversed(old)))  # same size, new ETag
        got.clear()
        with pytest.raises(UpstreamError, match="changed"):
            await http.read_blocks(url, 0, 200_000, valid, sink, source="https")
        assert bytes(got) == old[: len(got)]  # only old-version bytes were delivered
        assert cache.validated(url, 0) is None
        assert not any(k[0] == "range" for k in cache._items)
        # The next call revalidates and reads the new version.
        since = time.monotonic()
        await http.preflight(url, source="https", timeout_s=5, observe=True)
        _, body, _, _ = await http.read_head(url, 100, source="https", timeout_s=5, since=since)
        assert body == bytes(reversed(old))[:100]
    finally:
        await http.aclose()
        srv.close()


@pytest.mark.parametrize(
    ("path", "file"),
    [
        ("signal.bw", {}),  # visibility defaults to private
        ("signal.bw?x=1", {"visibility": "public"}),  # query string (signed-URL shape)
        ("noetag/signal.bw", {"visibility": "public"}),  # no strong validator
        ("nostore/signal.bw", {"visibility": "public"}),  # Cache-Control: no-store
    ],
)
async def test_private_signed_unvalidated_and_no_store_files_bypass(
    golden, fixture_server, tmp_path, path, file
):
    svc = service(tmp_path, "on")
    try:
        args = {"file": {"uri": fixture_server.url(path), **file}, "interval": iv("chrG", 0, 200)}
        for _ in range(2):
            res = await call(svc, "get_signal", args)
            assert res.data["summary"]["value"] == (100 * 1.0 + 50 * 3.0 + 10 * 0.5) / 160
            assert not any(w.startswith("cache:") for w in res.warnings)
        counts = svc.cache.counts
        assert counts["range_stores"] == 0 and counts["range_hits"] == 0
        assert not any(k[0] in ("range", "object") for k in svc.cache._items)
    finally:
        await svc.aclose()


async def test_byte_cap_applies_to_cached_hits(gsettings, perf_server):
    cache = MemoryCache(CacheSettings())
    http = HttpAccess(gsettings, cache=cache)
    proxy = RangeProxy(http)
    try:
        since = time.monotonic()
        url = perf_server.url("perf.bw")
        await http.preflight(url, source="https", timeout_s=5, observe=True)
        await http.read_head(url, 3 * BLOCK_BYTES, source="https", timeout_s=5, since=since)
        before = len(perf_server.log)
        async with httpx.AsyncClient() as client:
            for name, cap, want in (("roomy", 10 * BLOCK_BYTES, 206), ("capped", 1000, 502)):
                route = await proxy.register(
                    name, url, source="https", name="perf.bw",
                    expires=time.monotonic() + 30, byte_cap=cap, cache_since=since,
                )  # fmt: skip
                resp = await client.get(route, headers={"Range": f"bytes=0-{2 * BLOCK_BYTES}"})
                assert resp.status_code == want
        assert len(perf_server.log) == before  # both answered from cached blocks
        assert [e.info.code for e in proxy.errors("capped")] == ["budget_exceeded"]
        assert proxy.cached_bytes("roomy") == 2 * BLOCK_BYTES + 1
    finally:
        await proxy.aclose()
        await http.aclose()


class RangeObject:
    """In-process public object (MockTransport): 206 answers with a strong ETag. Tests set
    `extra` response headers after the preflight, or make a response stall mid-stream."""

    URL = "http://127.0.0.1:9/obj.bin"

    def __init__(self, size: int = 4 * BLOCK_BYTES) -> None:
        self.data = bytes(i % 251 for i in range(size))
        self.extra: dict[str, str] = {}
        self.stall_after: int | None = None
        self.stalled = asyncio.Event()
        self.closed = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        a, _, b = request.headers["range"].removeprefix("bytes=").partition("-")
        start, end = int(a), min(int(b or len(self.data) - 1), len(self.data) - 1)
        headers = {"etag": '"v1"', "content-range": f"bytes {start}-{end}/{len(self.data)}"}
        return httpx.Response(
            206, headers={**headers, **self.extra}, stream=_Body(self, self.data[start : end + 1])
        )


class _Body(httpx.AsyncByteStream):
    def __init__(self, obj: RangeObject, body: bytes) -> None:
        self.obj, self.body = obj, body

    async def __aiter__(self):
        n = self.obj.stall_after
        if n is None:
            yield self.body
            return
        yield self.body[:n]
        self.obj.stalled.set()
        await asyncio.Event().wait()  # never resumes: only cancellation ends this stream

    async def aclose(self) -> None:
        self.obj.closed += 1


async def validated_object(gsettings, obj: RangeObject):
    cache = MemoryCache(CacheSettings())
    http = HttpAccess(gsettings, cache=cache, transport=httpx.MockTransport(obj))
    since = time.monotonic()
    await http.preflight(obj.URL, source="https", timeout_s=5, observe=True)
    return http, cache, cache.validated(obj.URL, since)


async def collect(http, obj, valid, end: int) -> bytes:
    got = bytearray()

    async def sink(piece: bytes, cached: bool) -> None:
        got.extend(piece)

    await http.read_blocks(obj.URL, 0, end, valid, sink, source="https")
    return bytes(got)


def range_keys(cache: MemoryCache) -> list:
    return [k for k in cache._items if k[0] == "range"]


@pytest.mark.parametrize(
    "extra",
    [{"cache-control": "no-store"}, {"cache-control": "private"}, {"set-cookie": "s=synthetic"}],
)
async def test_block_response_policy_is_checked_not_only_the_preflight(gsettings, extra):
    obj = RangeObject()
    http, cache, valid = await validated_object(gsettings, obj)
    try:
        assert valid is not None
        obj.extra = extra  # the actual block response forbids sharing
        assert await collect(http, obj, valid, 2 * BLOCK_BYTES) == obj.data[: 2 * BLOCK_BYTES + 1]
        assert range_keys(cache) == [] and cache.validated(obj.URL, 0) is None
    finally:
        await http.aclose()


async def test_changed_content_encoding_is_rejected(gsettings):
    obj = RangeObject()
    http, cache, valid = await validated_object(gsettings, obj)
    try:
        obj.extra = {"content-encoding": "gzip"}
        with pytest.raises(UpstreamError, match="changed"):
            await collect(http, obj, valid, 1000)
        assert range_keys(cache) == [] and cache.validated(obj.URL, 0) is None
    finally:
        await http.aclose()


async def test_cookie_carrying_client_does_not_share_ranges(gsettings):
    obj = RangeObject()
    obj.extra = {"set-cookie": "session=synthetic"}
    http, cache, valid = await validated_object(gsettings, obj)
    try:
        assert valid is None  # the preflight set a cookie: no identity recorded
        obj.extra = {}
        await http.preflight(obj.URL, source="https", timeout_s=5, observe=True)
        assert cache.validated(obj.URL, 0) is None  # later requests carry the cookie
        assert "cookie" in obj.requests[-1].headers
    finally:
        await http.aclose()


async def test_inverted_explicit_range_is_416_on_a_cached_route(gsettings):
    obj = RangeObject()
    http, _cache, _valid = await validated_object(gsettings, obj)
    proxy = RangeProxy(http)
    try:
        route = await proxy.register(
            "call", obj.URL, source="https", name="obj.bin",
            expires=time.monotonic() + 30, cache_since=0,
        )  # fmt: skip
        async with httpx.AsyncClient() as client:
            bad = await client.get(route, headers={"Range": "bytes=10-5"}, timeout=5)
            good = await client.get(route, headers={"Range": "bytes=0-9"}, timeout=5)
        assert bad.status_code == 416 and good.content == obj.data[:10]
    finally:
        await proxy.aclose()
        await http.aclose()


async def test_cancelled_relay_keeps_only_complete_blocks_and_closes_upstream(gsettings):
    """Event-driven: the upstream delivers block 0 and part of block 1, then stalls until the
    call is released. No wall-clock timing is involved."""
    obj = RangeObject()
    http, cache, _valid = await validated_object(gsettings, obj)
    proxy = RangeProxy(http)
    obj.stall_after = BLOCK_BYTES + 1000
    try:
        route = await proxy.register(
            "call", obj.URL, source="https", name="obj.bin",
            expires=time.monotonic() + 60, cache_since=0,
        )  # fmt: skip

        async def reader() -> None:
            async with httpx.AsyncClient() as client:
                await client.get(route, headers={"Range": f"bytes=0-{3 * BLOCK_BYTES}"})

        task = asyncio.create_task(reader())
        await asyncio.wait_for(obj.stalled.wait(), 30)  # generous; not a timing assertion
        assert proxy.active_relays() == 1
        await proxy.release("call")  # what finishing/cancelling a tool call does
        assert proxy.active_relays() == 0 and obj.closed >= 1  # upstream stream closed
        with pytest.raises(httpx.HTTPError):
            await task  # the reader saw a truncated response, not a complete one
        stored = {k[3]: v[0] for k, v in cache._items.items() if k[0] == "range"}
        assert list(stored) == [0] and stored[0] == obj.data[:BLOCK_BYTES]
    finally:
        await proxy.aclose()
        await http.aclose()


async def test_concurrent_calls_and_small_memory_bound_keep_results_exact(perf_server, tmp_path):
    url = perf_server.url("perf.bw")
    spans = [bench.A, bench.B, bench.OVERLAP]
    off = service(tmp_path, "off", enabled=False)
    tight = service(tmp_path, "tight", max_bytes=200_000)
    try:
        expected = [science(await call(off, "get_signal", signal_args(url, s))) for s in spans]
        for _ in range(2):
            got = await asyncio.gather(
                *[call(tight, "get_signal", signal_args(url, s)) for s in spans * 2]
            )
            assert [science(r) for r in got] == expected * 2
        m = tight.status()["cache"]
        assert m["bytes"] <= 200_000 and m["counts"]["range_evictions"] > 0
    finally:
        await off.aclose()
        await tight.aclose()


async def test_disabled_cache_reads_upstream_every_call(perf_server, tmp_path):
    off = service(tmp_path, "off", enabled=False)
    try:
        args = signal_args(perf_server.url("perf.bw"), bench.A)
        await call(off, "get_signal", args)
        before = len(perf_server.log)
        res = await call(off, "get_signal", args)
        assert len(perf_server.log) - before > 1  # data ranges, not only the preflight
        assert off.status()["cache"] == {**off.status()["cache"], "enabled": False, "entries": 0}
        assert not any(w.startswith("cache:") for w in res.warnings)
    finally:
        await off.aclose()
