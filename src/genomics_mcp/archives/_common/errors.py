"""Source errors for archive and catalog clients.

Codes use the same strings as core `genomics_mcp.errors.ErrorCode`, so integration
maps `SourceError` to the core exception by code. Messages and details are redacted
on construction; secrets never reach `str(exc)`.
"""

from __future__ import annotations

from typing import Any

from genomics_mcp.archives._common.redact import redact, redact_obj


class SourceError(Exception):
    code = "internal_error"
    retryable_default = False

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        retryable: bool | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = redact(message)
        self.source = source
        self.retryable = self.retryable_default if retryable is None else retryable
        self.hint = redact(hint) if hint else None
        self.details = redact_obj(details or {})
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "retryable": self.retryable,
            "hint": self.hint,
            "details": self.details,
        }


class NotFoundError(SourceError):
    code = "not_found"


class UnauthorizedError(SourceError):
    """Missing/invalid credentials (HTTP 401) or permission denied (HTTP 403).

    `details["http_status"]` and `details["source_error"]` keep the distinction.
    """

    code = "unauthorized"


class UnsupportedError(SourceError):
    code = "unsupported"


class InvalidInputError(SourceError):
    code = "invalid_input"


class PreparationRequiredError(SourceError):
    code = "preparation_required"


class UpstreamError(SourceError):
    code = "upstream_error"
    retryable_default = True


class DeadlineExceededError(SourceError):
    code = "timeout"
    retryable_default = True


class BudgetExceededError(SourceError):
    code = "budget_exceeded"
