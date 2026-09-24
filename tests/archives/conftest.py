"""Shared fixtures: mock HTTP routing, synthetic BAMs and environment isolation.

Every test runs with cloud credential variables removed and instance metadata disabled.
Live tests (marker `live`) run only with GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os
import re

import pytest
from archive_helpers import Router, make_bam

_CLOUD_VARS = re.compile(r"^(AWS_|AMAZON_|BOTO_|GOOGLE_APPLICATION|AZURE_|HTS_S3|S3_)")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: calls real public services (GENOMICS_MCP_LIVE=1)")
    # Run `async def` tests without per-test marks (pyproject is not owned by this epic).
    config.option.asyncio_mode = "auto"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("GENOMICS_MCP_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="set GENOMICS_MCP_LIVE=1 to run live source tests")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_ambient_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if _CLOUD_VARS.match(key):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def synthetic_bam(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return make_bam(tmp_path_factory.mktemp("fixtures") / "synthetic.bam")
