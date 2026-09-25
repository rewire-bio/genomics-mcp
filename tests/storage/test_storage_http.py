"""HTTP(S) storage: range preflight, bounded reads, redirects and destination policy."""

from __future__ import annotations

import socket
from urllib.parse import quote

import pytest
from gm_test_support import FixtureServer, envelope, iv, make_settings

from genomics_mcp.context import OperationContext
from genomics_mcp.errors import GenomicsError
from genomics_mcp.models import FileRef
from genomics_mcp.public import Deadline
from genomics_mcp.registry import Operation
from genomics_mcp.result import EffectiveLimits
from genomics_mcp.service import GenomicsService


def ctx_for(svc: GenomicsService) -> OperationContext:
    return OperationContext(
        operation=Operation.GET_READS,
        settings=svc.settings,
        limits=EffectiveLimits.build(svc.settings.limits),
        deadline=Deadline(30),
        registry=svc.registry,
        http=svc.http,
        request_id="test",
    )


@pytest.fixture
async def svc(gsettings):
    s = GenomicsService(gsettings)
    yield s
    await s.aclose()


async def test_range_preflight_is_a_real_get(svc, fixture_server):
    r = await ctx_for(svc).resolve_file(FileRef(uri=fixture_server.url("golden.bam")))
    assert r.range_capable is True and r.readiness.state == "ready"
    assert r.size_bytes == (fixture_server.root / "golden.bam").stat().st_size
    assert r.index_state == "present" and r.index_kind == "bai"
    first = fixture_server.log[0]
    assert first["path"] == "/golden.bam" and first["headers"]["Range"] == "bytes=0-0"


async def test_ignored_range_closes_without_downloading(tmp_path, golden):
    big = tmp_path / "srv"
    big.mkdir()
    (big / "big.bam").write_bytes(golden["bam"].read_bytes() + b"\0" * (32 * 1024 * 1024))
    srv = FixtureServer(big)
    svc = GenomicsService(make_settings(tmp_path, [golden["root"]]))
    try:
        res = envelope(
            await svc.call(
                "get_reads",
                {"file": {"uri": srv.url("ignore-range/big.bam")}, "interval": iv("chrG", 0, 10)},
            )
        )
        assert res["error"]["code"] == "preparation_required"
        assert res["error"]["details"]["readiness"] == "download_required"
        sent = sum(v for k, v in srv.bytes_sent.items() if "ignore-range" in k)
        assert sent < 4 * 1024 * 1024, sent  # far less than the 32 MiB body
    finally:
        await svc.aclose()
        srv.close()


async def test_status_codes_are_distinct(svc, fixture_server):
    ctx = ctx_for(svc)
    codes = {}
    for path in ("forbidden/golden.bam", "expired/golden.bam", "nope.bam"):
        with pytest.raises(GenomicsError) as exc:
            await ctx.resolve_file(FileRef(uri=fixture_server.url(path)))
        codes[path] = exc.value.info.code
    assert codes == {
        "forbidden/golden.bam": "unauthorized",
        "expired/golden.bam": "unauthorized",
        "nope.bam": "not_found",
    }


async def test_redirect_final_url_stays_internal(svc, fixture_server):
    ctx = ctx_for(svc)
    r = await ctx.resolve_file(FileRef(uri=fixture_server.url("redirect/golden.bam")))
    assert r.redirected is True
    assert r.open_uri == fixture_server.url("golden.bam")
    assert r.file.uri == fixture_server.url("redirect/golden.bam")


async def test_redirect_to_metadata_and_downgrade_are_refused(svc, fixture_server):
    ctx = ctx_for(svc)
    with pytest.raises(GenomicsError) as exc:
        await ctx.resolve_file(FileRef(uri=fixture_server.url("redirect-meta/golden.bam")))
    assert exc.value.info.code == "unauthorized"
    assert "metadata" in exc.value.info.message
    for target in ("http://169.254.170.2/v2/credentials", "http://[fd00:ec2::254]/latest"):
        with pytest.raises(GenomicsError) as exc:
            await ctx.resolve_file(
                FileRef(uri=fixture_server.url("redirect-to/" + quote(target, safe="")))
            )
        assert exc.value.info.code == "unauthorized"


async def test_private_hosts_and_plain_http_need_explicit_allow(tmp_path, golden, fixture_server):
    svc = GenomicsService(
        make_settings(tmp_path, [golden["root"]], storage={"local_network_hosts": []})
    )
    try:
        with pytest.raises(GenomicsError) as exc:
            await ctx_for(svc).resolve_file(FileRef(uri=fixture_server.url("golden.bam")))
        assert exc.value.info.code in ("invalid_input", "unauthorized")
        with pytest.raises(GenomicsError) as exc:
            await ctx_for(svc).resolve_file(FileRef(uri="https://localhost:1/x.bam"))
        assert exc.value.info.code == "unauthorized"
    finally:
        await svc.aclose()


async def test_hostname_resolving_to_link_local_is_refused(svc, monkeypatch):
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "innocent.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(GenomicsError) as exc:
        await ctx_for(svc).resolve_file(FileRef(uri="https://innocent.example/a.bam"))
    assert exc.value.info.code == "unauthorized"


async def test_signed_query_values_never_leave(svc, fixture_server):
    uri = fixture_server.url("golden.bam") + "?X-Amz-Signature=supersecretsig&X-Amz-Expires=60"
    res = envelope(
        await svc.call(
            "get_reads",
            {
                "file": {
                    "uri": uri,
                    "index_uri": fixture_server.url("golden.bam.bai") + "?sig=zzz",
                },
                "interval": iv("chrG", 100, 110),
            },
        )
    )
    assert res["status"] == "ok", res
    text = str(res)
    assert "supersecretsig" not in text and "zzz" not in text


async def test_query_string_urls_do_not_guess_sidecars(svc, fixture_server):
    uri = fixture_server.url("golden.bam") + "?token=abc"
    r = await ctx_for(svc).resolve_file(FileRef(uri=uri))
    assert r.index_state == "not_checked"
    assert not any(e["path"].endswith(".bai") for e in fixture_server.log)
    res = envelope(
        await svc.call("get_reads", {"file": {"uri": uri}, "interval": iv("chrG", 100, 110)})
    )
    assert res["error"]["code"] == "preparation_required"


async def test_ambient_proxy_variables_are_ignored(svc, fixture_server, monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    res = envelope(
        await svc.call(
            "get_variants",
            {"file": {"uri": fixture_server.url("golden.bcf")}, "interval": iv("chrG", 0, 50)},
        )
    )
    assert res["status"] == "ok" and [r["pos"] for r in res["data"]["records"]] == [21, 31, 41]


async def test_http_and_local_results_are_identical(svc, fixture_server, golden):
    for op, name, args in (
        ("get_reads", "golden.bam", {"exclude_flags": 1024}),
        ("get_coverage", "golden.bam", {}),
        ("get_variants", "golden.vcf.gz", {}),
        ("get_variants", "golden.bcf", {}),
        ("get_features", "features.gff3.gz", {}),
        ("get_sequence", "ref.fa", {}),
    ):
        interval = iv("chrG", 0, 300)
        local = envelope(
            await svc.call(
                op, {"file": {"uri": str(golden["root"] / name)}, "interval": interval, **args}
            )
        )
        remote = envelope(
            await svc.call(
                op, {"file": {"uri": fixture_server.url(name)}, "interval": interval, **args}
            )
        )
        assert local["status"] == "ok", (op, name, local.get("error"))
        assert local["data"]["records"] == remote["data"]["records"], (op, name)
