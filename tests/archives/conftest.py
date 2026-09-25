"""Shared fixtures: mock HTTP routing, synthetic BAMs and environment isolation.

Cloud variables are scrubbed and AWS config pointed at empty files by tests/conftest.py.
Live tests (marker `network`) run only with GENOMICS_MCP_NETWORK_TESTS=1.
"""

from __future__ import annotations

import os

import pytest
from archive_helpers import Router, make_bam


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`network` tests call real public sources; run them with GENOMICS_MCP_NETWORK_TESTS=1."""
    if os.environ.get("GENOMICS_MCP_NETWORK_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set GENOMICS_MCP_NETWORK_TESTS=1 to run live source tests")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def synthetic_bam(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return make_bam(tmp_path_factory.mktemp("fixtures") / "synthetic.bam")
