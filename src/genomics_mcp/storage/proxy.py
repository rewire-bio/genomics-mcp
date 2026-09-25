"""Loopback range proxy between native readers and remote storage.

Native readers (HTSlib/libcurl, libBigWig) follow redirects and resolve names on their own,
so they are never given an upstream URL. Instead each remote file of a call is registered
here and the child receives `http://127.0.0.1:<port>/<token>/<name>`:

- routes are random capability tokens bound to one call (`request_id`); there is no
  arbitrary-URL endpoint;
- every upstream request goes through `HttpAccess.open`, so each hop, redirect and connected
  peer is checked by the storage destination policy; redirects are never passed to the child;
- upstream must answer ranges with 206; a 200 is refused (no silent whole-file transfer);
- each route has a byte cap and expires at the call deadline; releasing a call closes its
  connections;
- upstream failures are recorded so the caller sees the typed error (e.g. `unauthorized`)
  rather than a generic native read failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from genomics_mcp.errors import (
    BudgetExceededError,
    DeadlineExceededError,
    GenomicsError,
    UpstreamError,
)
from genomics_mcp.storage.http import HttpAccess, parse_content_range

log = logging.getLogger("genomics_mcp.storage.proxy")

ROUTE_BYTE_CAP = 512 * 1024 * 1024
"""Bytes one route may relay in one call (region reads touch indexes and a few blocks)."""
_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Route:
    token: str
    request_id: str
    upstream: str
    source: str
    trusted: tuple[str, ...]
    expires: float
    remaining: int
    size: int | None
    errors: list[GenomicsError] = field(default_factory=list)
    writers: set[asyncio.StreamWriter] = field(default_factory=set)
    tasks: set[asyncio.Task] = field(default_factory=set)
    requests: int = 0


class RangeProxy:
    def __init__(self, http: HttpAccess) -> None:
        self.http = http
        self._server: asyncio.base_events.Server | None = None
        self._routes: dict[str, Route] = {}
        self._lock = asyncio.Lock()
        self.port: int | None = None

    async def _ensure_started(self) -> None:
        async with self._lock:
            if self._server is None:
                self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
                self.port = self._server.sockets[0].getsockname()[1]

    async def register(
        self,
        request_id: str,
        upstream: str,
        *,
        source: str,
        name: str,
        expires: float,
        size: int | None = None,
        extra_trusted: Iterable[str] = (),
        byte_cap: int = ROUTE_BYTE_CAP,
    ) -> str:
        await self._ensure_started()
        self._purge()
        token = secrets.token_urlsafe(24)
        self._routes[token] = Route(
            token=token,
            request_id=request_id,
            upstream=upstream,
            source=source,
            trusted=tuple(extra_trusted),
            expires=expires,
            remaining=byte_cap,
            size=size,
        )
        safe = _SAFE_NAME.sub("_", name)[-100:] or "file"
        return f"http://127.0.0.1:{self.port}/{token}/{safe}"

    def errors(self, request_id: str) -> list[GenomicsError]:
        return [e for r in self._routes.values() if r.request_id == request_id for e in r.errors]

    def requests(self, request_id: str) -> int:
        return sum(r.requests for r in self._routes.values() if r.request_id == request_id)

    async def release(self, request_id: str) -> None:
        """Drop the call's routes, cancel in-flight relays (closing upstream) and wait for them."""
        tasks: list[asyncio.Task] = []
        for token in [t for t, r in self._routes.items() if r.request_id == request_id]:
            route = self._routes.pop(token)
            tasks.extend(_close_route(route))
        await _await_cancelled(tasks)

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, r in self._routes.items() if r.expires < now]:
            _close_route(self._routes.pop(token))

    def active_relays(self) -> int:
        return sum(len(r.tasks) for r in self._routes.values())

    async def aclose(self) -> None:
        tasks: list[asyncio.Task] = []
        for token in list(self._routes):
            tasks.extend(_close_route(self._routes.pop(token)))
        await _await_cancelled(tasks)
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------------ HTTP
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        route: Route | None = None
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=60)
                except (TimeoutError, ConnectionError):
                    return
                if not line:
                    return
                parts = line.decode("latin-1").split()
                headers: dict[str, str] = {}
                while True:
                    h = await reader.readline()
                    if not h or h in (b"\r\n", b"\n"):
                        break
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                if len(parts) < 2:
                    await _reply(writer, 400)
                    return
                method, target = parts[0].upper(), parts[1]
                token = target.lstrip("/").split("/", 1)[0]
                route = self._routes.get(token)
                if route is None or route.expires < time.monotonic():
                    await _reply(writer, 404)
                    return
                route.writers.add(writer)
                task = asyncio.current_task()
                if task is not None:
                    route.tasks.add(task)
                route.requests += 1
                if method == "HEAD":
                    extra = {"Accept-Ranges": "bytes"}
                    if route.size is not None:
                        extra["Content-Length"] = str(route.size)
                    await _reply(writer, 200, extra, body=False)
                    continue
                if method != "GET":
                    await _reply(writer, 405)
                    return
                if not await self._relay(route, headers.get("range"), writer):
                    return
        except (ConnectionError, asyncio.CancelledError):
            return
        finally:
            if route is not None:
                route.writers.discard(writer)
                route.tasks.discard(asyncio.current_task())  # type: ignore[arg-type]
            writer.close()

    async def _relay(self, route: Route, rng: str | None, writer: asyncio.StreamWriter) -> bool:
        """Relay one range. Returns False when the connection must close."""
        start, end = 0, None
        if rng:
            m = _RANGE.match(rng.strip())
            if not m:
                await _reply(writer, 416)
                return False
            start, end = int(m.group(1)), (int(m.group(2)) if m.group(2) else None)
        up_range = f"bytes={start}-{'' if end is None else end}"
        left = route.expires - time.monotonic()
        if left <= 0:
            route.errors.append(DeadlineExceededError("native read passed the call deadline"))
            await _reply(writer, 504)
            return False
        try:
            async with asyncio.timeout(left):
                async with self.http.open(
                    route.upstream,
                    source=route.source,
                    headers={"Range": up_range},
                    extra_trusted=route.trusted,
                ) as (resp, _final, _):
                    status = resp.status_code
                    if status == 416:
                        await _reply(writer, 416)
                        return True
                    if status >= 400:
                        from genomics_mcp.storage.http import status_error

                        route.errors.append(status_error(route.source, status, ""))
                        await _reply(writer, status if status in (401, 403, 404) else 502)
                        return False
                    cr = parse_content_range(resp.headers.get("content-range"))
                    if status != 206 or cr is None or cr[0] != start:
                        route.errors.append(
                            UpstreamError(
                                f"{route.source}: server stopped honouring byte ranges",
                                source=route.source,
                            )
                        )
                        await _reply(writer, 502)
                        return False
                    length = cr[1] - cr[0] + 1
                    if length > route.remaining and end is not None:
                        route.errors.append(_cap_error(route))
                        await _reply(writer, 502)
                        return False
                    hdrs = {"Content-Length": str(length), "Accept-Ranges": "bytes"}
                    if rng:
                        total = "*" if cr[2] is None else str(cr[2])
                        hdrs["Content-Range"] = f"bytes {cr[0]}-{cr[1]}/{total}"
                    await _reply(writer, 206 if rng else 200, hdrs, body=False)
                    sent = 0
                    async for chunk in resp.aiter_raw():
                        if sent + len(chunk) > length:
                            chunk = chunk[: length - sent]
                        if len(chunk) > route.remaining:
                            route.errors.append(_cap_error(route))
                            return False
                        route.remaining -= len(chunk)
                        writer.write(chunk)
                        await writer.drain()
                        sent += len(chunk)
                        if sent >= length:
                            break
                    return sent == length
        except GenomicsError as exc:
            route.errors.append(exc)
            with contextlib.suppress(ConnectionError):
                await _reply(writer, 403 if exc.info.code == "unauthorized" else 502)
            return False
        except TimeoutError:
            route.errors.append(DeadlineExceededError("native read passed the call deadline"))
            return False


def _close_route(route: Route) -> list[asyncio.Task]:
    for w in list(route.writers):
        w.close()
    me = asyncio.current_task()
    tasks = [t for t in route.tasks if t is not me and not t.done()]
    for t in tasks:
        t.cancel()
    return tasks


async def _await_cancelled(tasks: list[asyncio.Task]) -> None:
    if tasks:
        await asyncio.wait(tasks, timeout=5)


def _cap_error(route: Route) -> BudgetExceededError:
    return BudgetExceededError(
        f"{route.source}: native reader exceeded the per-call byte limit",
        hint="query a smaller interval, or fetch the file with fetch_file",
    )


_REASONS = {
    200: "OK",
    206: "Partial Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    416: "Range Not Satisfiable",
    502: "Bad Gateway",
    504: "Gateway Timeout",
}


async def _reply(
    writer: asyncio.StreamWriter,
    status: int,
    headers: dict[str, str] | None = None,
    *,
    body: bool = True,
) -> None:
    hdrs = dict(headers or {})
    if body:
        hdrs.setdefault("Content-Length", "0")
    lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}"]
    lines += [f"{k}: {v}" for k, v in hdrs.items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
