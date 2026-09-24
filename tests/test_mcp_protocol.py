"""Real MCP round-trips: stdio subprocess and authenticated Streamable HTTP subprocess."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from genomics_mcp.registry import Operation

EXPECTED_TOOLS = {op.value for op in Operation}
DUMMY_AWS = {
    "AWS_ACCESS_KEY_ID": "AKIADUMMYDUMMYDUMMY0",
    "AWS_SECRET_ACCESS_KEY": "dummy-ambient-secret-never-used",
}


def server_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    # HOME is inherited unchanged (never repurposed); isolation comes from an explicit work
    # dir and empty AWS config/credential files.
    inherited = {k: os.environ[k] for k in ("HOME",) if k in os.environ}
    env = {
        **inherited,
        "PATH": os.environ.get("PATH", ""),
        "GENOMICS_MCP_WORK_DIR": str(tmp_path / "work"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": str(tmp_path / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "aws-credentials"),
        **DUMMY_AWS,
        **extra,
    }
    (tmp_path / "aws-config").write_text("")
    (tmp_path / "aws-credentials").write_text("")
    return env


async def exercise(client: Client) -> None:
    assert client.server_info is not None and client.server_info.name == "genomics-mcp"

    caps = json.loads((await client.read_resource("genomics://capabilities")).contents[0].text)
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    assert set(caps["operations"]) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.output_schema is not None
        # Tools without a backend must say so rather than look usable.
        unavailable = "not implemented in this build" in tool.description.lower()
        assert unavailable == (not caps["operations"][name]["available"]), name
    assert "interval" in tools["get_reads"].input_schema["required"]

    res = await client.call_tool("list_sources", {})
    assert res.is_error is False
    body = res.structured_content
    assert body["status"] == "ok"
    assert {s["name"] for s in body["data"]["records"]} >= {"ega", "ena", "clinvar"}
    # Text is a short summary; the full envelope is only in structuredContent.
    summary = res.content[0].text
    assert summary.startswith("list_sources: ok")
    assert f"records: {len(body['data']['records'])}" in summary
    assert len(summary) < 2000 and '"records"' not in summary

    res = await client.call_tool(
        "describe_dataset", {"source": "no_such_source", "accession": "X1"}
    )
    assert res.is_error is True
    assert res.structured_content["status"] == "error"
    assert res.structured_content["error"]["code"] == "unsupported"
    assert "error unsupported:" in res.content[0].text
    assert res.structured_content["error"]["message"] in res.content[0].text

    bad = await client.call_tool(
        "get_reads",
        {
            "file": {"uri": "/data/sample.bam"},
            "interval": {"contig": "chr1", "start": 10, "end": 5, "assembly": "GRCh38"},
        },
    )
    assert bad.is_error is True
    assert "end must be greater than start" in bad.content[0].text

    schema = await client.read_resource("genomics://schemas/file_ref")
    assert json.loads(schema.contents[0].text)["properties"]["visibility"]["default"] == "private"
    status = await client.read_resource("genomics://status")
    for secret in DUMMY_AWS.values():
        assert secret not in status.contents[0].text


async def test_stdio_round_trip(tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "genomics_mcp"], env=server_env(tmp_path)
    )
    with (tmp_path / "stderr.log").open("w") as errlog:
        async with Client(stdio_client(params, errlog=errlog), mode="legacy") as client:
            await exercise(client)
    log = (tmp_path / "stderr.log").read_text()
    assert "op=list_sources status=ok" in log  # stderr really captured
    assert "dummy-ambient-secret-never-used" not in log


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(tmp_path) -> Iterator[tuple[str, str, Path]]:
    token = secrets.token_urlsafe(32)
    port = free_port()
    log = tmp_path / "http.log"
    env = server_env(tmp_path, GENOMICS_MCP_HTTP_TOKEN=token)
    with log.open("w") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "genomics_mcp", "--transport", "http", "--port", str(port)],
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"server exited: {log.read_text()}")
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("server did not start")
            yield f"http://127.0.0.1:{port}/mcp", token, log
        finally:
            proc.terminate()
            proc.wait(timeout=10)


async def test_http_rejects_missing_or_wrong_token(http_server):
    url, token, _ = http_server
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(trust_env=False) as c:
        r = await c.post(url, json=init, headers=headers)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        r = await c.post(url, json=init, headers={**headers, "Authorization": "Bearer wrong"})
        assert r.status_code == 401
        r = await c.post(url, json=init, headers={**headers, "Authorization": f"Basic {token}"})
        assert r.status_code == 401
        r = await c.get(url.replace("/mcp", "/anything"))
        assert r.status_code == 401
        r = await c.post(url, json=init, headers={**headers, "Authorization": f"Bearer {token}"})
        assert r.status_code == 200


async def test_http_authenticated_round_trip(http_server):
    url, token, log = http_server
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(url, http_client=http), mode="legacy") as client:
            await exercise(client)
    text = log.read_text()
    assert token not in text
    assert "dummy-ambient-secret-never-used" not in text


def run_cli(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "genomics_mcp", *args],
        env=server_env(tmp_path, **env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_http_refuses_to_start_without_token(tmp_path):
    proc = run_cli(tmp_path, "--transport", "http", "--port", str(free_port()))
    assert proc.returncode == 2
    assert "bearer token" in proc.stderr


def test_http_refuses_non_loopback_without_opt_in(tmp_path):
    proc = run_cli(
        tmp_path,
        "--transport",
        "http",
        "--host",
        "0.0.0.0",
        GENOMICS_MCP_HTTP_TOKEN="x" * 40,
    )
    assert proc.returncode == 2
    assert "non-loopback" in proc.stderr


def test_cli_help_and_check_config(tmp_path):
    proc = run_cli(tmp_path, "--help")
    assert proc.returncode == 0 and "--transport" in proc.stdout
    proc = run_cli(tmp_path, "--check-config")
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["capabilities"]["operations"]["list_sources"]["available"] is True
    assert "dummy-ambient-secret-never-used" not in proc.stdout
