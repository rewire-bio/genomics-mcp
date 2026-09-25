# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["mcp==2.2.0"]
# ///
"""MCP protocol smoke test against an installed server command.

    uv run --script scripts/mcp_smoke.py --data-dir /tmp/smoke-data -- genomics-mcp
    uv run --script scripts/mcp_smoke.py --data-dir "$PWD/smoke" --server-data-dir /data -- \\
        docker run --rm -i --mount type=bind,source="$PWD/smoke",target=/data,readonly IMAGE

Checks initialize, tools/list (all 23 tools available), resources, list_sources and an exact
synthetic FASTA region, with dummy ambient AWS credentials present and unused.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import anyio
from _mcpclient import DUMMY_AWS, TOOLS, call, connect, parse_env, server_env

SEQ = "ACGTTGCAAGGCTTAACCGGTTAAACCCGGGTTTAAAACCCC"  # 42 bases, synthetic


def write_fixture(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fasta = data_dir / "smoke.fa"
    fasta.write_text(f">chrSmoke synthetic\n{SEQ}\n")
    # name, length, offset of first base, bases per line, bytes per line
    offset = len(">chrSmoke synthetic\n")
    (data_dir / "smoke.fa.fai").write_text(
        f"chrSmoke\t{len(SEQ)}\t{offset}\t{len(SEQ)}\t{len(SEQ) + 1}\n"
    )


async def run(args: argparse.Namespace) -> dict:
    write_fixture(args.data_dir)
    fasta = f"{args.server_data_dir or args.data_dir}/smoke.fa"
    env = server_env(parse_env(args.env))
    checks: dict[str, object] = {}
    errlog = Path(tempfile.mkstemp(prefix="genomics-mcp-smoke-", suffix=".log")[1])
    async with connect(args.command, env, errlog) as client:
        info = client.server_info
        assert info is not None and info.name == "genomics-mcp", info
        if args.expect_version:
            assert info.version == args.expect_version, info.version
        checks["server"] = {"name": info.name, "version": info.version}

        tools = {t.name for t in (await client.list_tools()).tools}
        assert tools == TOOLS, sorted(tools ^ TOOLS)
        caps = json.loads((await client.read_resource("genomics://capabilities")).contents[0].text)
        unavailable = sorted(n for n, op in caps["operations"].items() if not op["available"])
        assert not unavailable, f"tools without a backend: {unavailable}"
        checks["tools"] = len(tools)

        sources = await call(client, "list_sources", {})
        assert sources["status"] == "ok", sources.get("error")
        checks["sources"] = sorted(s["name"] for s in sources["data"]["records"])

        seq = await call(
            client,
            "get_sequence",
            {
                "file": {"uri": fasta, "visibility": "private"},
                "interval": {"contig": "chrSmoke", "start": 3, "end": 13, "assembly": "synthetic"},
            },
        )
        assert seq["status"] == "ok", seq.get("error")
        got = seq["data"]["records"][0]["sequence"]
        assert got == SEQ[3:13], got
        checks["get_sequence"] = got

        status = (await client.read_resource("genomics://status")).contents[0].text
        assert not any(v in status for v in DUMMY_AWS.values())
    log = errlog.read_text()
    errlog.unlink()
    assert not any(v in log for v in DUMMY_AWS.values()), "dummy secret in server log"
    checks["dummy_ambient_aws_unused"] = True
    return checks


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--data-dir", type=Path, required=True, help="host directory for the fixture")
    p.add_argument("--server-data-dir", help="the same directory as the server sees it")
    p.add_argument("--env", action="append", default=[], help="KEY=VALUE for the server")
    p.add_argument("--expect-version")
    p.add_argument("--output", type=Path)
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        p.error("give the server command after --")
    checks = anyio.run(run, args)
    text = json.dumps({"status": "ok", "checks": checks}, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
