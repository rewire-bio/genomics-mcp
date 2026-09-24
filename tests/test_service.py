import asyncio
import sys
import types

import pytest

from genomics_mcp.errors import NotFoundError, UpstreamError
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import OperationOutput, ResultStatus
from genomics_mcp.service import GenomicsService

BAM = {"uri": "/data/a.bam"}
IV = {"contig": "chr1", "start": 100, "end": 200, "assembly": "GRCh38"}


def make(settings, register=None) -> GenomicsService:
    reg = Registry()
    if register:
        register(reg)
    return GenomicsService(settings, reg, load_providers=False)


async def test_unimplemented_operation_is_unsupported_not_empty(settings):
    svc = make(settings)
    res = await svc.call(Operation.GET_READS, {"file": BAM, "interval": IV})
    assert res.status is ResultStatus.ERROR
    assert res.error.code == "unsupported"
    assert "E4" in res.error.hint
    assert res.data is None


async def test_invalid_arguments_are_structured(settings):
    svc = make(settings)
    res = await svc.call(Operation.GET_READS, {"file": BAM, "interval": {**IV, "end": 50}})
    assert res.error.code == "invalid_input"
    assert res.error.details["errors"][0]["field"].startswith("interval")


async def test_format_checks_and_region_limit(settings):
    async def reads(req, ctx):
        return OperationOutput(data={"records": [{"name": "r1"}]})

    svc = make(settings, lambda r: r.register(Operation.GET_READS, "bam", reads, provider="t"))
    ok = await svc.call(Operation.GET_READS, {"file": BAM, "interval": IV})
    assert ok.status is ResultStatus.OK and ok.data == {"records": [{"name": "r1"}]}
    assert ok.limits.max_records == 10_000

    wrong = await svc.call(Operation.GET_READS, {"file": {"uri": "/d/x.bw"}, "interval": IV})
    assert wrong.error.code == "invalid_input"
    unknown = await svc.call(Operation.GET_READS, {"file": {"uri": "/d/x.dat"}, "interval": IV})
    assert unknown.error.code == "invalid_input"
    cram = await svc.call(Operation.GET_READS, {"file": {"uri": "/d/x.cram"}, "interval": IV})
    assert cram.error.code == "unsupported"
    big = await svc.call(
        Operation.GET_READS, {"file": BAM, "interval": {**IV, "start": 0, "end": 2_000_000}}
    )
    assert big.error.code == "budget_exceeded"


async def test_assembly_mismatch_is_rejected_without_liftover(settings):
    svc = make(settings)
    res = await svc.call(
        Operation.GET_VARIANTS,
        {"file": {"uri": "/d/x.vcf.gz", "assembly": "GRCh37"}, "interval": IV},
    )
    assert res.error.code == "invalid_input"
    assert "liftover" in res.error.message


async def test_handler_errors_are_envelopes(settings):
    async def missing(req, ctx):
        raise NotFoundError("no such dataset", source="ena")

    async def crash(req, ctx):
        raise RuntimeError("secret https://u:p@h/x")

    def register(r):
        r.register(Operation.DESCRIBE_DATASET, "ena", missing, provider="t")
        r.register(Operation.DESCRIBE_DATASET, "ega", crash, provider="t")

    svc = make(settings, register)
    res = await svc.call(Operation.DESCRIBE_DATASET, {"source": "ena", "accession": "PRJEB1"})
    assert res.error.code == "not_found" and res.error.source == "ena"
    res = await svc.call(Operation.DESCRIBE_DATASET, {"source": "EGA", "accession": "EGAD1"})
    assert res.status is ResultStatus.ERROR and res.error.code == "internal_error"
    assert "u:p" not in res.model_dump_json()
    res = await svc.call(Operation.DESCRIBE_DATASET, {"source": "geo", "accession": "GSE1"})
    assert res.error.code == "unsupported"


async def test_interactive_deadline(settings):
    settings.limits.interactive_timeout_s = 0.1

    async def slow(req, ctx):
        await asyncio.sleep(5)

    svc = make(settings, lambda r: r.register(Operation.FETCH_FILE, "default", slow, provider="t"))
    res = await svc.call(Operation.FETCH_FILE, {"file": BAM})
    assert res.error.code == "timeout"


