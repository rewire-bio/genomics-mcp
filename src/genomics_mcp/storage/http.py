"""HTTP(S) access for storage: range preflight, bounded range reads and streaming GETs.

- Range support is proven with `GET Range: bytes=0-0` expecting 206 and a Content-Range.
  HEAD/Accept-Ranges are not trusted. A 200 reply is closed without reading the body.
- Redirects are followed manually; every hop is validated (netguard) before it is sent.
  Cross-origin hops carry only safe headers.
- The client ignores proxy/netrc environment (`trust_env=False`) and sends no credentials.
- The final URL is returned for native readers but never logged or returned to callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

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
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
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
    ) -> Preflight:
        """Prove byte-range support with GET bytes=0-0. Never downloads the file."""
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
    ) -> tuple[int, bytes, int | None, str]:
        """Read at most `length` bytes from the start (206, or a 200 closed after `length`).

        Returns (status, bytes, total size if known, final URL). 4xx/5xx raise.
        """
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
