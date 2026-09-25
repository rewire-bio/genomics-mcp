"""E2-E5 are callable through the real MCP stdio transport, with honest capabilities."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from gm_test_support import GOLDEN_ASSEMBLY
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from genomics_mcp.config import load_settings
from genomics_mcp.registry import Operation
from genomics_mcp.service import GenomicsService

E2_E5_OPS = {
    "fetch_file",
    "get_transfer_status",
    "cancel_transfer",
    "get_reads",
    "get_coverage",
    "get_pileup",
    "get_variants",
    "get_sequence",
    "get_features",
    "get_signal",
    "list_files",
}


def test_capabilities_are_accurate(tmp_path):
    svc = GenomicsService(load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path)}}))
    caps = svc.capabilities()
    for op in E2_E5_OPS:
        assert caps["operations"][op]["available"], op
    assert caps["operations"]["get_features"]["keys"] == ["bed", "bigbed", "gff3", "gtf"]
    assert caps["operations"]["get_reads"]["keys"] == ["bam", "cram"]
    assert {"local", "s3"} <= set(caps["operations"]["list_files"]["keys"])
    assert {"file", "http", "https", "s3"} <= set(caps["file_schemes"])
    for p in (
        "genomics_mcp.storage",
        "genomics_mcp.artifacts",
        "genomics_mcp.readers",
        "genomics_mcp.signal",
    ):
        assert caps["providers"][p] == "loaded"
    states = {s["name"]: s["state"] for s in svc.status()["sources"]}
    assert states["local"] == states["https"] == states["s3"] == "ok"
    asyncio.run(svc.aclose())


async def test_stdio_round_trip_with_real_data(tmp_path, golden):
    (tmp_path / "aws-config").write_text("")
    (tmp_path / "aws-credentials").write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GENOMICS_MCP_WORK_DIR": str(tmp_path / "work"),
        "GENOMICS_MCP_ALLOWED_ROOTS": str(golden["root"]),
        "AWS_CONFIG_FILE": str(tmp_path / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "aws-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_ACCESS_KEY_ID": "AKIADUMMYDUMMYDUMMY0",
        "AWS_SECRET_ACCESS_KEY": "dummy-ambient-secret-never-used",
    }
    interval = {"contig": "chrG", "start": 20, "end": 45, "assembly": GOLDEN_ASSEMBLY}
    params = StdioServerParameters(command=sys.executable, args=["-m", "genomics_mcp"], env=env)
    with (tmp_path / "stderr.log").open("w") as errlog:
        async with Client(stdio_client(params, errlog=errlog), mode="legacy") as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            for op in E2_E5_OPS:
                assert "not implemented in this build" not in tools[op].description.lower(), op
            res = await client.call_tool(
                "get_variants", {"file": {"uri": str(golden["vcf"])}, "interval": interval}
            )
            body = res.structured_content
            assert body["status"] == "ok"
            assert [r["pos"] for r in body["data"]["records"]] == [21, 31, 41]
            assert body["data"]["records"][0]["samples"]["S1"]["genotype"]["text"] == "1|2"
            assert "records: 3" in res.content[0].text
            res = await client.call_tool(
                "get_coverage",
                {"file": {"uri": str(golden["bam"])}, "interval": interval, "bin_size": 5},
            )
            assert res.structured_content["status"] == "ok"
            assert len(res.structured_content["data"]["records"]) == 5
            res = await client.call_tool(
                "get_signal", {"file": {"uri": str(golden["bigwig"])}, "interval": interval}
            )
            assert res.structured_content["data"]["summary"]["value"] == 1.0
            res = await client.call_tool(
                "fetch_file", {"file": {"uri": str(golden["fasta_unindexed"])}, "prepare": True}
            )
            tid = res.structured_content["data"]["transfer"]["transfer_id"]
            for _ in range(100):
                st = await client.call_tool("get_transfer_status", {"transfer_id": tid})
                if st.structured_content["data"]["transfer"]["state"] == "completed":
                    break
                await asyncio.sleep(0.1)
            art = st.structured_content["data"]["transfer"]["artifact"]
            assert art["path"].startswith(str(tmp_path / "work"))
            assert "binary" not in json.dumps(st.structured_content)
            res = await client.call_tool(
                "list_files", {"source": "local", "accession": str(golden["root"])}
            )
            assert res.structured_content["status"] == "ok"
            denied = await client.call_tool(
                "get_reads",
                {"file": {"uri": "/etc/hosts", "format": "bam"}, "interval": interval},
            )
            assert denied.structured_content["error"]["code"] == "unauthorized"
    assert "dummy-ambient-secret-never-used" not in (tmp_path / "stderr.log").read_text()


def test_every_operation_has_a_request_model():
    assert {op.value for op in Operation} >= E2_E5_OPS
