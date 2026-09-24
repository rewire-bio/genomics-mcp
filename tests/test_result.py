import pytest

from genomics_mcp.config import Limits
from genomics_mcp.errors import BudgetExceededError, ErrorCode
from genomics_mcp.models import Interval
from genomics_mcp.result import (
    EffectiveLimits,
    ResultStatus,
    ToolResult,
    check_region,
    fit_to_response_budget,
    take_records,
)


def test_callers_can_lower_but_not_raise_limits():
    lim = EffectiveLimits.build(Limits(), max_records=10)
    assert lim.max_records == 10 and not lim.warnings
    lim = EffectiveLimits.build(Limits(), max_records=50_000)
    assert lim.max_records == 10_000
    assert lim.warnings


def test_region_limit_is_enforced_not_shrunk():
    lim = EffectiveLimits.build(Limits())
    check_region(Interval(contig="1", start=0, end=1_000_000, assembly="GRCh38"), lim)
    with pytest.raises(BudgetExceededError) as exc:
        check_region(Interval(contig="1", start=0, end=1_000_001, assembly="GRCh38"), lim)
    assert exc.value.info.code is ErrorCode.BUDGET_EXCEEDED


def test_take_records_detects_truncation_without_reading_everything():
    consumed = []

    def gen():
        for i in range(1_000_000):
            consumed.append(i)
            yield i

    out, trunc = take_records(gen(), 3)
    assert out == [0, 1, 2]
    assert trunc is not None and trunc.returned == 3
    assert len(consumed) == 4
    out, trunc = take_records(range(3), 3)
    assert out == [0, 1, 2] and trunc is None


def _result(n: int) -> ToolResult:
    records = [{"pos": i, "seq": "A" * 100} for i in range(n)]
    return ToolResult(operation="get_reads", status=ResultStatus.OK, data={"records": records})


def test_response_budget_trims_records_and_reports_it():
    res = fit_to_response_budget(_result(200), 8192)
    assert res.status is ResultStatus.OK
    assert res.json_size() <= 8192
    n = len(res.data["records"])
    assert 0 < n < 200
    assert res.truncation.reason == "max_response_bytes"
    assert res.truncation.returned == n
    assert res.warnings


def test_response_budget_untouched_when_small():
    res = _result(2)
    assert fit_to_response_budget(res, 1 << 20) is res


def test_untrimmable_response_becomes_error_not_empty_success():
    res = ToolResult(operation="x", status=ResultStatus.OK, data={"blob": "A" * 20_000})
    out = fit_to_response_budget(res, 4096)
    assert out.status is ResultStatus.ERROR
    assert out.error.code is ErrorCode.BUDGET_EXCEEDED
    assert out.data is None
