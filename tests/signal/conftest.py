"""Loads the shared E2-E5 test support (tests/storage/support.py) and its fixtures."""

import importlib.util
import sys
from pathlib import Path

if "gm_test_support" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "gm_test_support", Path(__file__).parents[1] / "storage" / "support.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["gm_test_support"] = _mod
    _spec.loader.exec_module(_mod)

from gm_test_support import fixture_server, golden, gsettings, service  # noqa: F401
