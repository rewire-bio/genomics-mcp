"""Bounded HTTP access for public archive/catalog APIs.

Every source client receives an injected `httpx.AsyncClient`. `SourceHttp` adds:
per-source host allowlist, request spacing (rate limit), bounded retries for 429/5xx
honouring Retry-After within the deadline, bounded body reads, manual redirects that
only follow allowlisted hosts and drop Authorization across origins, and mapping of
HTTP failures to source-native error codes (401/403 unauthorized, 404 not_found, ...).

Clients built by `make_client` use `trust_env=False`: no proxy variables, no .netrc,
no ambient credentials of any kind.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from genomics_mcp.archives._common.errors import (
    BudgetExceededError,
    DeadlineExceededError,
    InvalidInputError,
    NotFoundError,
    SourceError,
    UnauthorizedError,
    UpstreamError,
)
from genomics_mcp.archives._common.redact import redact, redact_url
from genomics_mcp.archives._common.workspace import open_temp, private_dir  # noqa: F401 (re-export)
from genomics_mcp.security import check_network_destination

USER_AGENT = "rewire-genomics-mcp/0.1 (+https://github.com/rewire-bio/genomics-mcp)"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_BODY = 8 * 1024 * 1024
MAX_REDIRECTS = 5
_RETRY_STATUS = {429, 500, 502, 503, 504}
_VERSION_HEADERS = ("ega-api-version", "x-datasets-version", "x-ncbi-total-count", "etag")


def make_client(*, timeout_s: float = DEFAULT_TIMEOUT_S) -> httpx.AsyncClient:
    """An httpx client that ignores environment proxies/.netrc and never follows redirects itself."""
    return httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
        headers={"User-Agent": USER_AGENT},
    )


@dataclass(frozen=True)
class SourcePolicy:
    name: str
    allowed_hosts: frozenset[str]
    min_interval_s: float = 0.2
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_retries: int = 2
    max_body_bytes: int = DEFAULT_MAX_BODY
    max_retry_after_s: float = 10.0
    terms_url: str | None = None


@dataclass
class HttpResult:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes
    redirects: list[str] = field(default_factory=list)

    def json(self) -> Any:
        try:
            return json.loads(self.body) if self.body.strip() else None
        except ValueError as exc:
            raise UpstreamError(
                "source returned malformed JSON", details={"url": redact_url(self.url)}
            ) from exc

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def version_headers(self) -> dict[str, str]:
        return {k: self.headers[k] for k in _VERSION_HEADERS if k in self.headers}


def host_is_blocked(host: str) -> bool:
    """Instance metadata, link-local, loopback and private IP literals are never fetched."""
    h = host.strip("[]").lower()
    if h in {"localhost", "metadata.google.internal", "metadata"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (
        ip.is_link_local or ip.is_loopback or ip.is_private or ip.is_unspecified or ip.is_multicast
    )


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urlsplit(url)
    port = p.port or {"https": 443, "http": 80}.get(p.scheme)
    return p.scheme, (p.hostname or "").lower(), port


class RateLimiter:
    """Spaces request starts by `min_interval_s`. Loop-agnostic (no lock) so one instance can be
    shared across calls: the slot is reserved synchronously before any await."""

    def __init__(self, min_interval_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._next = 0.0

    async def wait(self) -> None:
        now = self._clock()
        slot = max(now, self._next)
        self._next = slot + self.min_interval_s
        if slot > now:
            await asyncio.sleep(slot - now)


def _source_message(body: bytes) -> str | None:
    """Short upstream error text (JSON `message`/`error`, else the first 200 chars)."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        text = body[:200].decode("utf-8", errors="replace").strip()
        return None if text.lstrip().startswith("<") else text
    if isinstance(data, dict):
        inner = data.get("htsget") if isinstance(data.get("htsget"), dict) else data
        for key in ("message", "error", "detail", "title"):
            if isinstance(inner.get(key), str):
                return inner[key][:300]
    return None


def _source_error_code(body: bytes) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict):
        inner = data.get("htsget") if isinstance(data.get("htsget"), dict) else data
        err = inner.get("error")
        return err if isinstance(err, str) else None
    return None


def status_error(source: str, status: int, url: str, body: bytes = b"") -> SourceError:
    msg = _source_message(body)
    details: dict[str, Any] = {"http_status": status, "url": redact_url(url)}
    if code := _source_error_code(body):
        details["source_error"] = code
    if msg:
        details["source_message"] = redact(msg)
    if status == 401:
        return UnauthorizedError(
            f"{source}: authentication required or invalid",
            source=source,
            details=details,
            hint="Supply explicit credentials for this source; ambient credentials are never used.",
        )
    if status == 403:
        return UnauthorizedError(
            f"{source}: permission denied",
            source=source,
            details=details,
            hint="The configured account is not authorised for this record.",
        )
    if status in (404, 410):
        return NotFoundError(f"{source}: record not found", source=source, details=details)
    if status in (400, 422):
        return InvalidInputError(
            f"{source}: request rejected: {msg or status}", source=source, details=details
        )
    if status == 429:
        return UpstreamError(f"{source}: rate limited (HTTP 429)", source=source, details=details)
    return UpstreamError(f"{source}: upstream HTTP {status}", source=source, details=details)


