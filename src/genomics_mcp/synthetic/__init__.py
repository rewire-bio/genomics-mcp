"""Synthetic-data discovery (#69): a curated protocol catalogue and pinned truth sets.

Read-only. `register(registry)` adds `default` handlers for `list_synthetic_protocols`,
`describe_synthetic_protocol` and `get_truth_set`. Nothing is simulated or downloaded; truth
files are returned as FileRefs for `get_variants`, `get_features` or `fetch_file`.
"""

from __future__ import annotations

from typing import Any

from genomics_mcp.context import OperationContext
from genomics_mcp.errors import InvalidInputError, NotFoundError
from genomics_mcp.models import Interval, Provenance
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.requests import (
    DescribeSyntheticProtocolRequest,
    GetTruthSetRequest,
    ListSyntheticProtocolsRequest,
)
from genomics_mcp.result import OperationOutput
from genomics_mcp.synthetic.protocols import PROTOCOLS
from genomics_mcp.synthetic.truth_sets import CONTIG_LENGTHS, TRUTH_SETS, TruthSet

PROVIDER = __name__
CATALOGUE_VERSION = "2026-11-17"
CATALOGUE_SOURCE = "synthetic_catalogue"
_METHOD = "curated catalogue in genomics-mcp"


def _catalogue_provenance() -> Provenance:
    return Provenance(
        source=CATALOGUE_SOURCE,
        method=_METHOD,
        source_version=CATALOGUE_VERSION,
        url="https://github.com/rewire-bio/genomics-mcp/blob/main/src/genomics_mcp/synthetic",
    )


def _truth_provenance(ts: TruthSet) -> Provenance:
    return Provenance(
        source=ts.provider,
        source_record_id=ts.id,
        url=ts.release_url,
        method=_METHOD,
        source_version=ts.release,
    )


async def list_synthetic_protocols(
    req: ListSyntheticProtocolsRequest, ctx: OperationContext
) -> OperationOutput:
    words = (req.query or "").lower().split()
    records = []
    for p in PROTOCOLS.values():
        if req.variant_type and req.variant_type not in p.variant_types:
            continue
        if req.method and p.method != req.method:
            continue
        if req.setting and p.setting != req.setting:
            continue
        text = " ".join([p.id, p.title, p.summary, p.simulator.name]).lower()
        if any(w not in text for w in words):
            continue
        records.append(p.summary_record())
    return OperationOutput(data={"records": records}, provenance=[_catalogue_provenance()])


async def describe_synthetic_protocol(
    req: DescribeSyntheticProtocolRequest, ctx: OperationContext
) -> OperationOutput:
    protocol = PROTOCOLS.get(req.protocol_id.strip().lower())
    if protocol is None:
        raise NotFoundError(
            f"unknown protocol {req.protocol_id!r}",
            hint=f"known protocols: {', '.join(sorted(PROTOCOLS))}",
        )
    linked = [TRUTH_SETS[t].summary_record() for t in protocol.truth_sets]
    warnings = [
        f"recipe {protocol.recipe.id} is {protocol.recipe.status}: the ID is reserved and no "
        "runnable recipe is published yet"
    ]
    return OperationOutput(
        data={"protocol": protocol.model_dump(mode="json"), "truth_sets": linked},
        provenance=[_catalogue_provenance()],
        warnings=warnings,
    )


def _names(ts: TruthSet) -> set[str]:
    return {ts.assembly.lower(), ts.file_assembly.lower()}


def _select(req: GetTruthSetRequest) -> list[TruthSet]:
    if req.truth_set_id is not None:
        ts = TRUTH_SETS.get(req.truth_set_id.strip().lower())
        if ts is None:
            raise NotFoundError(
                f"unknown truth set {req.truth_set_id!r}",
                hint=f"known truth sets: {', '.join(sorted(TRUTH_SETS))}",
            )
        if req.assembly and req.assembly.strip().lower() not in _names(ts):
            raise InvalidInputError(
                f"truth set {ts.id} is on {ts.assembly}, not {req.assembly}; no liftover is "
                "performed",
                hint="call get_truth_set with only the assembly to list bundles on it",
            )
        return [ts]
    out = []
    for ts in TRUTH_SETS.values():
        if req.sample and req.sample.strip().upper() != ts.sample.upper():
            continue
        if req.assembly and req.assembly.strip().lower() not in _names(ts):
            continue
        if req.variant_type and req.variant_type not in ts.variant_types:
            continue
        if req.provider and req.provider != ts.provider:
            continue
        out.append(ts)
    return out


def _contig_interval(ts: TruthSet, contig: str) -> Interval:
    lengths = CONTIG_LENGTHS.get(ts.assembly)
    if lengths is None:
        raise InvalidInputError(
            f"truth set {ts.id} uses its own assembly ({ts.assembly}); contig lookup is only "
            "available for GRCh37 and GRCh38 bundles",
            hint="read contig names and lengths from the assembly's .fai file",
        )
    covered = ts.contigs or list(lengths)
    if contig not in covered:
        other = contig[3:] if contig.startswith("chr") else f"chr{contig}"
        hint = (
            f"{ts.assembly} truth files name it {other!r}"
            if other in covered
            else f"{ts.id} covers: {', '.join(covered)}"
        )
        raise InvalidInputError(f"contig {contig!r} is not in {ts.id}", hint=hint)
    return Interval(contig=contig, start=0, end=lengths[contig], assembly=ts.file_assembly)


async def get_truth_set(req: GetTruthSetRequest, ctx: OperationContext) -> OperationOutput:
    selected = _select(req)
    records: list[dict[str, Any]] = []
    for ts in selected:
        record = ts.model_dump(mode="json")
        if req.contig is not None:
            record["region"] = _contig_interval(ts, req.contig.strip()).model_dump(mode="json")
        records.append(record)
    warnings = []
    if not records:
        warnings.append(f"no truth set matches; known truth sets: {', '.join(sorted(TRUTH_SETS))}")
    return OperationOutput(
        data={"records": records},
        provenance=[_truth_provenance(ts) for ts in selected] or [_catalogue_provenance()],
        warnings=warnings,
    )


def register(registry: Registry) -> None:
    registry.register(
        Operation.LIST_SYNTHETIC_PROTOCOLS, "default", list_synthetic_protocols, provider=PROVIDER
    )
    registry.register(
        Operation.DESCRIBE_SYNTHETIC_PROTOCOL,
        "default",
        describe_synthetic_protocol,
        provider=PROVIDER,
    )
    registry.register(Operation.GET_TRUTH_SET, "default", get_truth_set, provider=PROVIDER)
