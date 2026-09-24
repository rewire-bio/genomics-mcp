"""Access boundaries shared by all adapters: local path allowlist and ambient-credential isolation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from genomics_mcp.config import Settings
from genomics_mcp.errors import InvalidInputError, NotFoundError, UnauthorizedError

# Variables that make HTSlib, boto3 or cloud SDKs pick up ambient credentials or config.
AMBIENT_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCS_", "GCLOUD_", "CLOUDSDK_", "HTS_S3_")
AMBIENT_NAMES = frozenset(
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "BOTO_CONFIG",
        "BOTO_PATH",
        "HTS_AUTH_LOCATION",
        "S3_ENDPOINT",
    }
)


def scrubbed_env(
    base: Mapping[str, str] | None = None, *, empty_aws_config_dir: Path | None = None
) -> dict[str, str]:
    """Copy of `base` (default os.environ) with ambient cloud credentials removed.

    If `empty_aws_config_dir` is given, AWS config/credential files point at empty files
    inside it so SDKs cannot read ~/.aws, and EC2/ECS metadata lookups are disabled.
    """
    src = dict(os.environ if base is None else base)
    env = {
        k: v
        for k, v in src.items()
        if not k.startswith(AMBIENT_PREFIXES) and k not in AMBIENT_NAMES
    }
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    if empty_aws_config_dir is not None:
        empty_aws_config_dir.mkdir(parents=True, exist_ok=True)
        cfg = empty_aws_config_dir / "config"
        creds = empty_aws_config_dir / "credentials"
        for f in (cfg, creds):
            if not f.exists():
                f.write_text("")
        env["AWS_CONFIG_FILE"] = str(cfg)
        env["AWS_SHARED_CREDENTIALS_FILE"] = str(creds)
    return env


def resolve_local_path(settings: Settings, path: str | Path, *, must_exist: bool = True) -> Path:
    """Resolve a local path and require it to sit under an allowed root or the work dir.

    Symlinks are resolved before the check, so a link cannot escape an allowed root.
    """
    raw = str(path)
    if raw.startswith("file://"):
        raw = raw[len("file://") :]
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise InvalidInputError("local paths must be absolute", details={"path": raw})
    resolved = p.resolve(strict=False)
    roots = [*settings.paths.allowed_roots, settings.paths.work_dir]
    if not any(resolved == r or resolved.is_relative_to(r) for r in roots):
        raise UnauthorizedError(
            "path is outside the configured allowed roots",
            source="local",
            hint="add a parent directory to paths.allowed_roots or GENOMICS_MCP_ALLOWED_ROOTS",
        )
    if must_exist and not resolved.exists():
        raise NotFoundError("local file does not exist", source="local", details={"path": raw})
    return resolved
