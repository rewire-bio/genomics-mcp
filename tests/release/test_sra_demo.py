"""The SRA demo verdict gates real evidence: every expected check must be present and pass."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from sra_demo import BASE_CHECKS, MID_SIZE_CHECKS, verdict

OK = {"ok": True}


def test_all_expected_checks_pass():
    assert verdict(dict.fromkeys(BASE_CHECKS, OK), skip_mid_size=True)
    assert verdict(dict.fromkeys(BASE_CHECKS + MID_SIZE_CHECKS, OK), skip_mid_size=False)


@pytest.mark.parametrize("missing", MID_SIZE_CHECKS)
def test_missing_mid_size_check_fails(missing):
    checks = dict.fromkeys(BASE_CHECKS + MID_SIZE_CHECKS, OK)
    del checks[missing]
    assert not verdict(checks, skip_mid_size=False)


def test_failed_interruption_fails_even_if_others_pass():
    checks = dict.fromkeys((*BASE_CHECKS, "cancel"), OK)
    checks["interrupt"] = {"ok": False, "failure": "Failed: ended before it could be interrupted"}
    checks["interrupt_resume"] = {"ok": False, "failure": "not run"}
    assert not verdict(checks, skip_mid_size=False)
    assert not verdict({**dict.fromkeys(BASE_CHECKS, OK), "paired": {}}, skip_mid_size=True)
