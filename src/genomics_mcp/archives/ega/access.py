"""EGA access configuration: explicit, personal, never ambient.

Core `SourceSettings` has no fields for account logins, so this typed extension reads only
values core already retains (`Settings` keeps the variable named by `[sources.ega].api_key_env`
and `GENOMICS_MCP_*` variables; nothing else from the environment):

| Mode | Configuration |
|---|---|
| bearer token | `[sources.ega] api_key_env = "MY_EGA_TOKEN"` (a personal EGA access token) |
| personal account | `GENOMICS_MCP_EGA_USERNAME` and `GENOMICS_MCP_EGA_PASSWORD` (password grant as pyega3) |
| public test account | `GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1` (default off) |

The password grant needs pyega3's public OpenID client secret. It is loaded in memory from
the official pyega3 repository at a pinned commit, verified by SHA-256
(`GENOMICS_MCP_EGA_CLIENT_SECRET` overrides it). Public-test mode loads the documented public
test account from the same pinned files. Nothing is written to disk or logged; every loaded
value is registered for redaction. No `~/.aws`, keychain or default credential discovery.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from genomics_mcp.archives._common.errors import UnauthorizedError, UpstreamError
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy
from genomics_mcp.archives._common.redact import register_secret
from genomics_mcp.archives.ega.auth import EgaAuth, EgaPasswordGrant

SOURCE = "ega"
PYEGA3_COMMIT = "5ec6c4cd37cc67142285051bdbd725c824da1384"  # EGA-archive/ega-download-client v5.2.1
PYEGA3_RAW = f"https://raw.githubusercontent.com/EGA-archive/ega-download-client/{PYEGA3_COMMIT}/pyega3/config"
PYEGA3_SHA256 = {
    "default_server_file.json": "98a60814f680b92d76302fa28b846b32995a480ab51e27e6b5cdbb1fd15d9190",
    "default_credential_file.json": "5fa7ab48fbbfa00f3bed6403702fd567afebea090184ba7f87b3dadaf38e1d9a",
}
ENV_USER = "GENOMICS_MCP_EGA_USERNAME"
ENV_PASSWORD = "GENOMICS_MCP_EGA_PASSWORD"  # noqa: S105 - variable name
ENV_CLIENT_SECRET = "GENOMICS_MCP_EGA_CLIENT_SECRET"  # noqa: S105 - variable name
ENV_PUBLIC_TEST = "GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT"

Mode = Literal["anonymous", "bearer_token", "personal_account", "public_test_account"]


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class EgaAccessConfig:
    mode: Mode
    token: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    client_secret: SecretStr | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> EgaAccessConfig:
        env: dict[str, str] = getattr(settings, "_env", {}) or {}
        token = settings.source_api_key(SOURCE)
        if token is not None:
            register_secret(token.get_secret_value())
            return cls("bearer_token", token=token)
        user, pw = env.get(ENV_USER), env.get(ENV_PASSWORD)
        secret = env.get(ENV_CLIENT_SECRET)
        for v in (pw, secret):
            register_secret(v)
        if user and pw:
            return cls(
                "personal_account",
                username=user,
                password=SecretStr(pw),
                client_secret=SecretStr(secret) if secret else None,
            )
        if user or pw:
            raise UnauthorizedError(
                f"EGA login needs both {ENV_USER} and {ENV_PASSWORD}", source=SOURCE
            )
        if _truthy(env.get(ENV_PUBLIC_TEST)):
            return cls("public_test_account")
        return cls("anonymous")

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "controlled_file_access": self.mode != "anonymous"}


async def fetch_pyega3_config(client: httpx.AsyncClient) -> dict[str, dict]:
    """The official pyega3 config files at the pinned commit, hash-verified, in memory only."""
    http = SourceHttp(
        client,
        SourcePolicy(
            SOURCE,
            frozenset({"raw.githubusercontent.com"}),
            min_interval_s=0,
            max_body_bytes=16 * 1024,
        ),
    )
    out: dict[str, dict] = {}
    for name, digest in PYEGA3_SHA256.items():
        res = await http.request("GET", f"{PYEGA3_RAW}/{name}")
        if hashlib.sha256(res.body).hexdigest() != digest:
            raise UpstreamError(
                "pinned pyega3 configuration did not match its recorded SHA-256",
                source=SOURCE,
                details={"file": name},
            )
        data = json.loads(res.body)
        for key in ("password", "client_secret"):
            if isinstance(data.get(key), str):
                register_secret(data[key])
        out[name] = data
    return out


class EgaAccess:
    """Builds and caches the EgaAuth for the configured mode (token cached across calls)."""

    def __init__(self) -> None:
        self._auth: dict[tuple, EgaAuth] = {}

    async def auth(self, cfg: EgaAccessConfig, client: httpx.AsyncClient) -> EgaAuth | None:
        if cfg.mode == "anonymous":
            return None
        key = (
            cfg.mode,
            cfg.username,
            hashlib.sha256(
                (cfg.token or cfg.password or SecretStr("")).get_secret_value().encode()
            ).hexdigest(),
        )
        if key in self._auth:
            return self._auth[key]
        if cfg.mode == "bearer_token":
            auth = EgaAuth(token=cfg.token)
        else:
            secret = cfg.client_secret
            username, password = cfg.username, cfg.password
            if secret is None or cfg.mode == "public_test_account":
                files = await fetch_pyega3_config(client)
                server = files["default_server_file.json"]
                secret = secret or SecretStr(server["client_secret"])
                if cfg.mode == "public_test_account":
                    cred = files["default_credential_file.json"]
                    username, password = cred["username"], SecretStr(cred["password"])
            auth = EgaAuth(
                grant=EgaPasswordGrant(
                    username=username or "", password=password, client_secret=secret
                )
            )
        self._auth[key] = auth
        return auth
