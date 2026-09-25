"""Rate-limited, deadline-aware HTTP access to public reference sources.

All requests go through an injected ``httpx.AsyncClient`` so callers control
transport, proxies and tests. Only read-only requests are issued (GET, and POST
for GraphQL queries), so retrying them is safe.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import httpx

from genomics_mcp.errors import GenomicsError
from genomics_mcp.errors import redact as core_redact
from genomics_mcp.security import check_network_destination

from .models import ErrorKind, SourceError

USER_AGENT = "rewire-genomics-mcp-references/0.1 (+https://github.com/rewire-bio/genomics-mcp)"

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
ERROR_BODY_BYTES = 64 * 1024
_DROP_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
MAX_RETRY_AFTER_SECONDS = 10.0

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|token|key|signature|x-amz-[a-z-]+|sig|secret|password)=([^&\s\"']+)"
)


def redact_text(text: str) -> str:
    """Local key=value redaction plus the core redactor (URLs, bearer tokens, registered secrets)."""
    return core_redact(_SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=REDACTED", text))


def redact_url(url: str | httpx.URL) -> str:
    """Return scheme, host and path only; query strings are never reported."""
    parsed = httpx.URL(str(url))
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.host}{port}{parsed.path}"


class SourceFailure(Exception):
    """Raised by adapters; carries a structured, redacted ``SourceError``."""

    def __init__(self, error: SourceError):
        super().__init__(error.message)
        self.error = error


def failure(
    source: str,
    operation: str,
    kind: ErrorKind,
    message: str,
    *,
    status_code: int | None = None,
    retryable: bool = False,
    url: str | None = None,
) -> SourceFailure:
    return SourceFailure(
        SourceError(
            source=source,
            operation=operation,
            kind=kind,
            message=redact_text(message),
            status_code=status_code,
            retryable=retryable,
            url=url,
        )
    )


class RateLimiter:
    """Sliding-window limiter: at most ``rate`` acquisitions per ``per`` seconds.

    If the wait would pass the caller's deadline the limiter fails immediately
    instead of blocking past the interactive budget.
    """

    def __init__(
        self,
        rate: int,
        per: float,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ):
        if rate < 1 or per <= 0:
            raise ValueError("rate must be >= 1 and per > 0")
        self.rate = rate
        self.per = per
        self._clock = clock
        self._sleep = sleep
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    def describe(self) -> str:
        if self.per == 1:
            return f"{self.rate}/second"
        if self.per == 60:
            return f"{self.rate}/minute"
        return f"{self.rate}/{self.per:g}s"

    async def acquire(self, deadline: float | None = None) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._times and now - self._times[0] >= self.per:
                    self._times.popleft()
                if len(self._times) < self.rate:
                    self._times.append(now)
                    return
                wait = self.per - (now - self._times[0])
                if deadline is not None and now + wait > deadline:
                    raise RateBudgetExceeded(wait)
                await self._sleep(wait)


class RateBudgetExceeded(Exception):
    def __init__(self, wait: float):
        super().__init__(f"local rate limit requires waiting {wait:.1f}s")
        self.wait = wait


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def upstream_message(response: httpx.Response) -> str:
    """A short, redacted description of an error response body."""
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    detail = ""
    if "json" in content_type:
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        if isinstance(body, dict):
            for key in ("error", "message", "messages", "errors", "detail"):
                if key in body:
                    value = body[key]
                    if isinstance(value, dict):
                        value = value.get("message", value)
                    detail = json.dumps(value) if not isinstance(value, str) else value
                    break
    elif content_type.startswith("text/plain"):
        detail = response.text
    if detail:
        detail = " ".join(detail.split())[:300]
        return redact_text(f"HTTP {response.status_code}: {detail}")
    kind = content_type or "unknown content type"
    return f"HTTP {response.status_code} with non-JSON body ({kind})"


def status_kind(status: int) -> ErrorKind:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status in (400, 422):
        return "invalid_input"
    return "upstream"


class BodyTooLarge(Exception):
    pass


async def read_bounded(response: httpx.Response, limit: int, *, truncate: bool = False) -> bytes:
    """Read a streamed body, stopping at ``limit`` decoded bytes.

    Raises ``BodyTooLarge`` (or truncates, for error bodies) instead of buffering
    past the limit. A declared Content-Length above the limit fails before reading.
    """
    declared = response.headers.get("content-length")
    if not truncate and declared and declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge(f"declared Content-Length {declared} exceeds {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            if truncate:
                chunks.append(chunk[: len(chunk) - (size - limit)])
                break
            raise BodyTooLarge(f"response exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class SourceHttp:
    """Per-source request helper sharing a limiter and retry policy.

    Bodies are streamed and bounded by ``max_bytes``; redirects are never
    followed (a redirect status is returned only when listed in ``accept_status``).
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        source: str,
        limiter: RateLimiter,
        timeout: float = 15.0,
        max_retries: int = 2,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        default_headers: Mapping[str, str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allowed_hosts: Iterable[str] | None = None,
        gate: Callable[[str], bool] | None = None,
    ):
        self._gate = gate
        self.max_bytes = max_bytes
        # Hosts this source may contact. Every request URL is checked; redirects are never followed.
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts) if allowed_hosts else None
        self.client = client
        self.source = source
        self.limiter = limiter
        self.timeout = timeout
        self.max_retries = max_retries
        self._clock = clock
        self._sleep = sleep
        self._headers = {"User-Agent": USER_AGENT, **(default_headers or {})}

    async def request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
        accept_status: Iterable[int] = (200,),
        max_bytes: int | None = None,
    ) -> httpx.Response:
        accepted = set(accept_status)
        limit = max_bytes or self.max_bytes
        safe_url = redact_url(url)
        if self._gate is not None and not self._gate(self.source):
            raise failure(
                self.source,
                operation,
                "not_configured",
                f"{self.source} is disabled by configuration or not selected for this call",
            )
        try:
            check_network_destination(url, source=self.source, allowed_hosts=self.allowed_hosts)
        except GenomicsError as exc:
            raise failure(
                self.source, operation, "forbidden", exc.info.message, url=safe_url
            ) from None
        last: SourceFailure | None = None
        for attempt in range(self.max_retries + 1):
            remaining = None if deadline is None else deadline - self._clock()
            if remaining is not None and remaining <= 0:
                raise last or failure(
                    self.source,
                    operation,
                    "timeout",
                    "deadline exhausted before request",
                    url=safe_url,
                )
            try:
                await self.limiter.acquire(deadline)
            except RateBudgetExceeded as exc:
                raise failure(
                    self.source,
                    operation,
                    "rate_limited",
                    f"{exc}; source limit {self.limiter.describe()} exceeds the remaining deadline",
                    retryable=True,
                    url=safe_url,
                ) from None
            timeout = self.timeout if remaining is None else max(0.1, min(self.timeout, remaining))
            retry_wait: float | None = None
            try:
                response = await asyncio.wait_for(
                    self._attempt(
                        method, url, params, json_body, headers, timeout, accepted, limit
                    ),
                    timeout + 1.0,
                )
            except BodyTooLarge as exc:
                raise failure(
                    self.source,
                    operation,
                    "invalid_response",
                    f"{exc}; response not read further",
                    url=safe_url,
                ) from None
            except (httpx.TimeoutException, TimeoutError):
                last = failure(
                    self.source,
                    operation,
                    "timeout",
                    f"no response within {timeout:.1f}s",
                    retryable=True,
                    url=safe_url,
                )
            except httpx.TransportError as exc:
                last = failure(
                    self.source,
                    operation,
                    "upstream",
                    f"transport error: {type(exc).__name__}",
                    retryable=True,
                    url=safe_url,
                )
            else:
                if response.status_code in accepted:
                    return response
                kind = status_kind(response.status_code)
                retryable = response.status_code in RETRYABLE_STATUS
                last = failure(
                    self.source,
                    operation,
                    kind,
                    upstream_message(response),
                    status_code=response.status_code,
                    retryable=retryable,
                    url=safe_url,
                )
                if not retryable:
                    raise last
                retry_wait = _retry_after_seconds(response)
            if attempt >= self.max_retries:
                break
            wait = retry_wait if retry_wait is not None else 0.5 * (2**attempt)
            if wait > MAX_RETRY_AFTER_SECONDS:
                break
            if deadline is not None and self._clock() + wait >= deadline:
                break
            await self._sleep(wait)
        assert last is not None
        raise last

    async def _attempt(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        json_body: Any,
        headers: Mapping[str, str] | None,
        timeout_s: float,
        accepted: set[int],
        limit: int,
    ) -> httpx.Response:
        async with self.client.stream(
            method,
            url,
            params=params,
            json=json_body,
            headers={**self._headers, **(headers or {})},
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        ) as streamed:
            if streamed.status_code in accepted:
                body = await read_bounded(streamed, limit)
            else:
                body = await read_bounded(streamed, ERROR_BODY_BYTES, truncate=True)
            kept = [
                (k, v) for k, v in streamed.headers.multi_items() if k.lower() not in _DROP_HEADERS
            ]
            return httpx.Response(
                streamed.status_code, headers=kept, content=body, request=streamed.request
            )

    async def get_json(self, url: str, *, operation: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, operation=operation, **kwargs)
        return self.decode_json(response, operation)

    def decode_json(self, response: httpx.Response, operation: str) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise failure(
                self.source,
                operation,
                "invalid_response",
                f"expected JSON, got {response.headers.get('content-type', 'unknown content type')}",
                status_code=response.status_code,
                url=redact_url(response.request.url),
            ) from None