async def test_fanout_isolates_failures(settings):
    async def ok(req, ctx):
        return OperationOutput(data={"symbol": "BRCA1"})

    async def down(req, ctx):
        raise UpstreamError("503", source="gnomad")

    async def hang(req, ctx):
        await asyncio.sleep(5)

    def register(r):
        r.register(Operation.LOOKUP_GENE, "hgnc", ok, provider="t")
        r.register(Operation.LOOKUP_GENE, "gnomad", down, provider="t")
        r.register(Operation.LOOKUP_GENE, "uniprot", hang, provider="t")

    settings.sources["uniprot"] = settings.source("uniprot").model_copy(update={"timeout_s": 0.1})
    svc = make(settings, register)
    res = await svc.call(
        Operation.LOOKUP_GENE,
        {"gene": "BRCA1", "sources": ["hgnc", "gnomad", "uniprot", "clinvar"]},
    )
    assert res.status is ResultStatus.PARTIAL
    assert res.data == {"by_source": {"hgnc": {"symbol": "BRCA1"}}}
    states = {s.source: s.state for s in res.source_status}
    assert states == {
        "hgnc": "ok",
        "gnomad": "unavailable",
        "uniprot": "timeout",
        "clinvar": "not_implemented",
    }

    res = await svc.call(Operation.LOOKUP_GENE, {"gene": "BRCA1", "sources": ["gnomad"]})
    assert res.status is ResultStatus.ERROR and res.error.code == "upstream_error"


async def test_list_sources_is_honest(settings):
    async def search(req, ctx):
        return OperationOutput(data={"records": []})

    svc = make(
        settings, lambda r: r.register(Operation.SEARCH_DATASETS, "ena", search, provider="t")
    )
    res = await svc.call(Operation.LIST_SOURCES, {})
    by_name = {s["name"]: s for s in res.data["records"]}
    assert by_name["ena"]["state"] == "ok"
    assert by_name["ega"]["state"] == "not_implemented"
    assert by_name["s3"]["state"] == "not_implemented"
    archives = await svc.call(Operation.LIST_SOURCES, {"kind": "archive"})
    assert {s["name"] for s in archives.data["records"]} == {"ega", "ena"}


def test_registry_rejects_bad_registrations():
    reg = Registry()

    async def h(req, ctx):
        return OperationOutput()

    with pytest.raises(ValueError, match="format"):
        reg.register(Operation.GET_READS, "vcf", h, provider="t")
    with pytest.raises(ValueError, match="built in"):
        reg.register(Operation.LIST_SOURCES, "x", h, provider="t")
    with pytest.raises(ValueError, match="default"):
        reg.register(Operation.FETCH_FILE, "bam", h, provider="t")
    reg.register(Operation.GET_VARIANTS, "vcf", h, provider="a")
    with pytest.raises(ValueError, match="already registered"):
        reg.register(Operation.GET_VARIANTS, "vcf", h, provider="b")


def test_provider_loading(monkeypatch):
    async def h(req, ctx):
        return OperationOutput()

    good = types.ModuleType("gm_test_provider")
    good.register = lambda r: r.register(Operation.GET_SIGNAL, "bigwig", h, provider="gm")
    monkeypatch.setitem(sys.modules, "gm_test_provider", good)
    reg = Registry()
    status = reg.load_providers({"gm_test_provider": "E5", "gm_absent_provider": "E9"})
    assert status == {"gm_test_provider": "loaded", "gm_absent_provider": "not installed (E9)"}
    assert reg.keys(Operation.GET_SIGNAL) == ["bigwig"]


def test_provider_with_broken_dependency_is_not_hidden(monkeypatch, tmp_path):
    (tmp_path / "gm_broken_provider.py").write_text("import gm_dependency_that_does_not_exist\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError):
        Registry().load_providers({"gm_broken_provider": "E4"})


async def test_fanout_timeouts_are_per_source(settings):
    async def fast_timeout_hang(req, ctx):
        await asyncio.sleep(5)

    async def slow_but_in_budget(req, ctx):
        await asyncio.sleep(0.3)
        return OperationOutput(data={"ok": True})

    def register(r):
        r.register(Operation.LOOKUP_GENE, "hgnc", fast_timeout_hang, provider="t")
        r.register(Operation.LOOKUP_GENE, "ensembl", slow_but_in_budget, provider="t")

    settings.sources["hgnc"] = settings.source("hgnc").model_copy(update={"timeout_s": 0.05})
    settings.sources["ensembl"] = settings.source("ensembl").model_copy(update={"timeout_s": 2.0})
    svc = make(settings, register)
    res = await svc.call(Operation.LOOKUP_GENE, {"gene": "TP53", "sources": ["hgnc", "ensembl"]})
    states = {s.source: s.state for s in res.source_status}
    assert states == {"hgnc": "timeout", "ensembl": "ok"}
    assert res.data == {"by_source": {"ensembl": {"ok": True}}}


async def test_fanout_source_timeout_still_bounded_by_call_deadline(settings):
    settings.limits.interactive_timeout_s = 0.1

    async def slow(req, ctx):
        await asyncio.sleep(5)

    settings.sources["hgnc"] = settings.source("hgnc").model_copy(update={"timeout_s": 60})
    svc = make(settings, lambda r: r.register(Operation.LOOKUP_GENE, "hgnc", slow, provider="t"))
    res = await svc.call(Operation.LOOKUP_GENE, {"gene": "TP53"})
    assert res.status is ResultStatus.ERROR and res.error.code == "timeout"
