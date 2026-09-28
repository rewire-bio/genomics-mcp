"""HTTP(S) access for storage: range preflight, bounded range reads and streaming GETs.

- Range support is proven with `GET Range: bytes=0-0` expecting 206 and a Content-Range.
  HEAD/Accept-Ranges are not trusted. A 200 reply is closed without reading the body.
- Redirects are followed manually; every hop is validated (netguard) before it is sent.
  Cross-origin hops carry only safe headers.
- The client ignores proxy/netrc environment (`trust_env=False`) and sends no credentials.
- The final URL is returned for native readers but never logged or returned to callers.
- With a `MemoryCache`, public files are read in 64 KiB blocks reused across calls
  (`read_blocks`); see `genomics_mcp.cache` for when a block may be reused.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from genomics_mcp.cache import (
    BLOCK_BYTES,
    Identity,
    MemoryCache,
    lifetime,
    object_identity,
    plain_url,
)
from genomics_mcp.config import Settings
from genomics_mcp.errors import (
    DeadlineExceededError,
    GenomicsError,
    InvalidInputError,
    NotFoundError,
    PreparationRequiredError,
    UnauthorizedError,
    UpstreamError,
)
from genomics_mcp.public import CROSS_ORIGIN_SAFE_HEADERS, REDIRECT_STATUSES, USER_AGENT
from genomics_mcp.storage.netguard import check_destination, check_peer

log = logging.getLogger("genomics_mcp.storage.http")

MAX_REDIRECTS = 5
MAX_RUN_BLOCKS = 64
"""Most blocks fetched by one upstream range request (4 MiB)."""
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


@dataclass
class Preflight:
    final_url: str
    status: int
    range_capable: bool
    size: int | None
    etag: str | None
    last_modified: str | None
    content_type: str | None
    redirected: bool
    expires_at: datetime | None


def signed_url_expiry(url: str) -> datetime | None:
    """Expiry of a presigned URL (SigV4 X-Amz-Date + X-Amz-Expires, or epoch `Expires`)."""
    params = {k.lower(): v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
    try:
        if "x-amz-date" in params and "x-amz-expires" in params:
            start = datetime.strptime(params["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            return start + timedelta(seconds=int(params["x-amz-expires"]))
        if "expires" in params and params["expires"].isdigit():
            return datetime.fromtimestamp(int(params["expires"]), tz=UTC)
    except ValueError:
        return None
    return None


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urlsplit(url)
    return (
        p.scheme.lower(),
        (p.hostname or "").lower(),
        p.port or {"http": 80, "https": 443}.get(p.scheme.lower()),
    )


def status_error(source: str, status: int, host: str) -> GenomicsError:
    details = {"http_status": status, "host": host}
    if status == 401:
        return UnauthorizedError(
            f"{source}: access refused (HTTP 401); the URL may have expired or need credentials",
            source=source,
            details=details,
        )
    if status == 403:
        return UnauthorizedError(
            f"{source}: access forbidden (HTTP 403)", source=source, details=details
        )
    if status in (404, 410):
        return NotFoundError(
            f"{source}: file not found (HTTP {status})", source=source, details=details
        )
    return UpstreamError(
        f"{source}: server returned HTTP {status}",
        source=source,
        retryable=status in (429, 502, 503, 504),
        details=details,
    )


def parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    if not value:
        return None
    m = _CONTENT_RANGE.match(value.strip())
    if not m:
        return None
    total = None if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), int(m.group(2)), total


class HttpAccess:
    """Shared async client for storage requests. One per server."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: MemoryCache | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache if cache is not None and cache.enabled else None
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @contextlib.asynccontextmanager
    async def open(
        self,
        url: str,
        *,
        source: str,
        headers: dict[str, str] | None = None,
        extra_trusted: Iterable[str] = (),
    ) -> AsyncIterator[tuple[httpx.Response, str, bool]]:
        """Send a GET, following validated redirects. Yields (response, final_url, redirected).

        The response is streaming; the body is not read unless the caller reads it.
        """
        extra = tuple(extra_trusted)
        target = url
        previous: str | None = None
        hdrs = dict(headers or {})
        for hop in range(MAX_REDIRECTS + 1):
            await check_destination(
                target, self.settings, source=source, previous_url=previous, extra_trusted=extra
            )
            request = self._client.build_request("GET", target, headers=hdrs)
            try:
                resp = await self._client.send(request, stream=True)
            except httpx.TimeoutException:
                raise DeadlineExceededError(
                    f"{source}: {urlsplit(target).hostname} did not respond in time", source=source
                ) from None
            except httpx.TransportError as exc:
                raise UpstreamError(
                    f"{source}: connection to {urlsplit(target).hostname} failed "
                    f"({type(exc).__name__})",
                    source=source,
                ) from None
            try:
                stream = resp.extensions.get("network_stream")
                peer = stream.get_extra_info("server_addr") if stream is not None else None
                check_peer(peer, target, self.settings, source=source, extra_trusted=extra)
                location = resp.headers.get("location")
                if resp.status_code in REDIRECT_STATUSES and location:
                    nxt = urljoin(target, location)
                    if _origin(nxt) != _origin(target):
                        hdrs = {
                            k: v for k, v in hdrs.items() if k.lower() in CROSS_ORIGIN_SAFE_HEADERS
                        }
                    previous, target = target, nxt
                    await resp.aclose()
                    continue
                yield resp, target, hop > 0
                return
            finally:
                await resp.aclose()
        raise UpstreamError(f"{source}: more than {MAX_REDIRECTS} redirects", source=source)

    async def preflight(
        self,
        url: str,
        *,
        source: str,
        timeout_s: float,
        extra_trusted: Iterable[str] = (),
        observe: bool = False,
    ) -> Preflight:
        """Prove byte-range support with GET bytes=0-0. Never downloads the file.

        `observe` records the object identity for cached block reads in this call."""
        host = urlsplit(url).hostname or ""
        try:
            async with asyncio.timeout(max(0.1, timeout_s)):
                async with self.open(
                    url,
                    source=source,
                    headers={"Range": "bytes=0-0"},
                    extra_trusted=extra_trusted,
                ) as (resp, final, redirected):
                    status = resp.status_code
                    if status >= 400:
                        raise status_error(source, status, urlsplit(final).hostname or host)
                    enc = resp.headers.get("content-encoding", "identity").lower()
                    etag = resp.headers.get("etag")
                    last_modified = resp.headers.get("last-modified")
                    ctype = resp.headers.get("content-type")
                    size: int | None = None
                    capable = False
                    if status == 206:
                        cr = parse_content_range(resp.headers.get("content-range"))
                        if cr is None or cr[0] != 0 or cr[1] != 0 or enc != "identity":
                            raise UpstreamError(
                                f"{source}: invalid 206 response to a byte-range request",
                                source=source,
                                details={"host": urlsplit(final).hostname},
                            )
                        size = cr[2]
                        body = b""
                        async for chunk in resp.aiter_raw():
                            body += chunk
                            if len(body) > 64:
                                break
                        capable = len(body) == 1
                        if observe and capable and self.cache is not None:
                            ident = object_identity(resp.headers, size)
                            if self._client.cookies:  # sent or set: per-client, never shared
                                ident = None
                            self.cache.observe(
                                final, ident, lifetime(resp.headers, self.cache.settings.ttl_s)
                            )
                    elif status == 200:
                        # Range ignored: close without reading the body.
                        cl = resp.headers.get("content-length")
                        size = int(cl) if cl and cl.isdigit() else None
                    else:
                        raise UpstreamError(
                            f"{source}: unexpected HTTP {status} to a byte-range request",
                            source=source,
                            details={"http_status": status},
                        )
                    return Preflight(
                        final_url=final,
                        status=status,
                        range_capable=capable,
                        size=size,
                        etag=etag,
                        last_modified=last_modified,
                        content_type=ctype,
                        redirected=redirected,
                        expires_at=signed_url_expiry(final),
                    )
        except TimeoutError:
            raise DeadlineExceededError(
                f"{source}: range preflight to {host} timed out", source=source
            ) from None

    async def read_range(
        self,
        url: str,
        start: int,
        length: int,
        *,
        source: str,
        timeout_s: float,
        extra_trusted: Iterable[str] = (),
    ) -> tuple[bytes, int | None]:
        """Read at most `length` bytes from `start`. Returns (bytes, total size if known).

        A server that answers 200 is closed without reading and reported as not range capable.
        """
        if length <= 0:
            raise InvalidInputError("range length must be positive")
        end = start + length - 1
        try:
            async with asyncio.timeout(max(0.1, timeout_s)):
                async with self.open(
                    url,
                    source=source,
                    headers={"Range": f"bytes={start}-{end}"},
                    extra_trusted=extra_trusted,
                ) as (resp, final, _):
                    if resp.status_code == 416:
                        return b"", None
                    if resp.status_code >= 400:
                        raise status_error(source, resp.status_code, urlsplit(final).hostname or "")
                    if resp.status_code != 206:
                        raise PreparationRequiredError(
                            f"{source}: server ignored the byte-range request",
                            source=source,
                            hint="download the file with fetch_file",
                        )
                    cr = parse_content_range(resp.headers.get("content-range"))
                    if cr is None or cr[0] != start:
                        raise UpstreamError(
                            f"{source}: Content-Range does not match the request", source=source
                        )
                    buf = bytearray()
                    async for chunk in resp.aiter_raw():
                        buf.extend(chunk)
                        if len(buf) >= length:
                            break
                    return bytes(buf[:length]), cr[2]
        except TimeoutError:
            raise DeadlineExceededError(f"{source}: range read timed out", source=source) from None

    async def read_head(
        self,
        url: str,
        length: int,
        *,
        source: str,
        timeout_s: float,
        extra_trusted: Iterable[str] = (),
        since: float | None = None,
    ) -> tuple[int, bytes, int | None, str]:
        """Read at most `length` bytes from the start (206, or a 200 closed after `length`).

        Returns (status, bytes, total size if known, final URL). 4xx/5xx raise. With `since`,
        cached blocks are used if a request at or after it validated the object's identity.
        """
        valid = self.cache.validated(url, since) if self.cache and since is not None else None
        if valid is not None:
            ident = valid[0]
            if ident.size == 0:
                return 416, b"", 0, url
            buf = bytearray()

            async def collect(piece: bytes, _cached: bool) -> None:
                buf.extend(piece)

            try:
                async with asyncio.timeout(max(0.1, timeout_s)):
                    await self.read_blocks(
                        url,
                        0,
                        min(length, ident.size) - 1,
                        valid,
                        collect,
                        source=source,
                        extra_trusted=extra_trusted,
                    )
            except TimeoutError:
                raise DeadlineExceededError(f"{source}: read timed out", source=source) from None
            return 206, bytes(buf), ident.size, url
        try:
            async with asyncio.timeout(max(0.1, timeout_s)):
                async with self.open(
                    url,
                    source=source,
                    headers={"Range": f"bytes=0-{length - 1}"},
                    extra_trusted=extra_trusted,
                ) as (resp, final, _):
                    if resp.status_code == 416:
                        return 416, b"", 0, final
                    if resp.status_code >= 400:
                        raise status_error(source, resp.status_code, urlsplit(final).hostname or "")
                    total: int | None = None
                    if resp.status_code == 206:
                        cr = parse_content_range(resp.headers.get("content-range"))
                        total = cr[2] if cr else None
                    else:
                        cl = resp.headers.get("content-length")
                        total = int(cl) if cl and cl.isdigit() else None
                    buf = bytearray()
                    async for chunk in resp.aiter_raw():
                        buf.extend(chunk)
                        if len(buf) >= length:
                            break
                    return resp.status_code, bytes(buf[:length]), total, final
        except TimeoutError:
            raise DeadlineExceededError(f"{source}: read timed out", source=source) from None

    async def read_blocks(
        self,
        url: str,
        start: int,
        end: int,
        valid: tuple[Identity, float],
        sink: Callable[[bytes, bool], Awaitable[None]],
        *,
        source: str,
        extra_trusted: Iterable[str] = (),
        open_ended: bool = False,
    ) -> None:
        """Deliver bytes [start, end] of a validated public object to `sink(piece, cached)`.

        Cached blocks are served from memory; each run of missing blocks is one upstream
        request with `If-Range: <ETag>`. A response that is not a 206 of the validated identity
        with identity encoding invalidates the object and raises, so versions never mix. A
        valid response whose own policy forbids sharing (no-store, cookies, a signed redirect
        target) is delivered but not stored, and the object is dropped. Only complete blocks
        are stored. Bytes stream through block by block (nothing larger is buffered).
        An `open_ended` request (the reader did not say where it stops) starts with one block
        per upstream request and doubles, to limit read-ahead.
        """
        assert self.cache is not None
        ident = valid[0]
        cache, pos = self.cache, start
        run = 1
        while pos <= end:
            first = pos // BLOCK_BYTES
            block = cache.get(("range", url, ident, first))
            if block is not None:
                piece = block[pos - first * BLOCK_BYTES : end + 1 - first * BLOCK_BYTES]
                await sink(piece, True)
                pos += len(piece)
                continue
            limit = first + (run if open_ended else MAX_RUN_BLOCKS) - 1
            last = first
            while last < min(limit, end // BLOCK_BYTES) and not cache.contains(
                ("range", url, ident, last + 1)
            ):
                last += 1
            cache.note("range_misses", last - first)  # get() counted the first block
            pos = await self._fill(url, first, last, pos, end, valid, sink, source, extra_trusted)
            run = min(run * 2, MAX_RUN_BLOCKS)

    async def _fill(
        self,
        url: str,
        first: int,
        last: int,
        pos: int,
        end: int,
        valid: tuple[Identity, float],
        sink: Callable[[bytes, bool], Awaitable[None]],
        source: str,
        extra_trusted: Iterable[str],
    ) -> int:
        """Fetch blocks first..last, store them, deliver [pos, end] from them. Returns new pos."""
        assert self.cache is not None
        ident, ttl = valid
        lo, hi = first * BLOCK_BYTES, min((last + 1) * BLOCK_BYTES, ident.size) - 1
        headers = {"Range": f"bytes={lo}-{hi}", "If-Range": ident.etag}
        async with self.open(url, source=source, headers=headers, extra_trusted=extra_trusted) as (
            resp,
            final,
            _,
        ):
            if resp.status_code >= 400 and resp.status_code != 416:
                raise status_error(source, resp.status_code, urlsplit(final).hostname or "")
            cr = parse_content_range(resp.headers.get("content-range"))
            same = object_identity(resp.headers, cr[2] if cr else None) == ident
            encoding = resp.headers.get("content-encoding", "identity").lower()
            if (
                resp.status_code != 206
                or cr is None
                or cr[:2] != (lo, hi)
                or not same
                or encoding != "identity"
            ):
                self.cache.invalidate(url)
                raise UpstreamError(
                    f"{source}: the remote file changed during the query; retry it",
                    source=source,
                    retryable=True,
                )
            ttl = min(ttl, lifetime(resp.headers, ttl))
            store = ttl > 0 and plain_url(final) and not self._client.cookies
            if not store:
                self.cache.invalidate(url)
                self.cache.note("range_bypass")
            buf = bytearray()
            block = first
            async for chunk in resp.aiter_raw():
                buf.extend(chunk)
                while block <= last:
                    size = min(BLOCK_BYTES, ident.size - block * BLOCK_BYTES)
                    if len(buf) < size:
                        break
                    data = bytes(buf[:size])
                    del buf[:size]
                    if store:
                        self.cache.put(("range", url, ident, block), data, size, ttl)
                    self.cache.note("range_upstream_bytes", size)
                    base = block * BLOCK_BYTES
                    if pos <= end and pos < base + size:
                        piece = data[pos - base : end + 1 - base]
                        await sink(piece, False)
                        pos += len(piece)
                    block += 1
                if block > last:
                    break
            if block <= last:
                raise UpstreamError(f"{source}: range response ended early", source=source)
        return pos
