"""Build the MCPB bundle: a Node launcher for the digest-pinned GHCR image.

    python3 scripts/build_mcpb.py --image ghcr.io/rewire-bio/genomics-mcp@sha256:<digest> --out dist

Stages manifest.json, server/launcher.cjs, README.md and LICENSE only, then validates, packs,
unpacks and re-validates with the official MCPB CLI, and prints the SHA-256. `--test-image`
builds an unpinned bundle for CI smoke tests; it is named *.test.mcpb and must not be released.
Standard library only; needs Node.js >= 20 and npx.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"
PINNED = re.compile(r"^ghcr\.io/rewire-bio/genomics-mcp@sha256:[0-9a-f]{64}$")

BUNDLE_README = """# Genomics MCP bundle

Runs `{image}` (linux/amd64) with Docker over stdio.

- Requires Node.js 20+ and a running Docker daemon that can run linux/amd64 images.
- Mounts only the chosen data directory (read-only, at /data) and a separate workspace (/work).
  The home directory and cloud credentials are never mounted or passed.
- Public archive and reference sources are queried over the network.

Source, documentation and licence: https://github.com/rewire-bio/genomics-mcp
"""


def mcpb(*args: str) -> str:
    cmd = ["npx", "--yes", MCPB_CLI, *args]
    print("+", " ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode:
        raise SystemExit(f"mcpb {args[0]} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def stage(dest: Path, image: str, version: str) -> None:
    manifest = json.loads((ROOT / "packaging/mcpb/manifest.json").read_text())
    if manifest["version"] != version:
        raise SystemExit(f"manifest version {manifest['version']} != pyproject {version}")
    launcher = (ROOT / "packaging/mcpb/server/launcher.cjs").read_text()
    if launcher.count('"@IMAGE@"') != 1:
        raise SystemExit("launcher image placeholder not found exactly once")
    (dest / "server").mkdir(parents=True)
    (dest / "server/launcher.cjs").write_text(launcher.replace('"@IMAGE@"', json.dumps(image)))
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (dest / "README.md").write_text(BUNDLE_README.format(image=image))
    shutil.copy(ROOT / "LICENSE", dest / "LICENSE")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--image", help="ghcr.io/rewire-bio/genomics-mcp@sha256:<64 hex>")
    group.add_argument("--test-image", help="any local image reference (CI only, not releasable)")
    p.add_argument("--out", type=Path, default=ROOT / "dist")
    args = p.parse_args()
    if args.image and not PINNED.match(args.image):
        raise SystemExit(f"--image must match {PINNED.pattern}")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    image = args.image or args.test_image
    suffix = ".mcpb" if args.image else ".test.mcpb"
    args.out.mkdir(parents=True, exist_ok=True)
    bundle = (args.out / f"genomics-mcp-{version}{suffix}").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        staged, unpacked = Path(tmp) / "stage", Path(tmp) / "unpacked"
        stage(staged, image, version)
        mcpb("validate", str(staged / "manifest.json"))
        bundle.unlink(missing_ok=True)
        mcpb("pack", str(staged), str(bundle))
        mcpb("unpack", str(bundle), str(unpacked))
        mcpb("validate", str(unpacked / "manifest.json"))
        members = sorted(str(f.relative_to(unpacked)) for f in unpacked.rglob("*") if f.is_file())
        expected = ["LICENSE", "README.md", "manifest.json", "server/launcher.cjs"]
        if members != expected:
            raise SystemExit(f"unexpected bundle contents: {members}")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    report = {
        "bundle": str(bundle),
        "sha256": digest,
        "image": image,
        "releasable": bool(args.image),
    }
    (bundle.parent / f"{bundle.name}.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
