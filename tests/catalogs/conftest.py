"""Catalog test fixtures: mock routing and environment isolation; `network` tests need
GENOMICS_MCP_NETWORK_TESTS=1."""

from __future__ import annotations

import os

import pytest
from catalog_helpers import Router


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
