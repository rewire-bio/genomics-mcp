"""Render server.json for publication from verified release facts.

    python3 scripts/render_server_json.py --out server.publish.json \\
        [--mcpb-sha256 <64 hex>] [--pypi]

The committed server.json lists only the GHCR OCI package. `--mcpb-sha256` adds the GitHub
release MCPB asset; `--pypi` adds the PyPI package, launched as `uvx <runtimeArguments> rewire-genomics-mcp==<version>`
with a forced pyBigWig source build. Pass them only after the publish workflow has checked that
the artifact is publicly downloadable and, for PyPI, that this exact launch works with
`pyBigWig.remote == 1`. No placeholders are written.
`--print-uvx-args` prints the PyPI runtime arguments, one per line.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/rewire-bio/genomics-mcp"


def uvx_runtime_arguments(version: str) -> list[str]:
    constraints = f"{REPO}/releases/download/v{version}/build-constraints.txt"
    return [
        "--python",
        "3.12",
        "--no-binary-package",
        "pybigwig",
        "--build-constraints",
        constraints,
    ]


def render(version: str, mcpb_sha256: str | None, pypi: bool) -> dict:
    server = json.loads((ROOT / "server.json").read_text())
    if server["version"] != version:
        raise SystemExit(f"server.json version {server['version']} != pyproject {version}")
    (oci,) = server["packages"]
    if oci["identifier"] != f"ghcr.io/rewire-bio/genomics-mcp:{version}":
        raise SystemExit(f"unexpected OCI identifier {oci['identifier']}")
    if mcpb_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", mcpb_sha256):
            raise SystemExit("--mcpb-sha256 must be 64 lowercase hex characters")
        server["packages"].append(
            {
                "registryType": "mcpb",
                "identifier": f"{REPO}/releases/download/v{version}/genomics-mcp-{version}.mcpb",
                "fileSha256": mcpb_sha256,
                "transport": {"type": "stdio"},
            }
        )
    if pypi:
        server["packages"].append(
            {
                "registryType": "pypi",
                "registryBaseUrl": "https://pypi.org",
                "identifier": "rewire-genomics-mcp",
                "version": version,
                "runtimeHint": "uvx",
                # uvx does not read the package's [tool.uv]: force the pyBigWig source build
                # (the Linux wheel has no remote support) with the release's pinned build pins.
                "runtimeArguments": [
                    {"type": "positional", "value": v} for v in uvx_runtime_arguments(version)
                ],
                "transport": {"type": "stdio"},
            }
        )
    return server


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", type=Path)
    p.add_argument("--mcpb-sha256")
    p.add_argument("--pypi", action="store_true")
    p.add_argument("--print-uvx-args", action="store_true")
    args = p.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if args.print_uvx_args:
        print("\n".join(uvx_runtime_arguments(version)))
        return 0
    if args.out is None:
        p.error("--out is required")
    server = render(version, args.mcpb_sha256, args.pypi)
    args.out.write_text(json.dumps(server, indent=2) + "\n")
    print(json.dumps(server, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
