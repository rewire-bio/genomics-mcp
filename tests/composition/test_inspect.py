"""inspect_locus through GenomicsService with real synthetic BAM/VCF/FASTA files."""

from __future__ import annotations

import asyncio
import time

from genomics_mcp.errors import ErrorCode
from genomics_mcp.models import SourceState
from genomics_mcp.registry import Operation
from genomics_mcp.result import OperationOutput, ResultStatus

from .conftest import configured, iv, ref
from .fixtures import COHORT_ROWS, REF


def records(body, component):
    return [r["record"] for r in body["data"]["records"] if r["component"] == component]


def comp(body, cid):
    return next(c for c in body["data"]["components"] if c["id"] == cid)


async def test_bam_vcf_fasta_composition(make_service, fx, spy):
    svc = make_service()
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "reference": ref(fx.fasta),
            "files": [ref(fx.deep_bam), ref(fx.cohort_vcf)],
        },
    )
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.OK, body["errors"]
    assert spy.requests == []  # nothing requested, nothing external

    (seq,) = records(body, "reference.sequence")
    assert seq["sequence"].upper() == REF[100:200]

    reads = records(body, "f0.reads")
    assert len(reads) == len([s for s in fx.deep_starts if s < 200 and s + 50 > 100])
    assert all("sequence" not in r and "base_qualities" not in r for r in reads)

    cov = comp(body, "f0.coverage")
    depth = fx.depth("deep", 100, 200)
    assert cov["summary"]["mean"] == sum(depth) / len(depth)
    assert cov["summary"]["max"] == max(depth) and cov["complete"] is True
    bins = records(body, "f0.coverage")
    assert bins[0]["start"] == 100 and bins[-1]["end"] == 200

    variants = records(body, "f1.variants")
    assert [v["pos"] for v in variants] == sorted(COHORT_ROWS)
    by_pos = {v["pos"]: v for v in variants}
    assert by_pos[131]["samples"]["S1"]["genotype"]["phased"] is True
    assert by_pos[131]["samples"]["S2"]["genotype"]["alleles"] == [None, None]
    assert by_pos[141]["samples"]["S1"]["genotype"]["alleles"] == [1, 2]
    assert by_pos[151]["samples"]["S1"]["genotype"]["ploidy"] == 1

    # identity is caller-asserted here (no header assembly), and said so per component
    for cid in ("reference.sequence", "f0.reads", "f0.coverage", "f1.variants"):
        entry = comp(body, cid)
        assert entry["status"] == "ok" and entry["provenance"]
        assert entry["assembly"]["status"] == "caller_asserted"
    assert len(body["provenance"]) == 4
    assert body["data"]["consistency"]["consistent"] is True
    assert body["data"]["annotation"]["requested"] is False


async def test_wrong_assembly_rejected_before_any_read(make_service, fx):
    calls = []

    async def spy_handler(req, ctx):
        calls.append(req)
        return OperationOutput(data={"records": []})

    svc = make_service(extra=lambda r: None, readers=False)
    for op in (Operation.GET_READS, Operation.GET_COVERAGE):
        svc.registry.register(op, "bam", spy_handler, provider="t")
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {"interval": iv(100, 200), "files": [ref(fx.deep_bam, assembly="GRCh37")]},
    )
    assert res.status is ResultStatus.ERROR and res.error.code == ErrorCode.INVALID_INPUT
    assert "no liftover" in res.error.message
    assert calls == []


async def test_unsupported_missing_and_unindexed_files_are_explicit(make_service, fx, data_root):
    unindexed = data_root / "noindex.bam"
    unindexed.write_bytes(fx.deep_bam.read_bytes())
    svc = make_service()
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "files": [
                ref(fx.deep_bam),
                ref(data_root / "reads.fastq.gz"),
                ref(data_root / "missing.vcf.gz"),
                ref(unindexed),
                {"uri": str(data_root / "mystery.dat")},
            ],
        },
    )
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.PARTIAL
    codes = {e["source"]: e["code"] for e in body["errors"]}
    assert codes["f1"] == "unsupported"
    assert codes["f2.variants"] == "not_found"
    assert codes["f3.reads"] == "preparation_required"
    assert codes["f4"] == "invalid_input"
    assert comp(body, "f0.reads")["status"] == "ok"
    assert records(body, "f0.reads")


