"""HTTP client for public source APIs.

Every request is bounded (response bytes, deadline), rate/concurrency limited per
source, retried only when safe, and carries provenance. A request built from
private-file-derived values is refused unless the caller gave per-call consent.
The client ignores proxy/netrc environment (`trust_env=False`) and never sends
cloud credentials.
"""

from __future__ import annotations

import asyncio
import dataclasses
import email.utils
import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from genomics_mcp import __version__
from genomics_mcp.config import MiB, Settings
from genomics_mcp.errors import (
    BudgetExceededError,
    ConsentRequiredError,
    DeadlineExceededError,
    GenomicsError,
    InvalidInputError,
    NotFoundError,
    UnauthorizedError,
    UnsupportedError,
    UpstreamError,
    redact_url,
)
from genomics_mcp.models import FileRef, Provenance, Visibility

log = logging.getLogger("genomics_mcp.public")

USER_AGENT = f"rewire-genomics-mcp/{__version__} (+https://github.com/rewire-bio/genomics-mcp)"
RETRY_STATUSES = frozenset({429, 502, 503, 504})


class Deadline:
    """Absolute monotonic deadline shared by the steps of one tool call."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.expires = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self.expires - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0

    def ensure(self, what: str = "operation") -> float:
        left = self.remaining()
        if left <= 0:
            raise DeadlineExceededError(f"{what} exceeded the {self.seconds:g}s deadline")
        return left


class EgressContext(BaseModel):
    """Declares whether outgoing query values were derived from private data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    derived_from_private: bool = False
    consent: bool = False

    @classmethod
    def public(cls) -> EgressContext:
        """Query values came from the caller's own tool arguments or public sources."""
        return cls()

    @classmethod
    def for_files(cls, files: Iterable[FileRef], *, consent: bool) -> EgressContext:
        private = any(f.visibility is Visibility.PRIVATE for f in files)
        return cls(derived_from_private=private, consent=consent)

    def check(self, source: str) -> None:
        if self.derived_from_private and not self.consent:
            raise ConsentRequiredError(
                f"query to {source} would send values derived from private files",
                source=source,
                hint="pass allow_external_annotation=true for this call to permit it",
            )


@dataclass(frozen=True)
class SourcePolicy:
    """Per-source network policy. Adapters declare defaults; config may override some."""

    name: str
    base_url: str
    allowed_hosts: frozenset[str] = frozenset()
    max_concurrency: int = 4
    requests_per_minute: float | None = None
    timeout_s: float = 30.0
    max_response_bytes: int = 16 * MiB
    max_retries: int = 2
    backoff_s: float = 0.5
    terms_url: str | None = None
    default_headers: Mapping[str, str] = field(default_factory=dict)

    def hosts(self) -> frozenset[str]:
        host = urlsplit(self.base_url).hostname or ""
        return self.allowed_hosts | {host}


class _RateLimiter:
    def __init__(self, per_minute: float | None) -> None:
        self.interval = 60.0 / per_minute if per_minute else 0.0
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def acquire(self, deadline: Deadline, source: str) -> None:
        if not self.interval:
            return
        async with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_at - now)
            if wait > deadline.remaining():
                raise DeadlineExceededError(
                    f"{source} rate limit would delay past the deadline", source=source
                )
            self.next_at = max(now, self.next_at) + self.interval
        if wait:
            await asyncio.sleep(wait)


