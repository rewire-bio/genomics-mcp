"""MCPB Node launcher: argument construction, path refusals and a real MCP round trip.

A fake `docker` executable records the arguments and environment it receives, then runs this
package's server with the host directories the launcher mounted. Real container execution of
the bundle runs in CI (package.yml and release.yml).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "packaging/mcpb/server/launcher.cjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")
TEST_IMAGE = "genomics-mcp:launcher-test"
DUMMY = {
    "AWS_ACCESS_KEY_ID": "AKIADUMMYLAUNCHER000",
    "AWS_SECRET_ACCESS_KEY": "dummy-launcher-secret",
}

FAKE_DOCKER = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.path.join(os.environ["DOCKER_CONFIG"], "call.json"), "w") as fh:
    json.dump({{"args": args, "env": dict(os.environ)}}, fh)
mounts = dict(
    (dict(kv.split("=", 1) for kv in args[i + 1].split(",") if "=" in kv)["target"],
     dict(kv.split("=", 1) for kv in args[i + 1].split(",") if "=" in kv)["source"])
    for i, a in enumerate(args) if a == "--mount"
)
env = {{"PATH": os.environ["PATH"], "GENOMICS_MCP_ALLOWED_ROOTS": mounts["/data"],
       "GENOMICS_MCP_WORK_DIR": mounts["/work"]}}
os.execve(sys.executable, [sys.executable, "-m", "genomics_mcp", *args[args.index("{image}") + 1:]], env)
"""


@pytest.fixture
def setup(tmp_path):
    docker = tmp_path / "bin" / "docker"
    docker.parent.mkdir()
    docker.write_text(FAKE_DOCKER.format(python=sys.executable, image=TEST_IMAGE))
    docker.chmod(0o755)
    data, work, record = tmp_path / "data", tmp_path / "work", tmp_path / "record"
    for d in (data, work, record):
        d.mkdir()
    (data / "ref.fa").write_text(">chrL\nACGTACGTTTGGCCAA\n")
    (data / "ref.fa.fai").write_text("chrL\t16\t6\t16\t17\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "DOCKER_CONFIG": str(record),
        "GENOMICS_MCP_LAUNCHER_TEST_IMAGE": TEST_IMAGE,
        "HTTPS_PROXY": "http://127.0.0.1:9",
        **DUMMY,
    }
    return docker, data, work, record, env


def run_launcher(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [NODE, str(LAUNCHER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


async def test_launcher_round_trip_and_arguments(setup, tmp_path):
    docker, data, work, record, env = setup
    params = StdioServerParameters(
        command=NODE, args=[str(LAUNCHER), str(docker), str(data), str(work)], env=env
    )
    with (tmp_path / "launcher.log").open("w") as errlog:
        async with Client(stdio_client(params, errlog=errlog), mode="legacy") as client:
            assert len((await client.list_tools()).tools) == 23
            res = await client.call_tool(
                "get_sequence",
                {
                    "file": {"uri": str(data / "ref.fa")},
                    "interval": {"contig": "chrL", "start": 4, "end": 10, "assembly": "test"},
                },
            )
            assert res.structured_content["data"]["records"][0]["sequence"] == "ACGTTT"

    call = json.loads((record / "call.json").read_text())
    args = call["args"]
    assert args[:3] == ["run", "--rm", "-i"]
    assert f"type=bind,source={data.resolve()},target=/data,readonly" in args
    assert f"type=bind,source={work.resolve()},target=/work" in args
    assert "--read-only" in args and "ALL" in args and "no-new-privileges" in args
    assert args[-3:] == [TEST_IMAGE, "--transport", "stdio"]
    assert args.count("--mount") == 2  # nothing else, in particular no home directory
    for pair in ("AWS_EC2_METADATA_DISABLED=true", "AWS_CONFIG_FILE=/dev/null"):
        assert pair in args
    # The Docker CLI never sees ambient AWS credentials or proxies from the host application.
    assert not {k for k in call["env"] if k.startswith("AWS_") or "PROXY" in k.upper()}
    assert not any(v in json.dumps(args) for v in DUMMY.values())


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("home", "home directory"),
        ("nested", "separate"),
        ("relative_docker", "absolute path"),
        ("missing", "does not exist"),
        ("comma", "unsupported character"),
    ],
)
def test_launcher_refuses_unsafe_paths(setup, tmp_path, case, message):
    docker, data, work, _, env = setup
    args = [str(docker), str(data), str(work)]
    if case == "home":
        args[1] = str(Path.home())
    elif case == "nested":
        (data / "inner").mkdir()
        args[2] = str(data / "inner")
    elif case == "relative_docker":
        args[0] = "docker"
    elif case == "missing":
        args[1] = str(tmp_path / "nope")
    elif case == "comma":
        odd = tmp_path / "a,b"
        odd.mkdir()
        args[1] = str(odd)
    proc = run_launcher(args, env)
    assert proc.returncode == 2 and message in proc.stderr, proc.stderr


def test_unpinned_bundle_refuses_to_run(setup):
    docker, data, work, _, env = setup
    env = {k: v for k, v in env.items() if k != "GENOMICS_MCP_LAUNCHER_TEST_IMAGE"}
    proc = run_launcher([str(docker), str(data), str(work)], env)
    assert proc.returncode == 2 and "digest-pinned" in proc.stderr
