"""Structured error codes, the exception type used by every tool path, and redaction.

Handlers raise `GenomicsError` (or a subclass). The service converts it into an
`ErrorInfo` inside the result envelope. Anything else escaping a handler becomes
`internal_error`; it is never turned into an empty success.
"""

from __future__ import annotations

import re
import threading
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"
    PREPARATION_REQUIRED = "preparation_required"
    UPSTREAM_ERROR = "upstream_error"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    CONSENT_REQUIRED = "consent_required"
    INTERNAL_ERROR = "internal_error"


_RETRYABLE_BY_DEFAULT = {ErrorCode.UPSTREAM_ERROR, ErrorCode.TIMEOUT}


class ErrorInfo(BaseModel):
    """Serializable error. `message` and `details` are redacted before construction."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    source: str | None = Field(default=None, description="Source or component that failed.")
    retryable: bool = False
    hint: str | None = Field(default=None, description="What the caller can do next.")
    details: dict[str, Any] = Field(default_factory=dict)


class GenomicsError(Exception):
    """Base exception carrying a structured, already-redacted error."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        source: str | None = None,
        retryable: bool | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        code = code or self.code
        self.info = ErrorInfo(
            code=code,
            message=redact(message),
            source=source,
            retryable=(code in _RETRYABLE_BY_DEFAULT) if retryable is None else retryable,
            hint=redact(hint) if hint else None,
            details=redact_obj(details or {}),
        )
        super().__init__(self.info.message)

    @property
    def error_code(self) -> ErrorCode:
        return self.info.code


class NotFoundError(GenomicsError):
    code = ErrorCode.NOT_FOUND


class UnauthorizedError(GenomicsError):
    code = ErrorCode.UNAUTHORIZED


class UnsupportedError(GenomicsError):
    code = ErrorCode.UNSUPPORTED


class InvalidInputError(GenomicsError):
    code = ErrorCode.INVALID_INPUT


class PreparationRequiredError(GenomicsError):
    code = ErrorCode.PREPARATION_REQUIRED


class UpstreamError(GenomicsError):
    code = ErrorCode.UPSTREAM_ERROR


class DeadlineExceededError(GenomicsError):
    code = ErrorCode.TIMEOUT


class BudgetExceededError(GenomicsError):
    code = ErrorCode.BUDGET_EXCEEDED


class ConsentRequiredError(GenomicsError):
    code = ErrorCode.CONSENT_REQUIRED


# --------------------------------------------------------------------------- redaction

REDACTED = "[REDACTED]"

_SENSITIVE_PARAM = re.compile(
    r"^(x-amz-[a-z-]+|signature|sig|token|access_token|id_token|refresh_token|api[_-]?key|key|"
    r"apikey|password|passwd|secret|client_secret|policy|key-pair-id|credential|auth|"
    r"authorization|session|sessionid|se|sp|sv|skoid|sktid|code)$",
    re.IGNORECASE,
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential|access[_-]?key|"
    r"private[_-]?key|cookie)"
)
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_AWS_KEY_ID = re.compile(r"\b(AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}\b")
_KV_SECRET = re.compile(
    r"(?i)\b(aws_secret_access_key|aws_session_token|secret_access_key|session_token|api_key|"
    r"password|client_secret)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)

_secrets_lock = threading.Lock()
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember a configured secret so any later occurrence is redacted. Short values are ignored."""
    if value and len(value) >= 6:
        with _secrets_lock:
            _registered_secrets.add(value)


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{REDACTED}@{netloc.rsplit('@', 1)[1]}"
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(k.lower().startswith("x-amz-") for k, _ in pairs):
            query = REDACTED
        else:
            query = urlencode(
                [(k, REDACTED if _SENSITIVE_PARAM.match(k) else v) for k, v in pairs], safe="[]"
            )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_url(url: str) -> str:
    """Remove userinfo and signed/secret query values from a URL."""
    return _redact_url(url)


def redact(text: str | None) -> str:
    """Redact credentials, signed URL parameters, bearer tokens and registered secrets."""
    if not text:
        return text or ""
    with _secrets_lock:
        secrets = sorted(_registered_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    text = _URL.sub(lambda m: _redact_url(m.group(0)), text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _AWS_KEY_ID.sub(REDACTED, text)
    text = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return text


def redact_obj(value: Any) -> Any:
    """Recursively redact strings inside JSON-like data; values under secret-like keys are dropped."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEY.search(k):
                out[k] = REDACTED
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(value, list | tuple):
        return [redact_obj(v) for v in value]
    return value
