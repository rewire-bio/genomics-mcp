"""Cache tests: shared E2-E5 support (range server, golden files) plus the benchmark fixtures."""

import importlib.util
import sys
from pathlib import Path

import pytest

if "gm_test_support" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "gm_test_support", Path(__file__).parents[1] / "storage" / "support.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["gm_test_support"] = _mod
    _spec.loader.exec_module(_mod)

from gm_test_support import FixtureServer, fixture_server, golden, gsettings, service  # noqa: F401

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import benchmark_cache


@pytest.fixture(scope="session")
def perf(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Multi-block synthetic bigWig/BAM/VCF/FASTA/BED (a few MiB), built once."""
    return benchmark_cache.build_fixtures(tmp_path_factory.mktemp("perf"))


@pytest.fixture
def perf_server(perf):
    srv = FixtureServer(perf["bam"].parent)
    srv.httpd.handle_error = lambda *a: None  # readers close ranges early
    yield srv
    srv.close()
