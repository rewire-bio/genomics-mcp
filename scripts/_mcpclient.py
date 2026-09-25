"""Shared helpers for the release scripts: launch an installed server command over stdio.

The scripts never import `genomics_mcp`; they talk MCP to whatever command they are given
(a clean venv, `uvx`, a container or the MCPB launcher).
"""

from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

TOOLS = {
    "list_sources",
    "search_datasets",
    "describe_dataset",
    "list_files",
    "list_samples",
    "get_sample_metadata",
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
    "inspect_locus",
    "compare_samples",
    "resolve_identifier",
    "normalize_variant",
    "lookup_variant",
    "lookup_gene",
    "lookup_protein",
}

# Injected into every server process to show ambient AWS credentials are never used.
DUMMY_AWS = {
    "AWS_ACCESS_KEY_ID": "AKIADUMMYAMBIENT0000",
    "AWS_SECRET_ACCESS_KEY": "dummy-ambient-secret-never-used",
    "AWS_SESSION_TOKEN": "dummy-ambient-session-never-used",
}

_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "DOCKER_HOST", "DOCKER_CONFIG")


def server_env(extra: dict[str, str] | None = None, *, dummy_aws: bool = True) -> dict[str, str]:
    """A minimal environment: no inherited cloud, proxy or HTSlib variables."""
    env = {k: os.environ[k] for k in _KEEP if k in os.environ}
    env.update(AWS_EC2_METADATA_DISABLED="true", AWS_CONFIG_FILE=os.devnull)
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    if dummy_aws:
        env.update(DUMMY_AWS)
    env.update(extra or {})
    return env


def parse_env(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--env expects KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


@asynccontextmanager
async def connect(command: list[str], env: dict[str, str], errlog: Path):
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    with errlog.open("w") as fh:
        async with Client(stdio_client(params, errlog=fh), mode="legacy") as client:
            yield client


async def call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    res = await client.call_tool(tool, args)
    body = res.structured_content
    if body is None:
        raise RuntimeError(f"{tool}: no structured content: {res.content[0].text[:500]}")
    return body


_SIGNED = re.compile(r"(?i)(x-amz-[a-z-]+|signature|sig|token|expires)=[^&\s\"']+")


def scrub(obj: Any, paths: dict[str, str] | None = None) -> Any:
    """Remove signed-URL query values, dummy secrets and local path prefixes from evidence."""
    text = json.dumps(obj)
    text = _SIGNED.sub(lambda m: f"{m.group(1)}=REDACTED", text)
    for secret in DUMMY_AWS.values():
        text = text.replace(secret, "REDACTED")
    replacements = {**(paths or {}), str(Path.home()): "~"}
    for prefix, label in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(json.dumps(prefix)[1:-1], label)
    return json.loads(text)
