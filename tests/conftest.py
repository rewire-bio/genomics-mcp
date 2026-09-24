from __future__ import annotations

import os
from pathlib import Path

import pytest

from genomics_mcp.config import Settings, load_settings
from genomics_mcp.security import AMBIENT_NAMES, AMBIENT_PREFIXES


@pytest.fixture(autouse=True)
def _isolate_cloud_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """No test may see ambient cloud credentials or ~/.aws."""
    for key in list(os.environ):
        if (
            key.startswith(AMBIENT_PREFIXES)
            or key in AMBIENT_NAMES
            or key.startswith("GENOMICS_MCP_")
        ):
            monkeypatch.delenv(key, raising=False)
    aws_dir = tmp_path_factory.mktemp("aws-empty")
    (aws_dir / "config").write_text("")
    (aws_dir / "credentials").write_text("")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_dir / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_dir / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def settings(tmp_path: Path, data_root: Path) -> Settings:
    return load_settings(
        env={},
        overrides={
            "paths": {"allowed_roots": [str(data_root)], "work_dir": str(tmp_path / "work")}
        },
    )
