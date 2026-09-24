"""Static bearer-token ASGI middleware for the Streamable HTTP transport.

Every HTTP request, including the MCP endpoint, requires `Authorization: Bearer <token>`.
Comparison is constant-time over SHA-256 digests; the token is never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("genomics_mcp.auth")


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: SecretStr) -> None:
        self.app = app
        self._digest = hashlib.sha256(token.get_secret_value().encode()).digest()

    def _authorized(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                scheme, _, credential = value.decode("latin-1").partition(" ")
                if scheme.lower() != "bearer" or not credential:
                    return False
                digest = hashlib.sha256(credential.strip().encode()).digest()
                return hmac.compare_digest(digest, self._digest)
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        log.warning("rejected unauthenticated %s request", scope["type"])
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": "unauthorized"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="genomics-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
