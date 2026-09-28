"""Bounded in-memory cache for public upstream bytes (one LRU per server process).

Two kinds of entry share the store, which is bounded by bytes, entries and age:

- ``range``: 64 KiB aligned blocks of public HTTP(S) files, keyed by URL and object identity
  (size + strong ETag). Identity comes from a request made during the current tool call
  (``object`` entries), so every new reader call revalidates the file before any block is
  reused, and blocks of different object versions never mix. See `storage.http`.
- ``api``: successful public archive/catalogue and reference API responses, keyed by a digest
  of the request as it would actually be sent (method, URL with parameters, every header
  including the client's own, body). See `request_key`.

`max_bytes` bounds values plus their keys and metadata. Nothing is written to disk.
`[cache] enabled = false` (or GENOMICS_MCP_CACHE_ENABLED=0) turns it off; restarting the server
clears it. Metrics are aggregate counts only.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Hashable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from genomics_mcp.config import CacheSettings

BLOCK_BYTES = 64 * 1024
_ENTRY_OVERHEAD = 256
# Request headers that may vary a response; all are part of the key. Any other header (auth,
# cookies, API keys, Range, ...) makes the request uncacheable.
_KEY_HEADERS = frozenset(
    {"accept", "accept-encoding", "accept-language", "content-type", "user-agent"}
)
_TRANSPORT_HEADERS = frozenset({"host", "connection", "content-length"})
_SECRET_PARAMS = frozenset(
    {"api_key", "apikey", "api-key", "key", "token", "access_token", "auth", "signature",
     "sig", "secret", "password", "credential", "expires"}
)  # fmt: skip
_SECRET_PARAM_PREFIXES = ("x-amz-", "x-goog-")


@dataclass(frozen=True)
class Identity:
    """One version of a remote object. Blocks are only reused under an equal identity."""

    size: int
    etag: str
    last_modified: str | None = None


def object_identity(headers: Mapping[str, str], size: int | None) -> Identity | None:
    """Strong ETag plus size, or None (weak/missing validators fail closed)."""
    etag = headers.get("etag")
    if size is None or not etag or etag.startswith("W/"):
        return None
    return Identity(size, etag, headers.get("last-modified"))


def plain_url(url: str) -> bool:
    """No query string and no userinfo: never a signed or credentialed URL."""
    p = urlsplit(url)
    return not (p.query or p.username or p.password)


def _secret_params(url: str) -> bool:
    p = urlsplit(url)
    if p.username or p.password:
        return True
    for name, _ in parse_qsl(p.query, keep_blank_values=True):
        n = name.lower()
        if n in _SECRET_PARAMS or n.startswith(_SECRET_PARAM_PREFIXES):
            return True
    return False


def _http_date(value: str | None) -> datetime | None:
    """An HTTP date with a time zone, or None (malformed or zone-less values are refused)."""
    try:
        parsed = email.utils.parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _seconds(value: str) -> float | None:
    """HTTP delta-seconds: ASCII digits only (no signs, fractions, NaN, infinity or other
    Unicode digits); values past 2**31 are clamped to it (RFC 9111 section 1.2.2)."""
    value = value.strip()
    if not (value.isascii() and value.isdigit()):
        return None
    return float(2**31 if len(value) > 10 else min(int(value), 2**31))


def lifetime(headers: Mapping[str, str], ttl_s: float, *, now: datetime | None = None) -> float:
    """Remaining seconds a response may be reused, at most `ttl_s`.

    0 for no-store/no-cache/private, Pragma no-cache, Set-Cookie and Vary *. Freshness comes
    from s-maxage/max-age, else Expires - Date, else `ttl_s`. The response's current age
    (the larger of `Age` and now - `Date`) is subtracted. Unparseable freshness information
    fails closed (0)."""
    if "set-cookie" in headers or headers.get("vary", "").strip() == "*":
        return 0.0
    if "no-cache" in headers.get("pragma", "").lower():
        return 0.0
    directives: dict[str, str] = {}
    for part in headers.get("cache-control", "").lower().split(","):
        name, _, value = part.strip().partition("=")
        directives[name] = value.strip('"')
    if directives.keys() & {"no-store", "no-cache", "private"}:
        return 0.0
    now = now or datetime.now(UTC)
    age = _seconds(headers.get("age", "0"))
    date = _http_date(headers.get("date"))
    if age is None or ("date" in headers and date is None):
        return 0.0
    if date is not None:
        age = max(age, (now - date).total_seconds())
    fresh: float | None = None
    for name in ("s-maxage", "max-age"):
        if name in directives:
            fresh = _seconds(directives[name])
            if fresh is None:
                return 0.0
            break
    if fresh is None and "expires" in headers:
        expires = _http_date(headers.get("expires"))
        if expires is None:
            return 0.0
        fresh = (expires - (date or now)).total_seconds()
    if fresh is None:
        fresh = ttl_s
    return max(0.0, min(ttl_s, fresh - age))


class MemoryCache:
    """LRU store bounded by `max_bytes`, `max_entries` and per-entry expiry.

    Values must be immutable (bytes, tuples, frozen dataclasses): hits return the stored object.
    Used from the event loop only, so it needs no locking.
    """

    def __init__(self, settings: CacheSettings, *, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.enabled = settings.enabled
        self._clock = clock
        self._items: OrderedDict[Hashable, tuple[Any, int, float]] = OrderedDict()
        self.bytes = 0
        self.counts: Counter[str] = Counter()

    def get(self, key: tuple) -> Any | None:
        if not self.enabled:
            return None
        item = self._items.get(key)
        if item is not None and item[2] <= self._clock():
            self._drop(key, "expired")
            item = None
        if item is None:
            self.counts[f"{key[0]}_misses"] += 1
            return None
        self._items.move_to_end(key)
        self.counts[f"{key[0]}_hits"] += 1
        return item[0]

    def contains(self, key: tuple) -> bool:
        item = self._items.get(key)
        return item is not None and item[2] > self._clock()

    def put(self, key: tuple, value: Any, size: int, ttl_s: float) -> bool:
        """Store `value` (`size` bytes of payload); the key is accounted for as well."""
        size += _ENTRY_OVERHEAD + len(repr(key))
        if not self.enabled or ttl_s <= 0 or size > self.settings.max_bytes:
            return False
        if key in self._items:
            self._drop(key, None)
        self._items[key] = (value, size, self._clock() + ttl_s)
        self.bytes += size
        self.counts[f"{key[0]}_stores"] += 1
        while self.bytes > self.settings.max_bytes or len(self._items) > self.settings.max_entries:
            self._drop(next(iter(self._items)), "evictions")
        return True

    def discard(self, match: Callable[[tuple], bool], reason: str = "invalidations") -> int:
        keys = [k for k in self._items if match(k)]
        for k in keys:
            self._drop(k, reason)
        return len(keys)

    def clear(self) -> None:
        self._items.clear()
        self.bytes = 0

    def note(self, name: str, n: int = 1) -> None:
        self.counts[name] += n

    def _drop(self, key: Hashable, reason: str | None) -> None:
        _value, size, _ = self._items.pop(key)
        self.bytes -= size
        if reason:
            self.counts[f"{key[0]}_{reason}"] += 1  # type: ignore[index]

    def metrics(self) -> dict[str, Any]:
        """Aggregate counts only: no URLs, identifiers, query values or bodies."""
        return {
            "enabled": self.enabled,
            "entries": len(self._items),
            "bytes": self.bytes,
            "max_bytes": self.settings.max_bytes,
            "max_entries": self.settings.max_entries,
            "ttl_s": self.settings.ttl_s,
            "counts": dict(sorted(self.counts.items())),
        }

    # -------------------------------------------------------------- object identity
    def observe(self, url: str, ident: Identity | None, ttl_s: float) -> None:
        """Record the identity a request made just now returned for `url`.

        A missing validator or no-store policy removes any earlier identity (bypass); a
        changed identity drops the old version's blocks. URLs with a query string or userinfo
        (signed or credentialed) are never recorded."""
        if not self.enabled or not plain_url(url):
            return
        old = self._items.get(("object", url))
        if ident is None or ttl_s <= 0:
            self.note("range_bypass")
        if old is not None and (ident is None or old[0][0] != ident or ttl_s <= 0):
            self._drop(("object", url), "invalidations")
            self.discard(lambda k: k[0] == "range" and k[1] == url)
        if ident is not None and ttl_s > 0:
            meta = len(ident.etag) + len(ident.last_modified or "")
            self.put(("object", url), (ident, self._clock(), ttl_s), meta, ttl_s)

    def validated(self, url: str, since: float) -> tuple[Identity, float] | None:
        """(identity, block ttl) if a request at or after `since` observed one for `url`."""
        item = self._items.get(("object", url)) if self.enabled else None
        if item is None or item[2] <= self._clock():
            return None
        ident, checked_at, ttl_s = item[0]
        return (ident, ttl_s) if checked_at >= since else None

    def invalidate(self, url: str) -> None:
        self.discard(lambda k: k[0] in ("range", "object") and k[1] == url)


# ------------------------------------------------------------------ API requests


def request_key(
    source: str, request: httpx.Request, client: httpx.AsyncClient, *, read_only: bool = False
) -> tuple | None:
    """Key for a public, credential-free, read-only request, from the request as it would be
    sent (client headers and cookies merged in); None means bypass.

    GET only, or POST when the caller declares a read-only GraphQL query. Requests with any
    header outside the representation headers (so any auth, cookie, API-key or Range header),
    client-level auth, key/token/signature parameters or URL userinfo, and private-file-derived
    queries are never cached. The key is a fixed-size digest."""
    if _PRIVATE_QUERY.get() or client.auth is not None:
        return None
    method = request.method.upper()
    if method == "POST":
        try:
            query = json.loads(request.content).get("query")
        except (ValueError, AttributeError):
            return None
        if not read_only or not isinstance(query, str) or query.lstrip().startswith("mutation"):
            return None
    elif method != "GET":
        return None
    names = {k.lower() for k in request.headers}
    if not names <= _KEY_HEADERS | _TRANSPORT_HEADERS or _secret_params(str(request.url)):
        return None
    headers = sorted(
        (k.lower(), v) for k, v in request.headers.items() if k.lower() in _KEY_HEADERS
    )
    material = repr((method, str(request.url), headers, request.content))
    return ("api", source, hashlib.sha256(material.encode()).hexdigest())


def storable(request: httpx.Request, status: int, headers: Mapping[str, str], body: bytes,
             final_url: str, client: httpx.AsyncClient, *, graphql: bool) -> bool:  # fmt: skip
    """Only a 200 that the endpoint's own format check would accept: JSON must parse when JSON
    was asked for or declared, GraphQL `errors` and HTML pages are never stored, and a
    redirect to a signed or credentialed URL is not shared. A client holding cookies after the
    exchange (set by a redirect hop or a retried response) may have sent them: not stored."""
    ctype = headers.get("content-type", "").lower()
    if status != 200 or "html" in ctype or _secret_params(final_url) or client.cookies:
        return False
    if graphql or "json" in ctype or "json" in request.headers.get("accept", "").lower():
        try:
            data = json.loads(body)
        except ValueError:
            return False
        if graphql and (not isinstance(data, dict) or data.get("errors")):
            return False
    return True


@dataclass(frozen=True)
class CachedResponse:
    status: int
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    retrieved_at: datetime

    @property
    def nbytes(self) -> int:
        return len(self.body) + len(self.url) + sum(len(k) + len(v) for k, v in self.headers)


# ------------------------------------------------------------------ per-call state

_PRIVATE_QUERY: ContextVar[bool] = ContextVar("genomics_mcp_private_query", default=False)


@contextmanager
def private_query(active: bool = True) -> Iterator[None]:
    """Requests made inside are derived from private files: never read or written."""
    token = _PRIVATE_QUERY.set(_PRIVATE_QUERY.get() or active)
    try:
        yield
    finally:
        _PRIVATE_QUERY.reset(token)


@dataclass
class CallUse:
    """Cache use of one tool call (shared by its fan-out tasks)."""

    api_hits: int = 0
    range_bytes: int = 0
    oldest: dict[str, datetime] = field(default_factory=dict)

    def summary(self) -> str | None:
        parts = []
        if self.api_hits:
            parts.append(
                f"{self.api_hits} upstream API response(s) reused; provenance reports the "
                "earliest upstream retrieval time per source"
            )
        if self.range_bytes:
            parts.append(
                f"{self.range_bytes} file bytes reused from blocks of the same file version "
                "(ETag revalidated in this call)"
            )
        return "cache: " + "; ".join(parts) if parts else None


_CALL: ContextVar[CallUse | None] = ContextVar("genomics_mcp_cache_call", default=None)


@contextmanager
def track_call() -> Iterator[CallUse]:
    use = CallUse()
    token = _CALL.set(use)
    try:
        yield use
    finally:
        _CALL.reset(token)


def note_response(source: str, retrieved_at: datetime, *, hit: bool) -> None:
    """Record when upstream produced a response used in this call (hit: its original time)."""
    use = _CALL.get()
    if use is not None:
        use.api_hits += hit
        if source not in use.oldest or retrieved_at < use.oldest[source]:
            use.oldest[source] = retrieved_at


def note_range_bytes(n: int) -> None:
    use = _CALL.get()
    if use is not None:
        use.range_bytes += n


def original_retrieval(source: str) -> datetime | None:
    """Earliest upstream retrieval time of the responses of `source` used so far in this call.

    Source-level, not per response: provenance/evidence models built during the call default
    `retrieved_at` to it, so a reused response never looks fresh and cold/warm calls report
    the same time for the same response."""
    use = _CALL.get()
    return use.oldest.get(source) if use is not None else None
