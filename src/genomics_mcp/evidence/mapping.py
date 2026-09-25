"""Convert reference-library results into core typed outputs without flattening.

Each `Evidence` becomes an `EvidenceRecord` (source-native `data` kept intact) under
`data["records"]`, so the core record/byte budget applies and is reported. Individual
ClinVar assertions, gnomAD counts/denominators and per-source truncation stay nested.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from genomics_mcp.errors import ErrorCode, ErrorInfo, redact
from genomics_mcp.models import EvidenceRecord, Provenance, SourceState, SourceStatus
from genomics_mcp.references.models import Evidence, SourceError
from genomics_mcp.references.schemas import ToolResult as RefResult
from genomics_mcp.result import OperationOutput, take_records

_CODE = {
    "invalid_input": ErrorCode.INVALID_INPUT,
    "not_found": ErrorCode.NOT_FOUND,
    "unauthorized": ErrorCode.UNAUTHORIZED,
    "forbidden": ErrorCode.UNAUTHORIZED,
    "rate_limited": ErrorCode.UPSTREAM_ERROR,
    "timeout": ErrorCode.TIMEOUT,
    "upstream": ErrorCode.UPSTREAM_ERROR,
    "invalid_response": ErrorCode.UPSTREAM_ERROR,
    "unsupported": ErrorCode.UNSUPPORTED,
    "not_configured": ErrorCode.UNSUPPORTED,
}

_STATE = {
    "invalid_input": SourceState.ERROR,
    "not_found": SourceState.NOT_FOUND,
    "unauthorized": SourceState.UNAUTHORIZED,
    "forbidden": SourceState.UNAUTHORIZED,
    "rate_limited": SourceState.UNAVAILABLE,
    "timeout": SourceState.TIMEOUT,
    "upstream": SourceState.UNAVAILABLE,
    "invalid_response": SourceState.ERROR,
    "unsupported": SourceState.NOT_IMPLEMENTED,
    "not_configured": SourceState.NOT_CONFIGURED,
}

# Predictions are not observations; everything else here is source-observed/curated data.
_PREDICTED = {"functional_prediction"}

CATEGORY = {
    "variant_consequence": "consequence",
    "clinical_allele_match": "clinical",
    "clinical_variant_record": "clinical",
    "clinical_assertion": "clinical",
    "population_frequency": "population",
    "functional_prediction": "functional_prediction",
}


def _is_consent(err: SourceError) -> bool:
    return err.operation == "egress_check"


def error_info(err: SourceError) -> ErrorInfo:
    if _is_consent(err):
        code = ErrorCode.CONSENT_REQUIRED
    else:
        code = _CODE[err.kind]
    details: dict[str, Any] = {"kind": err.kind, "operation": err.operation}
    if err.status_code is not None:
        details["http_status"] = err.status_code
    if err.url:
        details["url"] = err.url
    hint = None
    if code is ErrorCode.CONSENT_REQUIRED:
        hint = (
            "pass explicit per-call consent (allow_external_annotation) to permit external queries"
        )
    elif err.kind == "not_configured" and "by configuration" in err.message:
        hint = "enable the source in configuration or include it in `sources`"
    return ErrorInfo(
        code=code,
        message=redact(err.message),
        source=None if err.source == "local" else err.source,
        retryable=err.retryable,
        hint=hint,
        details=details,
    )


def _state(err: SourceError) -> SourceState:
    if _is_consent(err):
        return SourceState.SKIPPED
    if err.kind == "not_configured" and "by configuration" in err.message:
        return SourceState.DISABLED
    return _STATE[err.kind]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def provenance(ev: Evidence) -> Provenance:
    return Provenance(
        source=ev.source,
        source_record_id=ev.source_record_id,
        url=ev.source_url,
        method=f"{ev.source} {ev.evidence_type}",
        retrieved_at=ev.retrieved_at,
        source_version=ev.source_release,
        source_updated_at=_parse_time(ev.source_updated_at),
        terms_url=ev.terms_url,
        transformations=[f"{t.operation}: {t.detail}" for t in ev.transformations],
    )


def record(ev: Evidence) -> dict[str, Any]:
    """EvidenceRecord (validated) plus version, structured trace and nested truncation."""
    typed = EvidenceRecord(
        source=ev.source,
        source_record_id=ev.source_record_id,
        evidence_type=ev.evidence_type,
        observed=ev.evidence_type not in _PREDICTED,
        data=ev.data,
        limitations=ev.limitations,
        provenance=provenance(ev),
    )
    out = typed.model_dump(mode="json")
    out["category"] = CATEGORY.get(ev.evidence_type, ev.evidence_type)
    out["source_record_version"] = ev.source_record_version
    out["source_updated_at_raw"] = ev.source_updated_at
    out["transformation_trace"] = [t.model_dump(mode="json") for t in ev.transformations]
    if ev.truncation:
        out["source_truncation"] = [t.model_dump(mode="json") for t in ev.truncation]
    return out


def _summary_provenance(evidence: list[Evidence]) -> list[Provenance]:
    """One compact provenance entry per source and release; per-record provenance is in records."""
    seen: dict[tuple[str, str | None], Provenance] = {}
    for ev in evidence:
        key = (ev.source, ev.source_release)
        if key not in seen:
            seen[key] = Provenance(
                source=ev.source,
                method=f"{ev.source} public API",
                retrieved_at=ev.retrieved_at,
                source_version=ev.source_release,
                terms_url=ev.terms_url,
            )
    return list(seen.values())


def source_statuses(result: RefResult, evidence: list[Evidence]) -> list[SourceStatus]:
    errors_by_source: dict[str, list[SourceError]] = {}
    for err in result.errors:
        if err.source != "local":
            errors_by_source.setdefault(err.source, []).append(err)
    answered = list(dict.fromkeys([*result.sources_consulted, *(e.source for e in evidence)]))
    out: list[SourceStatus] = []
    for source in answered:
        errs = errors_by_source.pop(source, [])
        state = SourceState.PARTIAL if errs else SourceState.OK
        message = "; ".join(redact(e.message) for e in errs)[:500] or None
        out.append(SourceStatus(source=source, state=state, message=message))
    for source, errs in errors_by_source.items():
        out.append(
            SourceStatus(
                source=source,
                state=_state(errs[-1]),
                message="; ".join(redact(e.message) for e in errs)[:500],
            )
        )
    return out


def to_output(
    result: RefResult,
    evidence: list[Evidence],
    data: dict[str, Any],
    *,
    max_records: int,
    extra_errors: list[ErrorInfo] | None = None,
    extra_statuses: list[SourceStatus] | None = None,
) -> OperationOutput:
    errors = [*(extra_errors or []), *(error_info(e) for e in result.errors)]
    statuses: list[SourceStatus] = []
    for status in [*(extra_statuses or []), *source_statuses(result, evidence)]:
        if all(s.source != status.source for s in statuses):  # one status per source
            statuses.append(status)
    warnings = [redact(w) for w in result.warnings]
    if result.status == "error":
        if not errors:
            errors.append(
                ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR, message="reference call failed without detail"
                )
            )
        return OperationOutput(
            data=None,
            errors=errors,
            source_status=statuses,
            warnings=warnings,
            provenance=_summary_provenance(evidence),
        )
    records, truncation = take_records((record(ev) for ev in evidence), max_records)
    if truncation is not None:
        truncation.available = len(evidence)
    payload = {
        "result_status": result.status,
        **data,
        "transformations": [t.model_dump(mode="json") for t in result.transformations],
        "limitations": result.limitations,
        "records": records,
    }
    if result.truncation:
        payload["reference_truncation"] = [t.model_dump(mode="json") for t in result.truncation]
    return OperationOutput(
        data=payload,
        provenance=_summary_provenance(evidence),
        warnings=warnings,
        errors=errors,
        source_status=statuses,
        truncation=truncation,
    )
