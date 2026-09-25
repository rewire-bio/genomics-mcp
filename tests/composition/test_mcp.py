"""Both composition tools over real MCP (in-process and stdio subprocess); native cancellation."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from genomics_mcp.registry import Operation
from genomics_mcp.server import build_server

from .conftest import iv, ref
from .doubles import real_providers


async def test_in_process_mcp_calls_both_tools(make_service, fx, spy):
    svc = make_service()
    async with Client(build_server(svc)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("inspect_locus", "compare_samples"):
            assert "not implemented" not in tools[name].description.lower()
        res = await client.call_tool(
            "inspect_locus",
            {
                "interval": iv(100, 200),
                "files": [ref(fx.deep_bam), ref(fx.cohort_vcf)],
                "reference": ref(fx.fasta),
            },
        )
        body = res.structured_content
        assert body["status"] == "ok" and body["data"]["records"]
        assert res.content[0].text.startswith("inspect_locus: ok")
        res = await client.call_tool(
            "compare_samples",
            {"interval": iv(100, 200), "files": [ref(fx.cohort_vcf), ref(fx.second_vcf)]},
        )
        body = res.structured_content
        assert body["status"] == "ok"
        assert {s["key"] for s in body["data"]["samples"]} == {"f0:S1", "f0:S2", "f1:S1"}
    assert spy.requests == []


@pytest.mark.skipif(not real_providers(), reason="needs the E2-E5 providers")
async def test_stdio_subprocess_with_default_providers(fx, tmp_path, data_root):
    env = server_env(tmp_path, data_root)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "genomics_mcp", "--transport", "stdio"], env=env
    )
    async with Client(stdio_client(params), mode="legacy") as client:
        res = await client.call_tool(
            "compare_samples",
            {"interval": iv(100, 200), "files": [ref(fx.deep_bam), ref(fx.shallow_bam)]},
        )
        body = res.structured_content
        assert body["status"] == "ok", body
        means = {f["id"]: f["summary"]["mean"] for f in body["data"]["files"]}
        assert means == {
            "f0": sum(fx.depth("deep", 100, 200)) / 100,
            "f1": sum(fx.depth("shallow", 100, 200)) / 100,
        }
        res = await client.call_tool(
            "inspect_locus",
            {
                "interval": iv(100, 200),
                "files": [ref(fx.cohort_vcf)],
                "reference_sources": ["clinvar"],
            },
        )
        body = res.structured_content
        assert body["status"] == "partial"
        assert [e["code"] for e in body["errors"]] == ["consent_required"]


def server_env(tmp_path, data_root) -> dict[str, str]:
    """Scrubbed env: explicit work dir/roots, empty AWS files, dummy ambient keys (unused)."""
    (tmp_path / "aws-config").write_text("")
    (tmp_path / "aws-credentials").write_text("")
    return {
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PATH": os.environ.get("PATH", ""),
        "GENOMICS_MCP_WORK_DIR": str(tmp_path / "work"),
        "GENOMICS_MCP_ALLOWED_ROOTS": str(data_root),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": str(tmp_path / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "aws-credentials"),
        "AWS_ACCESS_KEY_ID": "AKIADUMMYDUMMYDUMMY0",
        "AWS_SECRET_ACCESS_KEY": "dummy-ambient-secret-never-used",
    }


def _children() -> list[str]:
    out = subprocess.run(
        ["/usr/bin/pgrep", "-P", str(os.getpid())], capture_output=True, text=True, check=False
    )
    return out.stdout.split()


@pytest.mark.skipif(not real_providers(), reason="needs the native E4 readers")
async def test_cancellation_kills_native_reader_processes(make_service, fx):
    svc = make_service()
    assert _children() == []
    task = asyncio.create_task(
        svc.call(
            Operation.INSPECT_LOCUS,
            {
                "interval": iv(100, 200),
                "files": [ref(fx.deep_bam), ref(fx.cohort_vcf), ref(fx.shallow_bam)],
            },
        )
    )
    for _ in range(200):  # wait until a native reader process exists
        if _children():
            break
        await asyncio.sleep(0.005)
    assert _children(), "no native reader process observed"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if not _children():
            break
        await asyncio.sleep(0.02)
    assert _children() == []
