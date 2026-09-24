"""Access boundaries shared by all adapters: local paths, network destinations, ambient credentials."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

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
        # HTSlib CRAM reference lookup. Inherited values could point at unapproved references.
        "REF_PATH",
        "REF_CACHE",
    }
)

# Cloud credential metadata services. Never reachable through this server, even if configured.
METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "fd00:ec2::254",
        "instance-data",
        "instance-data.ec2.internal",
        "metadata",
        "metadata.google.internal",
        "metadata.azure.internal",
    }
)


def _normalize_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def is_metadata_destination(host: str | None) -> bool:
    """True for cloud metadata endpoints, including any IPv4/IPv6 link-local literal."""
    if not host:
        return False
    h = _normalize_host(host)
    if h in METADATA_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_link_local or ip == ipaddress.ip_address("fd00:ec2::254")


def check_network_destination(
    url: str,
    *,
    source: str,
    allowed_hosts: Iterable[str] | None,
    allow_http: bool = False,
    previous_url: str | None = None,
) -> None:
    """Validate one outgoing request (or redirect hop) before it is sent.

    - scheme must be https, or http only when `allow_http` (explicit loopback fixtures/mirrors);
    - never downgrade https -> http across a redirect;
    - host must be in `allowed_hosts` when given (None means any public host, for resolvers
      that validate elsewhere);
    - cloud metadata destinations are always refused.
    Error messages carry only the host, never the URL (which may be signed).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = _normalize_host(parts.hostname or "")
    if not host or scheme not in ("http", "https"):
        raise InvalidInputError(
            f"{source}: only http(s) URLs with a host are allowed", source=source
        )
    if is_metadata_destination(host):
        raise UnauthorizedError(
            f"{source}: cloud metadata endpoints are never contacted",
            source=source,
            details={"host": host},
        )
    if scheme == "http":
        if previous_url and urlsplit(previous_url).scheme.lower() == "https":
            raise InvalidInputError(
                f"{source}: refusing https to http redirect", source=source, details={"host": host}
            )
        if not allow_http:
            raise InvalidInputError(
                f"{source}: plain http is not allowed", source=source, details={"host": host}
            )
    if allowed_hosts is not None and host not in {_normalize_host(h) for h in allowed_hosts}:
        raise InvalidInputError(
            f"{source}: host is not allowed for this source",
            source=source,
            details={"host": host},
        )


def scrubbed_env(
    base: Mapping[str, str] | None = None,
    *,
    empty_aws_config_dir: Path | None = None,
    empty_ref_dir: Path | None = None,
) -> dict[str, str]:
    """Copy of `base` (default os.environ) for blocking reader subprocesses.

    Removes ambient cloud credentials, cloud SDK config pointers and inherited HTSlib
    REF_PATH/REF_CACHE. Disables EC2/ECS metadata lookups.

    - `empty_aws_config_dir`: AWS config/credentials and BOTO_CONFIG point at empty files
      there, so SDKs cannot read ~/.aws or ~/.boto.
    - `empty_ref_dir`: REF_PATH and REF_CACHE point at this empty directory. Without it,
      HTSlib falls back to its built-in network reference server when REF_PATH is unset,
      so CRAM readers must pass it (or an explicit approved reference) in subprocesses.
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
        boto = empty_aws_config_dir / "boto"
        for f in (cfg, creds, boto):
            if not f.exists():
                f.write_text("")
        env["AWS_CONFIG_FILE"] = str(cfg)
        env["AWS_SHARED_CREDENTIALS_FILE"] = str(creds)
        env["BOTO_CONFIG"] = str(boto)
    if empty_ref_dir is not None:
        empty_ref_dir.mkdir(parents=True, exist_ok=True)
        env["REF_PATH"] = str(empty_ref_dir)
        env["REF_CACHE"] = str(empty_ref_dir)
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
