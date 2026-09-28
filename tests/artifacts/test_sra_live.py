"""convert_sra_run with the real SRA Toolkit on small public runs.

Opt in: GENOMICS_MCP_NETWORK_TESTS=1 and GENOMICS_MCP_SRA_TOOLKIT_DIR=<dir with prefetch and
fasterq-dump>. Counts are checked against ENA's run report, not only the toolkit's own.

- test_real_toolkit: prefetch and fasterq-dump, exactly as users run them.
- test_real_conversion_of_ncbi_run_files: the real fasterq-dump and verifier on run files taken
  anonymously from NCBI's public SRA bucket over HTTPS; only prefetch is replaced by a copier.
  This separates conversion evidence from NCBI's accession locator, which prefetch needs.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from gm_test_support import envelope, make_settings

from genomics_mcp.service import GenomicsService

TOOLKIT = os.environ.get("GENOMICS_MCP_SRA_TOOLKIT_DIR")
pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("GENOMICS_MCP_NETWORK_TESTS") != "1" or not TOOLKIT,
        reason="set GENOMICS_MCP_NETWORK_TESTS=1 and GENOMICS_MCP_SRA_TOOLKIT_DIR",
    ),
]
RUNS = {"SRR13450355": "paired", "SRR24157174": "single"}
ODP = "https://sra-pub-run-odp.s3.amazonaws.com/sra/{acc}/{acc}"
COPIER = """#!{python}
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print(sys.argv[0] + " : 3.4.1")
    raise SystemExit(0)
acc, out = args[0], Path(args[args.index("--output-directory") + 1])
(out / acc).mkdir(parents=True, exist_ok=True)
shutil.copy({cache!r} + "/" + acc, out / acc / (acc + ".sra"))
"""


def ena_counts(acc: str) -> tuple[int, int]:
    q = urllib.parse.urlencode(
        {"accession": acc, "result": "read_run", "fields": "read_count,base_count"}
    )
    url = f"https://www.ebi.ac.uk/ena/portal/api/filereport?{q}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        head, row = resp.read().decode().splitlines()[:2]
    values = dict(zip(head.split("\t"), row.split("\t"), strict=True))
    return int(values["read_count"]), int(values["base_count"])


async def convert_all(svc: GenomicsService) -> dict[str, dict]:
    out = {}
    for acc in RUNS:
        res = envelope(
            await svc.call("convert_sra_run", {"accession": acc, "budget_bytes": 16 << 20})
        )
        assert res["status"] == "ok", res["error"]
        tid = res["data"]["transfer"]["transfer_id"]
        for _ in range(600):
            res = envelope(await svc.call("get_transfer_status", {"transfer_id": tid}))
            if res["data"]["transfer"]["state"] in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)
        assert res["data"]["transfer"]["state"] == "completed", res["data"]["transfer"]["error"]
        out[acc] = res["data"]
    return out


def check_against_archive(results: dict[str, dict]) -> None:
    for acc, data in results.items():
        prep = data["preparation"]
        roles = {f["role"]: f for f in prep["fastq"]}
        spots, bases = ena_counts(acc)
        if RUNS[acc] == "paired":
            assert set(roles) >= {"mate_1", "mate_2"}
        else:
            assert set(roles) == {"unpaired"}
        pairs = roles.get("mate_1", {}).get("reads", 0)
        assert pairs + roles.get("unpaired", {}).get("reads", 0) == spots
        assert sum(f["bases"] for f in prep["fastq"]) == bases
        assert prep["verification"]["all_biological_bases_written"] is True
        assert prep["sequence_table"] == "SEQUENCE"
        for art in data["artifacts"]:
            sums = {c["algorithm"]: c["value"] for c in art["checksums"]}
            assert sums["sha256"] == hashlib.sha256(Path(art["path"]).read_bytes()).hexdigest()


async def test_real_toolkit(tmp_path):
    svc = GenomicsService(make_settings(tmp_path, [], sra_toolkit={"bin_dir": TOOLKIT}))
    try:
        check_against_archive(await convert_all(svc))
    finally:
        await svc.aclose()


async def test_real_conversion_of_ncbi_run_files(tmp_path):
    cache = tmp_path / "odp"
    cache.mkdir()
    for acc in RUNS:
        urllib.request.urlretrieve(ODP.format(acc=acc), cache / acc)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fasterq-dump").symlink_to(Path(TOOLKIT) / "fasterq-dump")
    copier = bin_dir / "prefetch"
    copier.write_text(COPIER.format(python=sys.executable, cache=str(cache)))
    copier.chmod(0o755)
    svc = GenomicsService(make_settings(tmp_path, [], sra_toolkit={"bin_dir": str(bin_dir)}))
    try:
        check_against_archive(await convert_all(svc))
    finally:
        await svc.aclose()
