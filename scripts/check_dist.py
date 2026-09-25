"""Check built distributions, then optionally install the wheel into a clean venv.

    uv build --out-dir dist
    uv export --frozen --no-dev --no-emit-project --format requirements.txt -o dist/requirements.lock.txt
    python3 scripts/check_dist.py dist                 # contents and metadata only
    python3 scripts/check_dist.py dist --install DIR   # plus a clean install into DIR

The install uses the exported lock with hashes, forces a pyBigWig source build with pinned build
requirements, and fails unless `pyBigWig.remote == 1`. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCP_NAME = "io.github.rewire-bio/genomics-mcp"
SCRIPTS = {"genomics-mcp", "rewire-genomics-mcp"}
SDIST_TOP = {
    "src",
    "tests",
    "docs",
    "packaging",
    "README.md",
    "LICENSE",
    "PRD.md",
    "config.example.toml",
    "pyproject.toml",
    "uv.lock",
    "PKG-INFO",
    ".gitignore",
}
FORBIDDEN = re.compile(
    r"(^|/)(\.work|\.venv|\.git|\.github|__pycache__|\.pytest_cache|\.ruff_cache|dist|build)(/|$)"
    r"|\.(pyc|log|pem|key|env)$|(^|/)\.env|(^|/)(credentials|\.aws|\.netrc)(/|$)|\.DS_Store$"
)
MAX_MEMBER = 2 * 1024 * 1024  # largest safe fixture is well under this
MAX_ARCHIVE = 5 * 1024 * 1024


def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def check_members(names: list[str], sizes: dict[str, int], label: str) -> list[str]:
    problems = [f"{label}: forbidden member {n}" for n in names if FORBIDDEN.search(n)]
    problems += [f"{label}: {n} is {s} bytes" for n, s in sizes.items() if s > MAX_MEMBER]
    return problems


def check_wheel(path: Path, version: str) -> list[str]:
    problems = []
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        problems += check_members(names, {i.filename: i.file_size for i in infos}, path.name)
        tops = {n.split("/")[0] for n in names}
        allowed = {"genomics_mcp", f"rewire_genomics_mcp-{version}.dist-info"}
        problems += [f"{path.name}: unexpected top-level {t}" for t in sorted(tops - allowed)]
        dist_info = f"rewire_genomics_mcp-{version}.dist-info"
        meta = Parser().parsestr(zf.read(f"{dist_info}/METADATA").decode())
        entry = zf.read(f"{dist_info}/entry_points.txt").decode()
        if f"{dist_info}/licenses/LICENSE" not in names:
            problems.append(f"{path.name}: LICENSE missing")
    problems += check_metadata(meta, version, path.name)
    found = set(re.findall(r"^([\w-]+) = genomics_mcp\.cli:main$", entry, re.M))
    if found != SCRIPTS:
        problems.append(f"{path.name}: console scripts {sorted(found)} != {sorted(SCRIPTS)}")
    return problems


def check_metadata(meta, version: str, label: str) -> list[str]:
    problems = []
    expect = {"Name": "rewire-genomics-mcp", "Version": version, "License-Expression": "MIT"}
    for key, value in expect.items():
        if meta.get(key) != value:
            problems.append(f"{label}: {key} {meta.get(key)!r} != {value!r}")
    rp = meta.get("Requires-Python", "").replace(" ", "")
    if set(rp.split(",")) != {">=3.12", "<3.13"}:
        problems.append(f"{label}: Requires-Python {rp!r}")
    if f"<!-- mcp-name: {MCP_NAME} -->" not in meta.get_payload():
        problems.append(f"{label}: README ownership marker missing from description")
    for req in meta.get_all("Requires-Dist") or []:
        if "extra ==" not in req and "==" not in req:
            problems.append(f"{label}: unpinned dependency {req}")
    return problems


def check_sdist(path: Path, version: str) -> list[str]:
    problems = []
    with tarfile.open(path) as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        prefix = f"rewire_genomics_mcp-{version}/"
        names = [m.name.removeprefix(prefix) for m in members]
        problems += check_members(
            names, dict(zip(names, (m.size for m in members), strict=True)), path.name
        )
        tops = {n.split("/")[0] for n in names}
        problems += [f"{path.name}: unexpected top-level {t}" for t in sorted(tops - SDIST_TOP)]
        for required in ("LICENSE", "README.md", "uv.lock", "packaging/build-constraints.txt"):
            if required not in names:
                problems.append(f"{path.name}: {required} missing")
        pkg = tf.extractfile(prefix + "PKG-INFO")
        assert pkg is not None
        problems += check_metadata(Parser().parsestr(pkg.read().decode()), version, path.name)
    return problems


def run(cmd: list[str], **kw) -> str:
    print("+", " ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, **kw)
    if proc.returncode:
        raise SystemExit(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-4000:]}"
        )
    return proc.stdout.strip()


def clean_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "TMPDIR", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(
        UV_NO_CONFIG="1",
        AWS_EC2_METADATA_DISABLED="true",
        AWS_CONFIG_FILE=os.devnull,
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
    )
    return env


def install(wheel: Path, lock: Path, venv: Path) -> dict:
    env = clean_env()
    constraints = ROOT / "packaging" / "build-constraints.txt"
    run(["uv", "venv", "--python", "3.12", "--no-project", str(venv)], env=env)
    py = venv / "bin" / "python"
    # `uv pip` spells it --no-binary <pkg>; uvx and uv tool use --no-binary-package.
    source_build = ["--no-binary", "pybigwig", "--build-constraints", str(constraints)]
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(py),
            "--require-hashes",
            "-r",
            str(lock),
            *source_build,
        ],
        env=env,
    )
    run(["uv", "pip", "install", "--python", str(py), "--no-deps", str(wheel)], env=env)
    remote = run([str(py), "-c", "import pyBigWig; print(pyBigWig.remote)"], env=env)
    if remote != "1":
        raise SystemExit(f"pyBigWig.remote == {remote}: built without libcurl")
    versions = {s: run([str(venv / "bin" / s), "--version"], env=env) for s in sorted(SCRIPTS)}
    frozen = run(["uv", "pip", "freeze", "--python", str(py)], env=env).splitlines()
    return {
        "python": run([str(py), "--version"], env=env),
        "pyBigWig.remote": 1,
        "scripts": versions,
        "packages": frozen,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("dist", type=Path)
    p.add_argument("--install", type=Path, metavar="VENV_DIR")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    version = project()["version"]
    wheels = sorted(args.dist.glob("*.whl"))
    sdists = sorted(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(f"expected one wheel and one sdist in {args.dist}")
    problems = check_wheel(wheels[0], version) + check_sdist(sdists[0], version)
    for f in (*wheels, *sdists):
        if f.stat().st_size > MAX_ARCHIVE:
            problems.append(f"{f.name} is {f.stat().st_size} bytes")
    report: dict = {
        "version": version,
        "sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in (*wheels, *sdists)},
        "problems": problems,
    }
    if not problems and args.install:
        lock = args.dist / "requirements.lock.txt"
        if not lock.exists():
            raise SystemExit(f"{lock} missing: run uv export first")
        report["install"] = install(wheels[0], lock, args.install)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
