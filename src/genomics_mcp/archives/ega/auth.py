"""EGA download-service authentication.

Only explicit credentials are used: a bearer token supplied by the caller, or an
OpenID password grant configured explicitly (e.g. the documented public test account
shipped in pyega3's `default_credential_file.json` / `default_server_file.json`, loaded
from paths the user names). Nothing is read from the environment or home directory
implicitly. Tokens are registered for redaction and never logged or returned.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import SecretStr

from genomics_mcp.archives._common.errors import InvalidInputError, UnauthorizedError, UpstreamError
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy
from genomics_mcp.archives._common.redact import register_secret

SOURCE = "ega"
PYEGA3_CLIENT_ID = "f20cd2d3-682a-4568-a53e-4262ef54c8f4"
"""Public OpenID client id of the official pyega3 client (pyega3/libs/auth_client.py)."""
DEFAULT_TOKEN_URL = "https://ega.ebi.ac.uk:8443/ega-openid-connect-server/token"
AUTH_HOSTS = frozenset({"ega.ebi.ac.uk"})


@dataclass
class EgaPasswordGrant:
    username: str
    password: SecretStr
    client_secret: SecretStr
    client_id: str = PYEGA3_CLIENT_ID
    token_url: str = DEFAULT_TOKEN_URL

    def __post_init__(self) -> None:
        # Every construction path registers the secrets before any request can echo them.
        register_secret(self.password.get_secret_value())
        register_secret(self.client_secret.get_secret_value())

    @classmethod
    def from_pyega3_files(cls, server_file: Path, credential_file: Path) -> EgaPasswordGrant:
        """Load an explicit pyega3 server/credential file pair (e.g. the public test account)."""
        try:
            server = json.loads(Path(server_file).read_text())
            cred = json.loads(Path(credential_file).read_text())
            grant = cls(
                username=cred["username"],
                password=SecretStr(cred["password"]),
                client_secret=SecretStr(server["client_secret"]),
                token_url=server.get("url_auth", DEFAULT_TOKEN_URL),
            )
        except (OSError, ValueError, KeyError) as exc:
            raise InvalidInputError(
                "EGA credential/server file is unreadable or missing fields", source=SOURCE
            ) from exc
        return grant


@dataclass
class EgaAuth:
    """Supplies `Authorization` headers for EGA download/htsget hosts only."""

    token: SecretStr | None = None
    grant: EgaPasswordGrant | None = None
    _expires_at: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if self.token is None and self.grant is None:
            raise InvalidInputError("EgaAuth needs an explicit token or password grant", source=SOURCE)
        if self.token is not None:
            register_secret(self.token.get_secret_value())
            self._expires_at = float("inf")

    @property
    def mode(self) -> str:
        return "password_grant" if self.grant else "bearer_token"

    async def bearer(self, http: SourceHttp) -> str:
        if self.token is not None and time.monotonic() < self._expires_at:
            return self.token.get_secret_value()
        if self.grant is None:
            raise UnauthorizedError("EGA token expired; supply a new token", source=SOURCE)
        g = self.grant
        auth_http = SourceHttp(http.client, SourcePolicy(
            name=SOURCE, allowed_hosts=AUTH_HOSTS, min_interval_s=0.2, max_retries=1,
            max_body_bytes=256 * 1024,
        ))
        try:
            res = await auth_http.request(
                "POST", g.token_url, ok=(200, 400, 401, 403),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={
                    "grant_type": "password",
                    "client_id": g.client_id,
                    "scope": "openid",
                    "client_secret": g.client_secret.get_secret_value(),
                    "username": g.username,
                    "password": g.password.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:  # pragma: no cover - SourceHttp maps transport errors
            raise UpstreamError("EGA token service unreachable", source=SOURCE) from exc
        if res.status != 200:
            # Upstream auth error bodies may echo submitted values; keep only the status and a
            # short OAuth error code if it is a plain token (e.g. invalid_grant).
            code = None
            try:
                body = res.json()
                raw = body.get("error") if isinstance(body, dict) else None
                code = raw if isinstance(raw, str) and raw.replace("_", "").isalpha() and len(raw) < 40 \
                    else None
            except Exception:  # noqa: BLE001 - body is never surfaced
                code = None
            details = {"http_status": res.status}
            if code:
                details["source_error"] = code
            raise UnauthorizedError("EGA authentication failed", source=SOURCE, details=details,
                                    hint="Check the explicitly configured EGA credentials.")
        data = res.json() or {}
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise UpstreamError("EGA token response had no access_token", source=SOURCE)
        register_secret(token)
        self.token = SecretStr(token)
        expires_in = data.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
        self._expires_at = time.monotonic() + max(30.0, lifetime - 60.0)
        return token
