"""Release metadata, packaging and workflow invariants. No network, no Docker."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
VERSION = PROJECT["project"]["version"]
NAME = "io.github.rewire-bio/genomics-mcp"
sys.path.insert(0, str(ROOT / "scripts"))


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_build_constraints_match_project_policy():
    lines = (ROOT / "packaging/build-constraints.txt").read_text().splitlines()
    pins = sorted(line for line in lines if line and not line.startswith("#"))
    assert pins == sorted(PROJECT["tool"]["uv"]["build-constraint-dependencies"])
    assert PROJECT["tool"]["uv"]["no-binary-package"] == ["pybigwig"]


def test_console_scripts_include_distribution_name_alias():
    assert PROJECT["project"]["scripts"] == {
        "genomics-mcp": "genomics_mcp.cli:main",
        "rewire-genomics-mcp": "genomics_mcp.cli:main",
    }


def test_readme_has_registry_ownership_marker():
    assert f"<!-- mcp-name: {NAME} -->" in (ROOT / "README.md").read_text()


def test_server_json_matches_package():
    server = load("server.json")
    assert server["name"] == NAME and server["version"] == VERSION
    assert len(server["description"]) <= 100
    assert server["repository"] == {
        "url": "https://github.com/rewire-bio/genomics-mcp",
        "source": "github",
    }
    assert server["packages"] == [
        {
            "registryType": "oci",
            "identifier": f"ghcr.io/rewire-bio/genomics-mcp:{VERSION}",
            "transport": {"type": "stdio"},
        }
    ]
    assert "remotes" not in server


def test_rendered_server_json_variants():
    from render_server_json import render

    full = render(VERSION, "a" * 64, pypi=True)
    kinds = [p["registryType"] for p in full["packages"]]
    assert kinds == ["oci", "mcpb", "pypi"]
    mcpb = full["packages"][1]
    assert mcpb["identifier"].endswith(f"/releases/download/v{VERSION}/genomics-mcp-{VERSION}.mcpb")
    assert "registryBaseUrl" not in mcpb
    assert full["packages"][2]["identifier"] == "rewire-genomics-mcp"
    assert full["packages"][2]["version"] == VERSION
    assert render(VERSION, None, pypi=False) == load("server.json")
    with pytest.raises(SystemExit):
        render(VERSION, "REPLACE_WITH_SHA", pypi=False)


def test_glama_json_live_schema_shape():
    assert load("glama.json") == {
        "$schema": "https://glama.ai/mcp/schemas/server.json",
        "maintainers": ["timini"],
    }


def test_dockerfile_invariants():
    text = (ROOT / "Dockerfile").read_text()
    for image in re.findall(r"^ARG \w+_IMAGE=(\S+)$", text, re.M):
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), image
    assert 'io.modelcontextprotocol.server.name="io.github.rewire-bio/genomics-mcp"' in text
    assert 'org.opencontainers.image.source="https://github.com/rewire-bio/genomics-mcp"' in text
    assert re.search(r"^USER genomics:genomics$", text, re.M)
    assert 'ENTRYPOINT ["genomics-mcp"]' in text and 'CMD ["--transport", "stdio"]' in text
    assert "uv sync --locked --no-dev" in text and "pyBigWig.remote == 1" in text
    assert "AWS_ACCESS_KEY_ID" not in text and "AWS_SECRET" not in text
    allowed = {
        line.strip("!/")
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.startswith("!")
    }
    for line in re.findall(r"^COPY (?!--from)(.+) \S+$", text, re.M):
        for src in line.split():
            assert src.strip("/") in allowed, src


def test_workflow_actions_are_pinned_and_permissions_scoped():
    flows = {p.name: p.read_text() for p in (ROOT / ".github/workflows").glob("*.yml")}
    for name, text in flows.items():
        for ref in re.findall(r"uses:\s*(\S+)", text):
            assert re.search(r"@[0-9a-f]{40}$", ref), f"{name}: {ref}"
        assert "permissions:\n  contents: read" in text, name
    assert "id-token: write" not in flows["release.yml"]
    pypi = flows["publish-pypi.yml"]
    assert "environment: pypi" in pypi and "id-token: write" in pypi
    assert "password" not in pypi and "PYPI_TOKEN" not in pypi
    registry = flows["publish-mcp-registry.yml"]
    assert 'mcp-publisher" login github-oidc' in registry
    assert "--docker-config" in registry and "include_pypi" in registry


def test_mcpb_manifest_and_launcher():
    manifest = load("packaging/mcpb/manifest.json")
    assert manifest["version"] == VERSION and manifest["manifest_version"] == "0.3"
    assert manifest["server"]["type"] == "node"
    assert set(manifest["user_config"]) == {"docker_path", "data_root", "work_dir"}
    launcher = (ROOT / "packaging/mcpb/server/launcher.cjs").read_text()
    assert launcher.count('"@IMAGE@"') == 1 and "shell: false" in launcher
    assert "target=/data,readonly" in launcher


def test_registry_templates_render_without_placeholders(tmp_path):
    from render_registry import render

    commit = "0123456789abcdef0123456789abcdef01234567"
    written = render(commit, tmp_path)
    mcp = json.loads(written[1].read_text())
    meta = written[0].read_text()
    identifier = re.search(r"^identifier: (\S+)$", meta, re.M).group(1)
    assert list(mcp["mcpServers"]) == [identifier] == ["rewire-bio/genomics-mcp"]
    assert written[0].parent.name == identifier.replace("/", "-")
    assert f"commit: {commit}" in written[2].read_text()
    assert "@COMMIT@" not in "".join(p.read_text() for p in written)
    with pytest.raises(SystemExit):
        render("main", tmp_path / "x")


def test_scrub_removes_signed_values_secrets_and_home():
    from _mcpclient import DUMMY_AWS, scrub

    home = str(Path.home())
    raw = {
        "u": "https://b.s3.amazonaws.com/f?X-Amz-Signature=abc123&X-Amz-Credential=zz",
        "s": DUMMY_AWS["AWS_SECRET_ACCESS_KEY"],
        "p": f"{home}/data/x.bam",
        "r": "/tmp/run-1/work/a",
    }
    out = json.dumps(scrub(raw, {"/tmp/run-1": "$RUN"}))
    assert "abc123" not in out and "zz" not in out
    assert DUMMY_AWS["AWS_SECRET_ACCESS_KEY"] not in out
    assert home not in out and "~/data/x.bam" in out and "$RUN/work/a" in out


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_built_distributions_contain_only_allowed_files(tmp_path):
    subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path), "-q"], cwd=ROOT, check=True, timeout=300
    )
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_dist.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(proc.stdout)
    assert proc.returncode == 0 and report["problems"] == [], report["problems"]


def test_release_notes_and_ledger_are_consistent():
    assert (ROOT / f"docs/release-notes/{VERSION}.md").is_file()
    ledger = load("registry/ledger.json")
    assert ledger["registry_name"] == NAME and ledger["version"] == VERSION
    for route in ledger["routes"]:
        # A route can only be approved after it was published, and published after submission
        # (GitHub/GHCR/PyPI are published directly by workflows, without a submission step).
        if route.get("approved"):
            assert route.get("published"), route["route"]
        if route.get("published") and route["route"] not in {"github_release", "ghcr_oci", "pypi"}:
            assert route.get("submitted"), route["route"]
