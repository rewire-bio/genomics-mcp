"""Check a local container image's config: registry ownership labels, non-root user, stdio default.

    python3 scripts/check_image.py genomics-mcp:ci
    python3 scripts/check_image.py ghcr.io/rewire-bio/genomics-mcp:0.1.0 --docker-config EMPTY_DIR

The official MCP Registry reads `io.modelcontextprotocol.server.name` from the image config
labels, so they are checked there (not only on an index annotation). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_LABELS = {
    "io.modelcontextprotocol.server.name": "io.github.rewire-bio/genomics-mcp",
    "org.opencontainers.image.source": "https://github.com/rewire-bio/genomics-mcp",
    "org.opencontainers.image.licenses": "MIT",
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("image")
    p.add_argument(
        "--docker-config", help="Docker config dir (an empty one proves anonymous access)"
    )
    p.add_argument("--pull", action="store_true", help="pull linux/amd64 first")
    args = p.parse_args()
    docker = ["docker"] + (["--config", args.docker_config] if args.docker_config else [])
    if args.pull:
        subprocess.run([*docker, "pull", "--platform", "linux/amd64", args.image], check=True)
    out = subprocess.run(
        [*docker, "image", "inspect", args.image], check=True, capture_output=True, text=True
    ).stdout
    (info,) = json.loads(out)
    config = info["Config"]
    labels = config.get("Labels") or {}
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    problems = [
        f"label {k}={labels.get(k)!r}, expected {v!r}"
        for k, v in {**EXPECTED_LABELS, "org.opencontainers.image.version": version}.items()
        if labels.get(k) != v
    ]
    user = config.get("User") or ""
    if user in {"", "0", "root"} or user.startswith(("0:", "root:")):
        problems.append(f"image runs as root (User={user!r})")
    if config.get("Entrypoint") != ["genomics-mcp"]:
        problems.append(f"Entrypoint {config.get('Entrypoint')!r}")
    if config.get("Cmd") != ["--transport", "stdio"]:
        problems.append(f"Cmd {config.get('Cmd')!r}")
    if info.get("Architecture") != "amd64" or info.get("Os") != "linux":
        problems.append(f"platform {info.get('Os')}/{info.get('Architecture')}")
    env = dict(e.split("=", 1) for e in config.get("Env") or [])
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        if key in env:
            problems.append(f"image sets {key}")
    report = {
        "image": args.image,
        "id": info["Id"],
        "repo_digests": info.get("RepoDigests", []),
        "user": user,
        "labels": {k: labels.get(k) for k in sorted(labels)},
        "problems": problems,
    }
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