async def test_all_files_failing_is_an_error_not_empty_success(make_service, data_root):
    svc = make_service()
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {"interval": iv(100, 200), "files": [ref(data_root / "missing.bam")]},
    )
    assert res.status is ResultStatus.ERROR
    assert res.errors and res.data is None


async def test_stalled_file_times_out_alone(make_service, fx, tmp_path, data_root):
    cfg = configured(tmp_path, data_root, sources={"slowarchive": {"timeout_s": 0.3}})
    started = asyncio.Event()

    def extra(reg):
        orig = reg.handler(Operation.GET_VARIANTS, "vcf").handler

        async def maybe_stall(req, ctx):
            if req.file.source == "slowarchive":
                started.set()
                await asyncio.sleep(3600)
            return await orig(req, ctx)

        reg._handlers[Operation.GET_VARIANTS]["vcf"] = reg._handlers[Operation.GET_VARIANTS][
            "vcf"
        ].__class__(Operation.GET_VARIANTS, "vcf", maybe_stall, "t")

    svc = make_service(cfg, extra)
    t0 = time.monotonic()
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "files": [ref(fx.second_vcf, source="slowarchive"), ref(fx.cohort_vcf)],
        },
    )
    assert time.monotonic() - t0 < 5
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.PARTIAL
    assert comp(body, "f0.variants")["status"] == "timeout"
    assert comp(body, "f1.variants")["status"] == "ok"
    assert len(records(body, "f1.variants")) == len(COHORT_ROWS)


async def test_overall_deadline_keeps_completed_components(make_service, fx, tmp_path, data_root):
    cfg = configured(tmp_path, data_root, limits={"interactive_timeout_s": 1.5})

    async def stall(req, ctx):
        await asyncio.sleep(3600)

    def extra(reg):
        reg.register(Operation.GET_SIGNAL, "bigwig", stall, provider="t")

    svc = make_service(cfg, extra, readers=False)
    from .doubles import real_providers, register_doubles

    if real_providers():
        svc.registry.load_providers({"genomics_mcp.storage": "E2", "genomics_mcp.readers": "E4"})
    else:
        register_doubles_no_signal(svc.registry, register_doubles)
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {"interval": iv(100, 200), "files": [ref(fx.deep_bam), ref(fx.root / "x.bw")]},
    )
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.PARTIAL, body
    assert comp(body, "f1.signal")["status"] == "timeout"
    assert records(body, "f0.reads")


def register_doubles_no_signal(reg, register_doubles):
    from genomics_mcp.registry import Registry

    tmp = Registry()
    register_doubles(tmp)
    for op in (Operation.GET_READS, Operation.GET_COVERAGE):
        reg.register(op, "bam", tmp.handler(op, "bam").handler, provider="test-double")


async def test_source_disablement_is_honoured(make_service, fx, tmp_path, data_root, spy):
    cfg = configured(
        tmp_path, data_root, sources={"ena": {"enabled": False}, "clinvar": {"enabled": False}}
    )
    svc = make_service(cfg)
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "files": [
                ref(fx.cohort_vcf, source="ena", visibility="public"),
                ref(fx.second_vcf, visibility="public"),
            ],
            "reference_sources": ["clinvar"],
        },
    )
    body = res.model_dump(mode="json")
    assert comp(body, "f0.variants")["status"] == "disabled"
    assert not records(body, "f0.variants")
    assert records(body, "f1.variants")
    states = {s["source"]: s["state"] for s in body["source_status"]}
    assert states["clinvar"] == "disabled"
    assert spy.requests == []


async def test_lowered_limits_trim_evenly_and_fit_response_cap(make_service, fx):
    svc = make_service()
    files = [ref(fx.deep_bam), ref(fx.cohort_vcf)]
    res = await svc.call(
        Operation.INSPECT_LOCUS, {"interval": iv(100, 200), "files": files, "max_records": 6}
    )
    body = res.model_dump(mode="json")
    assert len(body["data"]["records"]) <= 6
    assert body["truncation"] is not None
    assert {r["component"] for r in body["data"]["records"]} == {
        "f0.reads",
        "f0.coverage",
        "f1.variants",
    }
    assert comp(body, "f0.reads")["truncation"]["reason"] == "max_records"

    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {"interval": iv(100, 200), "files": files, "max_response_bytes": 12_000},
    )
    body = res.model_dump(mode="json")
    assert res.json_size() <= 12_000
    assert res.status is not ResultStatus.ERROR
    assert body["data"]["components"] and body["data"]["records"]
    assert {r["component"] for r in body["data"]["records"]} >= {"f0.reads", "f1.variants"}
    assert body["truncation"]["reason"] == "max_response_bytes"


