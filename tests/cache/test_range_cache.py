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


async def test_cancelled_read_stores_only_complete_blocks(gsettings, perf):
    srv = FixtureServer(perf["bam"].parent)
    srv.httpd.handle_error = lambda *a: None
    cache = MemoryCache(CacheSettings())
    http = HttpAccess(gsettings, cache=cache)
    try:
        since = time.monotonic()
        url = srv.url("slow/perf.bw")  # 4 KiB every 50 ms: one block takes about 0.8 s
        await http.preflight(url, source="https", timeout_s=5, observe=True)
        valid = cache.validated(url, since)

        async def sink(piece: bytes, cached: bool) -> None:
            pass

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                http.read_blocks(url, 0, 4 * BLOCK_BYTES - 1, valid, sink, source="https"), 1.2
            )
        blocks = {k[3]: v[0] for k, v in cache._items.items() if k[0] == "range"}
        assert blocks and all(len(v) == BLOCK_BYTES for v in blocks.values())
        assert len(blocks) < 4
    finally:
        await http.aclose()
        srv.close()


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
