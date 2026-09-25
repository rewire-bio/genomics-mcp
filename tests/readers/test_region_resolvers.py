"""Readers use another provider's region resolver (the E6 htsget/EGA contract).

A fake `htsget` resolver returns a plain core `ResolvedFile` for a local, unindexed slice with
`region` set. Readers must call it with the interval, never resolve the whole file, and read the
slice without guessing an index.
"""

from __future__ import annotations

import pysam
from gm_test_support import envelope, iv

from genomics_mcp.contracts import ResolvedFile
from genomics_mcp.errors import UnsupportedError
from genomics_mcp.models import Readiness, ReadinessState


class SliceResolver:
    def __init__(self, path, index=None):
        self.path, self.index, self.calls = path, index, []

    async def resolve(self, file, ctx):
        raise UnsupportedError("whole-file resolution is not offered")

    async def stat(self, file, ctx):
        return file

    async def resolve_region(self, file, interval, ctx):
        self.calls.append(interval)
        return ResolvedFile(
            file=file,
            open_uri=str(self.path),
            local_path=self.path,
            range_capable=True,
            readiness=Readiness(state=ReadinessState.READY),
            region=interval,
        )


async def test_region_slice_without_index(service, golden, tmp_path):
    slice_bam = tmp_path / "slice.bam"
    with (
        pysam.AlignmentFile(str(golden["bam"])) as src,
        pysam.AlignmentFile(str(slice_bam), "wb", header=src.header) as out,
    ):
        for r in src.fetch("chrG", 100, 300):
            out.write(r)
    service.registry.register_resolver("htsget", SliceResolver(slice_bam), provider="test")
    res = envelope(
        await service.call(
            "get_reads",
            {
                "file": {"uri": "htsget://example/reads/X1", "format": "bam"},
                "interval": iv("chrG", 150, 160),
            },
        )
    )
    assert res["status"] == "ok", res.get("error")
    direct = envelope(
        await service.call(
            "get_reads", {"file": {"uri": str(golden["bam"])}, "interval": iv("chrG", 150, 160)}
        )
    )
    assert res["data"]["records"] == direct["data"]["records"]
    resolver = service.registry.resolver("htsget")
    assert [c.start for c in resolver.calls] == [150]


async def test_region_slice_for_variants(service, golden, tmp_path):
    sl = tmp_path / "slice.vcf.gz"
    with (
        pysam.VariantFile(str(golden["vcf"])) as src,
        pysam.VariantFile(str(sl), "wz", header=src.header) as out,
    ):
        for r in src.fetch("chrG", 0, 50):
            out.write(r)
    service.registry.register_resolver("htsget", SliceResolver(sl), provider="test")
    res = envelope(
        await service.call(
            "get_variants",
            {
                "file": {"uri": "htsget://example/variants/V1", "format": "vcf"},
                "interval": iv("chrG", 25, 45),
            },
        )
    )
    assert res["status"] == "ok", res.get("error")
    assert [r["pos"] for r in res["data"]["records"]] == [31, 41]
