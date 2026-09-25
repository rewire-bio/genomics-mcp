"""Settings: TOML file plus a small set of GENOMICS_MCP_* environment variables.

Precedence: explicit overrides (CLI) > environment > TOML > defaults.

Credentials are never read from ambient cloud variables. Secrets are referenced by
the *name* of an environment variable (or a token file) and resolved on use.
Names starting with AWS_ are rejected so a work shell's credentials cannot be
picked up by accident.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from genomics_mcp.errors import ErrorCode, GenomicsError, register_secret

ENV_PREFIX = "GENOMICS_MCP_"
DEFAULT_TOKEN_ENV = "GENOMICS_MCP_HTTP_TOKEN"  # noqa: S105 - variable name, not a secret
MIN_TOKEN_LENGTH = 32

MiB = 1024 * 1024
GiB = 1024 * MiB


class ConfigError(GenomicsError):
    code = ErrorCode.INVALID_INPUT


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_env_name(v: str | None) -> str | None:
    if v is None:
        return v
    if not v or not v.replace("_", "").isalnum():
        raise ValueError("must be an environment variable name")
    if v.upper().startswith("AWS_"):
        raise ValueError(
            "AWS_* variables are ambient credentials and are never used; "
            "export personal credentials under a project-specific name"
        )
    return v


def _read_env(name: str, env: Mapping[str, str]) -> SecretStr | None:
    value = env.get(name)
    if not value:
        return None
    register_secret(value)
    return SecretStr(value)


class Limits(_Model):
    """Default bounds. Callers may lower interactive limits per call, never raise them."""

    max_region_bp: int = Field(default=1_000_000, gt=0)
    max_records: int = Field(default=10_000, gt=0)
    max_response_bytes: int = Field(default=1 * MiB, ge=4096)
    interactive_timeout_s: float = Field(default=30.0, gt=0)
    max_transfer_bytes: int = Field(
        default=100 * MiB,
        gt=0,
        description="Transfer size allowed without an explicit caller budget.",
    )
    transfer_budget_ceiling_bytes: int = Field(
        default=20 * GiB, gt=0, description="Largest explicit caller budget accepted."
    )
    workspace_max_bytes: int = Field(default=50 * GiB, gt=0)
    max_files_per_call: int = Field(default=16, gt=0)


class HttpSettings(_Model):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    path: str = "/mcp"
    token_env: str = DEFAULT_TOKEN_ENV
    token_file: Path | None = None
    allow_non_loopback: bool = False
    allowed_hosts: list[str] = Field(
        default_factory=list, description="Extra Host header values accepted (host:port)."
    )

    @field_validator("token_env")
    @classmethod
    def _env_name(cls, v: str) -> str | None:
        return _check_env_name(v)

    @property
    def is_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


class SourceSettings(_Model):
    enabled: bool = True
    api_key_env: str | None = None
    base_url: str | None = Field(default=None, description="Override for mirrors or tests.")
    timeout_s: float | None = Field(default=None, gt=0)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    requests_per_minute: float | None = Field(default=None, gt=0)
    contact_email: str | None = None

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        return _check_env_name(v)


class S3Profile(_Model):
    """An explicitly configured S3 or S3-compatible endpoint (e.g. personal AWS, MinIO)."""

    endpoint_url: str = Field(description="Required; no implicit AWS endpoint discovery.")
    region: str = "us-east-1"
    anonymous: bool = False
    access_key_id_env: str | None = None
    secret_access_key_env: str | None = None
    session_token_env: str | None = None
    addressing_style: Literal["path", "virtual", "auto"] = "path"
    requester_pays: bool = False
    buckets: list[str] = Field(default_factory=list, description="Allowlist; empty means any.")

    @field_validator("access_key_id_env", "secret_access_key_env", "session_token_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        return _check_env_name(v)

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint(cls, v: str) -> str:
        if not v.startswith(("https://", "http://")):
            raise ValueError("endpoint_url must be an http(s) URL")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _credentials_named(self) -> S3Profile:
        if not self.anonymous and not (self.access_key_id_env and self.secret_access_key_env):
            raise ValueError(
                "non-anonymous profile needs access_key_id_env and secret_access_key_env"
            )
        return self


class S3Credentials(_Model):
    access_key_id: SecretStr
    secret_access_key: SecretStr
    session_token: SecretStr | None = None


class StorageSettings(_Model):
    profiles: dict[str, S3Profile] = Field(default_factory=dict)
    allow_public_s3: bool = True
    public_s3_endpoint: str = "https://s3.amazonaws.com"
    public_s3_region: str = "us-east-1"
    local_network_hosts: list[str] = Field(
        default_factory=list,
        description="Hosts (name or IP literal) that storage resolvers may reach although they "
        "are loopback/private, and over plain http, e.g. a local fixture server or LAN mirror. "
        "S3 profile endpoints are allowed implicitly. Cloud metadata addresses are always refused.",
    )


class PathSettings(_Model):
    allowed_roots: list[Path] = Field(
        default_factory=list, description="Local directories readable by tools. Empty: none."
    )
    work_dir: Path = Field(default_factory=lambda: _default_work_dir())

    @field_validator("allowed_roots")
    @classmethod
    def _abs(cls, v: list[Path]) -> list[Path]:
        return [p.expanduser().resolve() for p in v]

    @field_validator("work_dir")
    @classmethod
    def _abs_work(cls, v: Path) -> Path:
        return v.expanduser().resolve()


class LoggingSettings(_Model):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_query_content: bool = Field(
        default=False, description="Log intervals/identifiers from requests. Off by default."
    )


class Settings(_Model):
    limits: Limits = Field(default_factory=Limits)
    http: HttpSettings = Field(default_factory=HttpSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    sources: dict[str, SourceSettings] = Field(default_factory=dict)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Environment captured at load time, restricted to named secrets and GENOMICS_MCP_*.
    _env: dict[str, str] = PrivateAttr(default_factory=dict)

    def source(self, name: str) -> SourceSettings:
        return self.sources.get(name, SourceSettings())

    def source_api_key(self, name: str) -> SecretStr | None:
        cfg = self.source(name)
        return _read_env(cfg.api_key_env, self._env) if cfg.api_key_env else None

    def http_token(self) -> SecretStr | None:
        if self.http.token_file is not None:
            try:
                value = self.http.token_file.expanduser().read_text().strip()
            except OSError as exc:
                raise ConfigError(f"cannot read http.token_file: {exc.strerror}") from None
            if value:
                register_secret(value)
                return SecretStr(value)
        return _read_env(self.http.token_env, self._env)

    def _token_configured(self) -> bool:
        try:
            return self.http_token() is not None
        except ConfigError:
            return False

    def s3_profile(self, name: str) -> S3Profile:
        try:
            return self.storage.profiles[name]
        except KeyError:
            raise ConfigError(
                f"storage profile {name!r} is not configured",
                hint="add [storage.profiles.<name>] to the config file",
            ) from None

    def s3_credentials(self, name: str) -> S3Credentials | None:
        """Explicit credentials for a profile, or None for anonymous. Never consults AWS_* or ~/.aws."""
        profile = self.s3_profile(name)
        if profile.anonymous:
            return None
        assert profile.access_key_id_env and profile.secret_access_key_env
        key = _read_env(profile.access_key_id_env, self._env)
        secret = _read_env(profile.secret_access_key_env, self._env)
        if key is None or secret is None:
            raise ConfigError(
                f"storage profile {name!r}: credential environment variables are not set",
                code=ErrorCode.UNAUTHORIZED,
                details={
                    "access_key_id_env": profile.access_key_id_env,
                    "secret_access_key_env": profile.secret_access_key_env,
                },
            )
        token = (
            _read_env(profile.session_token_env, self._env) if profile.session_token_env else None
        )
        return S3Credentials(access_key_id=key, secret_access_key=secret, session_token=token)

    def public_view(self) -> dict[str, Any]:
        """Non-secret summary for status resources."""
        return {
            "limits": self.limits.model_dump(),
            "http": {
                "host": self.http.host,
                "port": self.http.port,
                "path": self.http.path,
                "token_configured": self._token_configured(),
            },
            "paths": {
                "allowed_roots": [str(p) for p in self.paths.allowed_roots],
                "work_dir": str(self.paths.work_dir),
            },
            "storage_profiles": {
                name: {
                    "endpoint_url": p.endpoint_url,
                    "anonymous": p.anonymous,
                    "requester_pays": p.requester_pays,
                }
                for name, p in self.storage.profiles.items()
            },
            "sources": {
                name: {
                    "enabled": s.enabled,
                    "api_key_configured": self.source_api_key(name) is not None,
                }
                for name, s in self.sources.items()
            },
        }


def _default_work_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "genomics-mcp"


def _deep_merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {}

    def put(section: str, key: str, value: Any) -> None:
        data.setdefault(section, {})[key] = value

    if v := env.get(f"{ENV_PREFIX}HOST"):
        put("http", "host", v)
    if v := env.get(f"{ENV_PREFIX}PORT"):
        put("http", "port", v)
    if v := env.get(f"{ENV_PREFIX}HTTP_TOKEN_FILE"):
        put("http", "token_file", v)
    if v := env.get(f"{ENV_PREFIX}WORK_DIR"):
        put("paths", "work_dir", v)
    if v := env.get(f"{ENV_PREFIX}ALLOWED_ROOTS"):
        put("paths", "allowed_roots", [p for p in v.split(os.pathsep) if p])
    if v := env.get(f"{ENV_PREFIX}LOG_LEVEL"):
        put("logging", "level", v.upper())
    return data


def load_settings(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Load settings. `env` defaults to os.environ; only GENOMICS_MCP_* and named secrets are read."""
    env = dict(os.environ if env is None else env)
    path = config_path or env.get(f"{ENV_PREFIX}CONFIG")
    data: dict[str, Any] = {}
    if path:
        p = Path(path).expanduser()
        try:
            with p.open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {p}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {p}: {exc}") from None
    data = _deep_merge(data, _env_overrides(env))
    if overrides:
        data = _deep_merge(data, overrides)
    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"invalid configuration: {problems}") from None
    # Keep only variables the configuration names explicitly, plus our own prefix.
    wanted = {settings.http.token_env}
    for s in settings.sources.values():
        if s.api_key_env:
            wanted.add(s.api_key_env)
    for prof in settings.storage.profiles.values():
        wanted.update(
            n
            for n in (prof.access_key_id_env, prof.secret_access_key_env, prof.session_token_env)
            if n
        )
    settings._env = {k: v for k, v in env.items() if k in wanted or k.startswith(ENV_PREFIX)}
    return settings


def validate_http_security(settings: Settings) -> SecretStr:
    """Refuse to serve HTTP without a token, or off-loopback without explicit opt-in."""
    token = settings.http_token()
    if token is None:
        raise ConfigError(
            "HTTP transport requires a bearer token",
            hint=f"set {settings.http.token_env} or http.token_file (at least {MIN_TOKEN_LENGTH} chars)",
        )
    if len(token.get_secret_value()) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"HTTP bearer token must be at least {MIN_TOKEN_LENGTH} characters")
    if not settings.http.is_loopback and not settings.http.allow_non_loopback:
        raise ConfigError(
            f"refusing to bind non-loopback host {settings.http.host!r}",
            hint="set http.allow_non_loopback = true and put TLS in front of the server",
        )
    return token
