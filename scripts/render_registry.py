"""Render registry submission files for one released commit.

    python3 scripts/render_registry.py --commit <40-hex release commit> --out build/registry

Writes:
  biocontext/servers/rewire-bio-genomics-mcp/{meta.yaml,mcp.json}   (biocontext-ai/registry)
  docker/servers/genomics-mcp/server.yaml                           (docker/mcp-registry)

Templates live in registry/. The commit must be the tagged release commit that is already public.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def render(commit: str, out: Path) -> list[Path]:
    if not COMMIT.match(commit):
        raise SystemExit("--commit must be a 40-character lowercase SHA-1")
    bio = out / "biocontext/servers/rewire-bio-genomics-mcp"
    docker = out / "docker/servers/genomics-mcp"
    bio.mkdir(parents=True, exist_ok=True)
    docker.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "registry/biocontext/meta.yaml", bio / "meta.yaml")
    mcp = (ROOT / "registry/biocontext/mcp.json.in").read_text().replace("@COMMIT@", commit)
    json.loads(mcp)
    (bio / "mcp.json").write_text(mcp)
    yaml = (ROOT / "registry/docker/server.yaml.in").read_text().replace("@COMMIT@", commit)
    (docker / "server.yaml").write_text(yaml)
    written = [bio / "meta.yaml", bio / "mcp.json", docker / "server.yaml"]
    for path in written:
        if "@" in re.sub(
            r"@(context|type|id)\b|@\{|[\w.-]+@[0-9a-f]{40}|github\.com/[\w./-]+@",
            "",
            path.read_text(),
        ):
            raise SystemExit(f"unrendered placeholder in {path}")
    return written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--commit", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    for path in render(args.commit, args.out):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