async def test_file_count_cap_and_duplicate_files(make_service, fx, tmp_path, data_root):
    cfg = configured(tmp_path, data_root, limits={"max_files_per_call": 2})
    svc = make_service(cfg)
    res = await svc.call(
        Operation.INSPECT_LOCUS, {"interval": iv(100, 200), "files": [ref(fx.cohort_vcf)] * 3}
    )
    assert res.error.code == ErrorCode.BUDGET_EXCEEDED
    res = await svc.call(
        Operation.INSPECT_LOCUS, {"interval": iv(100, 200), "files": [ref(fx.cohort_vcf)] * 2}
    )
    body = res.model_dump(mode="json")
    assert res.status is ResultStatus.OK
    assert records(body, "f0.variants") == records(body, "f1.variants")
    assert any("repeats f0" in w for w in body["warnings"])


async def test_signed_url_is_never_echoed(make_service):
    svc = make_service()
    secret = "SIGNATURE-SHOULD-NOT-LEAK"
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "files": [
                {"uri": f"https://example.org/x.bam?X-Amz-Signature={secret}&token=abc123456"}
            ],
        },
    )
    text = res.model_dump_json()
    assert secret not in text and "abc123456" not in text


async def test_large_interval_omits_sequence_explicitly(make_service, fx):
    svc = make_service()
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {"interval": iv(0, 1000), "reference": ref(fx.fasta), "max_response_bytes": 4096},
    )
    body = res.model_dump(mode="json")
    entry = comp(body, "reference.sequence")
    assert entry["status"] == "skipped" and "use get_sequence" in entry["notes"][0]


async def test_contig_length_disagreement_is_reported(make_service, fx, data_root):
    other = data_root / "other.fa"
    other.write_text(">7\n" + REF[:900] + "\n")
    import pysam

    pysam.faidx(str(other))
    svc = make_service()
    res = await svc.call(
        Operation.INSPECT_LOCUS,
        {"interval": iv(100, 200), "reference": ref(other), "files": [ref(fx.cohort_vcf)]},
    )
    body = res.model_dump(mode="json")
    assert body["data"]["consistency"]["consistent"] is False
    assert any(e["source"] == "consistency" for e in body["errors"])


async def test_cancellation_leaves_no_child_tasks(make_service, fx):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stall(req, ctx):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    svc = make_service(readers=False)
    svc.registry.register(Operation.GET_VARIANTS, "vcf", stall, provider="t")
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        svc.call(
            Operation.INSPECT_LOCUS,
            {"interval": iv(100, 200), "files": [ref(fx.cohort_vcf), ref(fx.second_vcf)]},
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert cancelled.is_set()
    await asyncio.sleep(0)
    leftover = [t for t in asyncio.all_tasks() - before if t is not asyncio.current_task()]
    assert leftover == []


async def test_nothing_to_inspect(make_service):
    res = await make_service().call(Operation.INSPECT_LOCUS, {"interval": iv(1, 2)})
    assert res.error.code == ErrorCode.INVALID_INPUT


def test_source_state_values_are_core():
    assert SourceState.TIMEOUT.value == "timeout"


async def test_minimum_response_cap_compacts_metadata_instead_of_failing(make_service, fx):
    res = await make_service().call(
        Operation.INSPECT_LOCUS,
        {
            "interval": iv(100, 200),
            "files": [ref(fx.deep_bam), ref(fx.cohort_vcf)],
            "max_response_bytes": 4096,
        },
    )
    body = res.model_dump(mode="json")
    assert res.json_size() <= 4096
    assert res.status is not ResultStatus.ERROR, body
    assert "metadata_compacted" in body["data"] and body["data"]["records"]
    entries = {c["id"]: c for c in body["data"]["components"]}
    assert set(entries) == {"f0.reads", "f0.coverage", "f1.variants"}
    assert all(e["status"] == "ok" and e["provenance"] for e in entries.values())
    assert entries["f0.coverage"]["summary"]["mean"] is not None
