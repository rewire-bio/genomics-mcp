"""Live checks for the truth-set catalogue. Opt in with GENOMICS_MCP_LIVE=1.

HEAD every pinned URL (data, index and .gzi companions) and compare sizes with the catalogue,
then read a bounded chr20 interval from the GIAB VCF through get_variants. NCBI throttles bursts
with 403, so requests are paced and retried.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from genomics_mcp.config import load_settings
from genomics_mcp.models import Compression
from genomics_mcp.service import GenomicsService
from genomics_mcp.synthetic.truth_sets import TRUTH_SETS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("GENOMICS_MCP_LIVE") != "1", reason="set GENOMICS_MCP_LIVE=1"
    ),
]


def _urls() -> list[tuple[str, int | None]]:
    seen: dict[str, int | None] = {}
    for ts in TRUTH_SETS.values():
        for tf in ts.files:
            f = tf.file
            seen[f.uri] = f.size_bytes
            if f.index_uri:
                seen.setdefault(f.index_uri, None)
            if f.compression == Compression.BGZF and f.uri.endswith((".fa.gz", ".fasta.gz")):
                seen.setdefault(f.uri + ".gzi", None)
    return sorted(seen.items())


async def _head(client: httpx.AsyncClient, url: str) -> httpx.Response:
    for attempt in range(5):
        resp = await client.head(url)
        if resp.status_code != 403:
            return resp
        await asyncio.sleep(15 * (attempt + 1))
    return resp


async def test_every_truth_url_resolves() -> None:
    failures = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for url, size in _urls():
            resp = await _head(client, url)
            length = resp.headers.get("content-length")
            print(resp.status_code, length, url)
            if resp.status_code != 200:
                failures.append(f"{resp.status_code} {url}")
            elif size is not None and length is not None and int(length) != size:
                failures.append(f"size {length} != {size} {url}")
            await asyncio.sleep(2)
    assert not failures, failures


async def test_variants_on_chr20_from_giab_vcf(tmp_path: Path) -> None:
    svc = GenomicsService(
        load_settings(env={}, overrides={"paths": {"work_dir": str(tmp_path / "work")}})
    )
    try:
        truth = await svc.call(
            "get_truth_set", {"truth_set_id": "giab-hg002-v4.2.1-grch38", "contig": "chr20"}
        )
        (bundle,) = truth.data["records"]
        vcf = next(f["file"] for f in bundle["files"] if f["role"] == "benchmark_vcf")
        interval = {**bundle["region"], "start": 10_000_000, "end": 10_010_000}
        res = await svc.call("get_variants", {"file": vcf, "interval": interval})
        assert res.status == "ok", res.error
        records = res.data["records"]
        print(len(records), records[:2])
        assert records and all(r["contig"] == "chr20" for r in records)
    finally:
        await svc.aclose()