class SourceHttp:
    def __init__(
        self,
        client: httpx.AsyncClient,
        policy: SourcePolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.client = client
        self.policy = policy
        self.limiter = limiter or RateLimiter(policy.min_interval_s, clock)
        self._clock = clock

    # -- validation --------------------------------------------------------------
    def check_url(self, url: str, *, allowed_hosts: frozenset[str] | None = None) -> None:
        p = urlsplit(url)
        hosts = allowed_hosts if allowed_hosts is not None else self.policy.allowed_hosts
        host = (p.hostname or "").lower()
        if p.scheme != "https":
            raise InvalidInputError(
                f"{self.policy.name}: only https URLs are fetched",
                source=self.policy.name,
                details={"url": redact_url(url)},
            )
        if not host or host_is_blocked(host) or host not in hosts:
            raise InvalidInputError(
                f"{self.policy.name}: host {host or '(none)'} is not an approved source host",
                source=self.policy.name,
                details={"url": redact_url(url), "allowed_hosts": sorted(hosts)},
            )
        try:  # core boundary check (metadata endpoints, link-local literals) as well
            check_network_destination(url, source=self.policy.name, allowed_hosts=hosts)
        except Exception as exc:
            raise InvalidInputError(
                f"{self.policy.name}: destination refused ({host})", source=self.policy.name
            ) from exc

    # -- requests ----------------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        ok: tuple[int, ...] = (200,),
        max_body: int | None = None,
        timeout_s: float | None = None,
        allowed_hosts: frozenset[str] | None = None,
    ) -> HttpResult:
        """Request with retries and bounded body. Raises a SourceError for any non-`ok` status.

        `max_body=None` uses the policy default; `0` or less means no budget left, so no request."""
        if max_body is not None and max_body <= 0:
            raise BudgetExceededError(
                f"{self.policy.name}: no byte budget left for this request",
                source=self.policy.name,
                details={"url": redact_url(url)},
            )
        timeout_s = timeout_s or self.policy.timeout_s
        deadline = self._clock() + timeout_s
        try:
            async with asyncio.timeout(timeout_s):
                return await self._request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    data=data,
                    ok=ok,
                    max_body=self.policy.max_body_bytes if max_body is None else max_body,
                    deadline=deadline,
                    allowed_hosts=allowed_hosts,
                )
        except TimeoutError as exc:
            raise DeadlineExceededError(
                f"{self.policy.name}: no response within {timeout_s:g}s",
                source=self.policy.name,
                details={"url": redact_url(url)},
            ) from exc

    async def _request(
        self, method, url, *, params, headers, data, ok, max_body, deadline, allowed_hosts
    ):
        attempt = 0
        while True:
            try:
                result = await self._send_following(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    data=data,
                    max_body=max_body,
                    allowed_hosts=allowed_hosts,
                )
            except httpx.TimeoutException as exc:
                raise DeadlineExceededError(
                    f"{self.policy.name}: request timed out",
                    source=self.policy.name,
                    details={"url": redact_url(url)},
                ) from exc
            except httpx.TransportError as exc:
                if attempt < self.policy.max_retries:
                    attempt += 1
                    await asyncio.sleep(
                        min(2.0**attempt * 0.25, max(0.0, deadline - self._clock()))
                    )
                    continue
                raise UpstreamError(
                    f"{self.policy.name}: connection failed ({type(exc).__name__})",
                    source=self.policy.name,
                    details={"url": redact_url(url)},
                ) from exc
            if result.status in ok:
                return result
            if result.status in _RETRY_STATUS and attempt < self.policy.max_retries:
                wait = self._retry_after(result.headers.get("retry-after"), attempt)
                if wait is not None and self._clock() + wait < deadline:
                    attempt += 1
                    await asyncio.sleep(wait)
                    continue
            raise status_error(self.policy.name, result.status, result.url, result.body)

    def _retry_after(self, value: str | None, attempt: int) -> float | None:
        if value is None:
            return min(0.5 * 2**attempt, self.policy.max_retry_after_s)
        try:
            wait = float(value)
        except ValueError:
            return None
        return wait if 0 <= wait <= self.policy.max_retry_after_s else None

    async def _send_following(self, method, url, *, params, headers, data, max_body, allowed_hosts):
        redirects: list[str] = []
        current = url
        hdrs = dict(headers or {})
        origin = _origin(url)
        for _ in range(MAX_REDIRECTS + 1):
            self.check_url(current, allowed_hosts=allowed_hosts)
            if _origin(current) != origin:
                hdrs = {
                    k: v for k, v in hdrs.items() if k.lower() not in ("authorization", "cookie")
                }
            await self.limiter.wait()
            req = self.client.build_request(
                method, current, params=params if not redirects else None, headers=hdrs, data=data
            )
            resp = await self.client.send(req, stream=True, follow_redirects=False)
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    redirects.append(redact_url(current))
                    current = urljoin(str(resp.url), resp.headers["location"])
                    if resp.status_code == 303:
                        method, data = "GET", None
                    continue
                body = await _read_bounded(resp, max_body, self.policy.name, str(resp.url))
                return HttpResult(resp.status_code, str(resp.url), resp.headers, body, redirects)
            finally:
                await resp.aclose()
        raise UpstreamError(
            f"{self.policy.name}: too many redirects",
            source=self.policy.name,
            details={"url": redact_url(url)},
        )

    @asynccontextmanager
    async def open_range_stream(
        self,
        url: str,
        *,
        start: int,
        end: int,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream bytes [start, end) of `url` with an explicit Range; no redirects are followed.

        The server must answer 206 starting exactly at `start`; the iterator never yields more
        than `end - start` bytes. Used by transfer backends (resume = a later `start`)."""
        if start < 0 or end <= start:
            raise InvalidInputError(
                f"{self.policy.name}: invalid byte range", source=self.policy.name
            )
        self.check_url(url)
        await self.limiter.wait()
        hdrs = {**(headers or {}), "Range": f"bytes={start}-{end - 1}"}
        req = self.client.build_request("GET", url, params=params, headers=hdrs)
        resp = await self.client.send(req, stream=True, follow_redirects=False)
        try:
            if resp.status_code != 206:
                body = await _read_bounded(
                    resp, 64 * 1024, self.policy.name, str(resp.url), strict=False
                )
                raise (
                    status_error(self.policy.name, resp.status_code, str(resp.url), body)
                    if resp.status_code >= 300
                    else UpstreamError(
                        f"{self.policy.name}: server ignored the Range request (HTTP {resp.status_code})",
                        source=self.policy.name,
                    )
                )
            if not resp.headers.get("content-range", "").startswith(f"bytes {start}-"):
                raise UpstreamError(
                    f"{self.policy.name}: Content-Range does not start at {start}",
                    source=self.policy.name,
                )

            async def body_iter() -> AsyncIterator[bytes]:
                left = end - start
                async for chunk in resp.aiter_bytes():
                    if left <= 0:
                        break
                    piece = chunk[:left]
                    left -= len(piece)
                    yield piece

            yield body_iter()
        finally:
            await resp.aclose()

    async def get_json(self, url: str, **kw: Any) -> tuple[Any, HttpResult]:
        headers = {
            "Accept": "application/json",
            **(kw.pop("headers", None) or {}),
        }  # caller may override
        res = await self.request("GET", url, headers=headers, **kw)
        return res.json(), res

    async def get_text(self, url: str, **kw: Any) -> tuple[str, HttpResult]:
        res = await self.request("GET", url, **kw)
        return res.text, res

    async def resolve_redirect(
        self, url: str, *, allowed_hosts: frozenset[str] | None = None
    ) -> str:
        """Return the final URL after allowlisted redirects (issues one GET with Range 0-0)."""
        res = await self.request(
            "GET",
            url,
            headers={"Range": "bytes=0-0"},
            ok=(200, 206),
            max_body=1024 * 1024,
            allowed_hosts=allowed_hosts,
        )
        return res.url

    async def probe_range(
        self, url: str, *, nbytes: int = 64, allowed_hosts: frozenset[str] | None = None
    ) -> RangeProbe:
        """GET bytes=0..nbytes-1 and report whether the server honoured the range."""
        res = await self.request(
            "GET",
            url,
            headers={"Range": f"bytes=0-{nbytes - 1}"},
            ok=(200, 206),
            max_body=max(nbytes, 1024 * 1024),
            allowed_hosts=allowed_hosts,
        )
        total = None
        cr = res.headers.get("content-range", "")
        if res.status == 206 and "/" in cr:
            tail = cr.rsplit("/", 1)[1]
            total = int(tail) if tail.isdigit() else None
        return RangeProbe(
            url=res.url,
            status=res.status,
            range_supported=res.status == 206 and cr.startswith("bytes 0-"),
            total_size=total,
            head=res.body[:nbytes],
            etag=res.headers.get("etag"),
        )

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        budget_bytes: int,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        expected_size: int | None = None,
        timeout_s: float | None = None,
        allowed_hosts: frozenset[str] | None = None,
    ) -> Download:
        """Stream a whole file to `dest` (private, atomic) within `budget_bytes`."""
        if expected_size is not None and expected_size > budget_bytes:
            raise BudgetExceededError(
                f"{self.policy.name}: file is {expected_size} bytes, over the {budget_bytes}-byte budget",
                source=self.policy.name,
                hint="Pass an explicit larger transfer budget.",
                details={"size_bytes": expected_size, "budget_bytes": budget_bytes},
            )
        timeout_s = timeout_s or max(self.policy.timeout_s, 600.0)
        try:
            async with asyncio.timeout(timeout_s):
                return await self._download(url, dest, budget_bytes, headers, params, allowed_hosts)
        except httpx.TransportError as exc:
            raise UpstreamError(
                f"{self.policy.name}: download interrupted ({type(exc).__name__}); partial file removed",
                source=self.policy.name,
                details={"url": redact_url(url)},
            ) from exc
        except TimeoutError as exc:
            raise DeadlineExceededError(
                f"{self.policy.name}: download exceeded {timeout_s:g}s",
                source=self.policy.name,
                details={"url": redact_url(url)},
            ) from exc

    async def _download(self, url, dest, budget, headers, params, allowed_hosts) -> Download:
        current, hdrs, origin = url, dict(headers or {}), _origin(url)
        for _ in range(MAX_REDIRECTS + 1):
            self.check_url(current, allowed_hosts=allowed_hosts)
            if _origin(current) != origin:
                hdrs = {
                    k: v for k, v in hdrs.items() if k.lower() not in ("authorization", "cookie")
                }
            await self.limiter.wait()
            req = self.client.build_request(
                "GET", current, params=params if current == url else None, headers=hdrs
            )
            resp = await self.client.send(req, stream=True, follow_redirects=False)
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    current = urljoin(str(resp.url), resp.headers["location"])
                    continue
                if resp.status_code != 200 and not _is_whole_206(resp):
                    body = await _read_bounded(
                        resp, 64 * 1024, self.policy.name, str(resp.url), strict=False
                    )
                    raise status_error(self.policy.name, resp.status_code, str(resp.url), body)
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > budget:
                    raise BudgetExceededError(
                        f"{self.policy.name}: response is {declared} bytes, over the {budget}-byte budget",
                        source=self.policy.name,
                        details={"budget_bytes": budget},
                    )
                return await _stream_to(resp, dest, budget, self.policy.name, url)
            finally:
                await resp.aclose()
        raise UpstreamError(f"{self.policy.name}: too many redirects", source=self.policy.name)


def _is_whole_206(resp: httpx.Response) -> bool:
    """Accept a 206 only when it starts at byte 0 and matches any Range we sent.

    EGA /files answers even unranged GETs with 206 and has reported internally inconsistent
    Content-Range values; callers must verify checksums after download."""
    if resp.status_code != 206:
        return False
    cr = resp.headers.get("content-range", "").strip()
    sent = resp.request.headers.get("range")
    if sent:
        m = re.fullmatch(r"bytes=0-(\d+)", sent.strip())
        return bool(m) and cr.startswith(f"bytes 0-{m.group(1)}/")
    return cr.startswith("bytes 0-")


@dataclass
class RangeProbe:
    url: str
    status: int
    range_supported: bool
    total_size: int | None
    head: bytes
    etag: str | None


@dataclass
class Download:
    path: Path
    size_bytes: int
    md5: str
    sha256: str
    final_url: str


async def _read_bounded(
    resp: httpx.Response, limit: int, source: str, url: str, *, strict: bool = True
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():  # decoded bytes: bounds decompressed size too
        total += len(chunk)
        if total > limit:
            if not strict:
                chunks.append(chunk[: max(0, limit - (total - len(chunk)))])
                break
            raise BudgetExceededError(
                f"{source}: response exceeded {limit} bytes",
                source=source,
                details={"url": redact_url(url), "limit_bytes": limit},
                hint="Request a smaller page.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _stream_to(
    resp: httpx.Response, dest: Path, budget: int, source: str, url: str
) -> Download:
    fd, tmp = open_temp(dest)
    md5, sha = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > budget:
                    raise BudgetExceededError(
                        f"{source}: download exceeded the {budget}-byte budget",
                        source=source,
                        details={"budget_bytes": budget, "url": redact_url(url)},
                        hint="Pass an explicit larger transfer budget.",
                    )
                fh.write(chunk)
                md5.update(chunk)
                sha.update(chunk)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return Download(dest, size, md5.hexdigest(), sha.hexdigest(), str(resp.url))


def file_digests(path: Path) -> tuple[str, str]:
    md5, sha = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()
