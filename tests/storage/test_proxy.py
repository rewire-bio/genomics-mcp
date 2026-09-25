"""Native readers reach remote files only through the per-call loopback proxy."""

from __future__ import annotations

import asyncio
import time

import httpx
from gm_test_support import FixtureServer, envelope, iv

from genomics_mcp.service import GenomicsService
from genomics_mcp.storage.http import HttpAccess
from genomics_mcp.storage.proxy import RangeProxy


async def test_changing_redirect_never_reaches_unapproved_host(service, golden, fixture_server):
    other = FixtureServer(golden["root"])  # same files, reached as "localhost" (not trusted)
    try:
        # Preflight and head read are served; every later (native) request redirects.
        fixture_server.redirect_after["/signal.bw"] = (
            2,
            f"http://localhost:{other.port}/signal.bw",
        )
        res = envelope(
            await service.call(
                "get_signal",
                {"file": {"uri": fixture_server.url("signal.bw")}, "interval": iv("chrG", 0, 200)},
            )
        )
        assert res["status"] == "error"
        assert res["error"]["code"] in ("invalid_input", "unauthorized")
        assert other.log == []
    finally:
        other.close()


async def test_permitted_redirect_still_works(service, golden, fixture_server):
    fixture_server.redirect_after["/moved.bw"] = (2, fixture_server.url("signal.bw"))
    (golden["root"] / "moved.bw").write_bytes(golden["bigwig"].read_bytes())
    res = envelope(
        await service.call(
            "get_signal",
            {"file": {"uri": fixture_server.url("moved.bw")}, "interval": iv("chrG", 0, 200)},
        )
    )
    assert res["status"] == "ok", res.get("error")
    assert res["data"]["summary"]["value"] == (100 * 1.0 + 50 * 3.0 + 10 * 0.5) / 160


async def test_routes_are_per_call_capabilities(gsettings, fixture_server):
    http = HttpAccess(gsettings)
    proxy = RangeProxy(http)
    try:
        url = await proxy.register(
            "call-a",
            fixture_server.url("signal.bw"),
            source="https",
            name="signal.bw",
            expires=time.monotonic() + 30,
        )
        async with httpx.AsyncClient(trust_env=False) as c:
            ok = await c.get(url, headers={"Range": "bytes=0-3"})
            assert ok.status_code == 206 and len(ok.content) == 4
            guess = url.rsplit("/", 2)[0] + "/not-a-token/signal.bw"
            assert (await c.get(guess)).status_code == 404
            await proxy.release("call-a")
            assert (await c.get(url, headers={"Range": "bytes=0-3"})).status_code == 404
    finally:
        await proxy.aclose()
        await http.aclose()


async def test_release_cancels_in_flight_relay(gsettings, golden, tmp_path):
    root = tmp_path / "big"
    root.mkdir()
    (root / "big.bin").write_bytes(b"\0" * 4_000_000)
    srv = FixtureServer(root)
    http = HttpAccess(gsettings)
    proxy = RangeProxy(http)
    try:
        url = await proxy.register(
            "call-b",
            srv.url("slow/big.bin"),
            source="https",
            name="big.bin",
            expires=time.monotonic() + 30,
        )

        async def read_slowly():
            async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
                async with c.stream("GET", url, headers={"Range": "bytes=0-"}) as r:
                    async for _ in r.aiter_raw():
                        await asyncio.sleep(0.01)

        reader = asyncio.create_task(read_slowly())
        await asyncio.sleep(0.5)
        assert proxy.active_relays() == 1
        await proxy.release("call-b")
        assert proxy.active_relays() == 0
        sent = srv.bytes_sent.get("/slow/big.bin", 0)
        await asyncio.sleep(0.5)
        assert srv.bytes_sent.get("/slow/big.bin", sent) < 4_000_000
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    finally:
        await proxy.aclose()
        await http.aclose()
        srv.close()


async def test_native_timeout_releases_routes(tmp_path, golden):
    from gm_test_support import make_settings

    root = tmp_path / "srv"
    root.mkdir()
    (root / "s.bw").write_bytes(golden["bigwig"].read_bytes())
    srv = FixtureServer(root)
    settings = make_settings(tmp_path, [], limits={"interactive_timeout_s": 3})
    svc = GenomicsService(settings)
    try:
        srv.redirect_after["/s.bw"] = (2, srv.url("slow/s.bw"))
        res = envelope(
            await svc.call(
                "get_signal", {"file": {"uri": srv.url("s.bw")}, "interval": iv("chrG", 0, 10)}
            )
        )
        storage = svc.registry.component("storage").manager
        assert storage.proxy.active_relays() == 0
        assert res["status"] in ("ok", "error")
    finally:
        await svc.aclose()
        srv.close()


async def test_concurrent_readers_of_one_request_keep_their_routes(service, golden, fixture_server):
    """Fan-out siblings share request_id; one finishing must not close the other's routes."""
    import pyBigWig

    from genomics_mcp.context import OperationContext, fan_out
    from genomics_mcp.public import Deadline
    from genomics_mcp.registry import Operation
    from genomics_mcp.requests import SignalRequest
    from genomics_mcp.result import EffectiveLimits

    big = golden["root"] / "big_signal.bw"
    if not big.exists():
        bw = pyBigWig.open(str(big), "w")
        bw.addHeader([("chrG", 3000)])
        bw.addEntries("chrG", 0, values=[float(i % 7) for i in range(3000)], span=1, step=1)
        bw.close()
    ctx = OperationContext(
        operation=Operation.GET_SIGNAL,
        settings=service.settings,
        limits=EffectiveLimits.build(service.settings.limits),
        deadline=Deadline(30),
        registry=service.registry,
        http=service.http,
        request_id="shared-request",
    )
    handler = service.registry.handler(Operation.GET_SIGNAL, "bigwig").handler

    def task(url):
        req = SignalRequest(file={"uri": url}, interval=iv("chrG", 0, 3000), max_records=5)
        return lambda c: handler(req, c)

    result = await fan_out(
        ctx,
        {
            "fast": task(fixture_server.url("signal.bw")),
            "slow": task(fixture_server.url("slow/big_signal.bw")),
        },
    )
    assert result.errors == []
    assert set(result.outputs) == {"fast", "slow"}
    with pyBigWig.open(str(big)) as bw:
        expected = bw.stats("chrG", 0, 3000, exact=True)[0]
    assert result.outputs["slow"].data["summary"]["value"] == expected
    storage = service.registry.component("storage").manager
    assert storage.proxy.active_relays() == 0 and storage.proxy._routes == {}