@dataclass
class PublicResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes
    provenance: Provenance

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except ValueError:
            raise UpstreamError(
                "upstream returned invalid JSON", source=self.provenance.source
            ) from None

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class PublicHttpClient:
    """Shared async client. Create once per server; use `async with` or `aclose()`."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": USER_AGENT},
        )
        self._policies: dict[str, SourcePolicy] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._limiters: dict[str, _RateLimiter] = {}

    async def __aenter__(self) -> PublicHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def policy(self, default: SourcePolicy) -> SourcePolicy:
        """Register (once) and return the effective policy, applying config overrides."""
        existing = self._policies.get(default.name)
        if existing is not None:
            return existing
        cfg = self.settings.sources.get(default.name)
        policy = default
        if cfg is not None:
            if not cfg.enabled:
                raise UnsupportedSourceDisabled(default.name)
            overrides: dict[str, Any] = {}
            if cfg.base_url:
                overrides["base_url"] = cfg.base_url
            if "max_concurrency" in cfg.model_fields_set:
                overrides["max_concurrency"] = cfg.max_concurrency
            if cfg.requests_per_minute:
                # Config may only slow a source down relative to its documented limit.
                rpm = default.requests_per_minute
                overrides["requests_per_minute"] = min(cfg.requests_per_minute, rpm or float("inf"))
            if cfg.timeout_s:
                overrides["timeout_s"] = cfg.timeout_s
            policy = dataclasses.replace(default, **overrides)
        self._policies[policy.name] = policy
        self._semaphores[policy.name] = asyncio.Semaphore(policy.max_concurrency)
        self._limiters[policy.name] = _RateLimiter(policy.requests_per_minute)
        return policy

    async def request(
        self,
        policy: SourcePolicy,
        method: str,
        path_or_url: str,
        *,
        deadline: Deadline,
        egress: EgressContext,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        idempotent: bool | None = None,
        max_response_bytes: int | None = None,
        record_id: str | None = None,
    ) -> PublicResponse:
        """Send one request. Raises a `GenomicsError` subclass on any failure."""
        egress.check(policy.name)
        policy = self.policy(policy)
        method = method.upper()
        url = urljoin(policy.base_url.rstrip("/") + "/", path_or_url)
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or parts.hostname not in policy.hosts():
            raise InvalidInputError(
                f"URL host is not allowed for source {policy.name}",
                source=policy.name,
                details={"url": redact_url(url)},
            )
        if parts.scheme == "http" and not policy.base_url.startswith("http://"):
            raise InvalidInputError("plain http is not allowed for this source", source=policy.name)
        safe = method in ("GET", "HEAD") if idempotent is None else idempotent
        cap = max_response_bytes or policy.max_response_bytes
        hdrs = {**policy.default_headers, **(headers or {})}
        attempts = 1 + (policy.max_retries if safe else 0)

        last_error: GenomicsError | None = None
        for attempt in range(attempts):
            deadline.ensure(f"{policy.name} request")
            await self._limiters[policy.name].acquire(deadline, policy.name)
            try:
                async with self._semaphores[policy.name]:
                    timeout = min(policy.timeout_s, deadline.ensure(f"{policy.name} request"))
                    started = time.monotonic()
                    resp = await self._send(
                        method, url, params, hdrs, json_body, timeout, cap, policy.name
                    )
            except (httpx.TimeoutException, TimeoutError):
                last_error = DeadlineExceededError(
                    f"{policy.name} did not respond within {timeout:g}s", source=policy.name
                )
            except httpx.TransportError as exc:
                last_error = UpstreamError(
                    f"{policy.name} connection failed: {type(exc).__name__}", source=policy.name
                )
            else:
                status, final_url, resp_headers, body = resp
                log.info(
                    "source=%s method=%s host=%s status=%s ms=%d bytes=%d",
                    policy.name,
                    method,
                    urlsplit(final_url).hostname,
                    status,
                    (time.monotonic() - started) * 1000,
                    len(body),
                )
                if status < 400:
                    return PublicResponse(
                        status_code=status,
                        url=redact_url(final_url),
                        headers=resp_headers,
                        content=body,
                        provenance=Provenance(
                            source=policy.name,
                            source_record_id=record_id,
                            url=final_url,
                            method=f"HTTP {method}",
                            retrieved_at=datetime.now(UTC),
                            terms_url=policy.terms_url,
                        ),
                    )
                last_error = _status_error(policy.name, status, body)
                if status not in RETRY_STATUSES:
                    raise last_error
                retry_after = _retry_after(resp_headers.get("retry-after"))
                if retry_after is not None and retry_after >= deadline.remaining():
                    raise last_error
            if attempt + 1 < attempts:
                delay = policy.backoff_s * (2**attempt)
                if delay >= deadline.remaining():
                    break
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def get_json(
        self,
        policy: SourcePolicy,
        path_or_url: str,
        *,
        deadline: Deadline,
        egress: EgressContext,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        record_id: str | None = None,
    ) -> tuple[Any, Provenance]:
        resp = await self.request(
            policy,
            "GET",
            path_or_url,
            deadline=deadline,
            egress=egress,
            params=params,
            headers={"Accept": "application/json", **(headers or {})},
            record_id=record_id,
        )
        return resp.json(), resp.provenance

    async def _send(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        json_body: Any,
        timeout_s: float,
        cap: int,
        source: str,
    ) -> tuple[int, str, dict[str, str], bytes]:
        async with asyncio.timeout(timeout_s):
            async with self._client.stream(
                method, url, params=params, headers=headers, json=json_body, timeout=timeout_s
            ) as resp:
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > cap:
                    raise BudgetExceededError(
                        f"{source} response is {declared} bytes; limit is {cap}", source=source
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > cap:
                        raise BudgetExceededError(
                            f"{source} response exceeded {cap} bytes", source=source
                        )
                    chunks.append(chunk)
                keep = ("content-type", "retry-after", "etag", "last-modified", "content-range")
                hdrs = {k: resp.headers[k] for k in keep if k in resp.headers}
                return resp.status_code, str(resp.url), hdrs, b"".join(chunks)


class UnsupportedSourceDisabled(UnsupportedError):
    def __init__(self, source: str) -> None:
        super().__init__(f"source {source} is disabled in configuration", source=source)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    if value.strip().isdigit():
        return float(value.strip())
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _status_error(source: str, status: int, body: bytes) -> GenomicsError:
    details = {"http_status": status}
    if status in (401, 403):
        return UnauthorizedError(
            f"{source} refused access (HTTP {status})", source=source, details=details
        )
    if status == 404:
        return NotFoundError(
            f"{source} has no such record (HTTP 404)", source=source, details=details
        )
    retryable = status in RETRY_STATUSES
    return UpstreamError(
        f"{source} returned HTTP {status}", source=source, retryable=retryable, details=details
    )
