"""OperationContext.resolve_file region contract (EGA/htsget never resolve whole files)."""

import pytest

from genomics_mcp.context import OperationContext
from genomics_mcp.contracts import RegionFileResolver, ResolvedFile
from genomics_mcp.errors import GenomicsError
from genomics_mcp.models import FileRef, Interval
from genomics_mcp.public import Deadline, PublicHttpClient
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import EffectiveLimits

IV = Interval(contig="chr1", start=100, end=200, assembly="GRCh38")


class WholeFileOnly:
    def __init__(self):
        self.calls = []

    async def resolve(self, file, ctx):
        self.calls.append("resolve")
        return ResolvedFile(file=file, open_uri="https://h/whole.bam")

    async def stat(self, file, ctx):
        return file


class RegionCapable(WholeFileOnly):
    def __init__(self, region=IV):
        super().__init__()
        self.region = region

    async def resolve_region(self, file, interval, ctx):
        self.calls.append(("resolve_region", interval))
        return ResolvedFile(file=file, open_uri="https://h/slice.bam", region=self.region)


def ctx_with(settings, scheme, resolver) -> OperationContext:
    reg = Registry()
    reg.register_resolver(scheme, resolver, provider="t")
    return OperationContext(
        operation=Operation.GET_READS,
        settings=settings,
        limits=EffectiveLimits.build(settings.limits),
        deadline=Deadline(5),
        registry=reg,
        http=PublicHttpClient(settings),
        request_id="t",
    )


async def test_region_resolver_used_when_interval_given(settings):
    r = RegionCapable()
    assert isinstance(r, RegionFileResolver)
    ctx = ctx_with(settings, "ega", r)
    f = FileRef(uri="ega://EGAF00001775036")
    got = await ctx.resolve_file(f, interval=IV)
    assert got.region == IV and r.calls == [("resolve_region", IV)]
    await ctx.resolve_file(f)  # explicit whole-file resolution (e.g. fetch_file) still allowed
    assert r.calls[-1] == "resolve"


async def test_region_only_scheme_never_falls_back_to_whole_file(settings):
    r = WholeFileOnly()
    assert not isinstance(r, RegionFileResolver)
    ctx = ctx_with(settings, "htsget", r)
    with pytest.raises(GenomicsError) as exc:
        await ctx.resolve_file(FileRef(uri="htsget://h/reads/x"), interval=IV)
    assert exc.value.info.code == "unsupported"
    assert r.calls == []


async def test_region_only_resolver_must_cover_request(settings):
    short = Interval(contig="chr1", start=150, end=200, assembly="GRCh38")
    ctx = ctx_with(settings, "ega", RegionCapable(region=short))
    with pytest.raises(GenomicsError) as exc:
        await ctx.resolve_file(FileRef(uri="ega://EGAF1"), interval=IV)
    assert exc.value.info.code == "upstream_error"


async def test_ordinary_schemes_use_resolve_with_interval(settings, data_root):
    r = WholeFileOnly()
    ctx = ctx_with(settings, "file", r)
    await ctx.resolve_file(FileRef(uri=str(data_root / "a.bam")), interval=IV)
    assert r.calls == ["resolve"]
