"""Redaction for URLs, tokens and registered secrets.

Mirrors core `genomics_mcp.errors.redact`; kept local so these clients run before
core is merged. Integration should also call core `register_secret` for EGA tokens.
"""

from __future__ import annotations

import re
import threading
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SENSITIVE_PARAM = re.compile(
    r"^(x-amz-[a-z-]+|signature|sig|token|access_token|id_token|refresh_token|api[_-]?key|key|"
    r"apikey|password|passwd|secret|client_secret|policy|key-pair-id|credential|auth|"
    r"authorization|session|awsaccesskeyid|expires|se|sp|sv|si|sr|skoid|sktid|code)$",
    re.IGNORECASE,
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential|access[_-]?key|"
    r"private[_-]?key|cookie)"
)
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")

_lock = threading.Lock()
_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        with _lock:
            _secrets.add(value)


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if parts.scheme == "data":
        return "data:[inline]"
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{REDACTED}@{netloc.rsplit('@', 1)[1]}"
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(k.lower().startswith("x-amz-") or k.lower() == "signature" for k, _ in pairs):
            query = REDACTED
        else:
            query = urlencode(
                [(k, REDACTED if _SENSITIVE_PARAM.match(k) else v) for k, v in pairs], safe="[]*"
            )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    text = _URL.sub(lambda m: redact_url(m.group(0)), text)
    text = _JWT.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return text


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _SENSITIVE_KEY.search(k) else redact_obj(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_obj(v) for v in value]
    return value
