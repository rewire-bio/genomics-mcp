"""Opaque offset cursors and page-size limits shared by source clients."""

from __future__ import annotations

import base64
import json

from genomics_mcp.archives._common.errors import InvalidInputError

DEFAULT_PAGE = 50
MAX_PAGE = 1000
MAX_RECORDS = 10_000
"""Default record ceiling (AGENTS.md). Sources without native offsets never scan past it."""


def page_size(limit: int | None, *, default: int = DEFAULT_PAGE, maximum: int = MAX_PAGE) -> int:
    if limit is None:
        return default
    if limit < 1:
        raise InvalidInputError("limit must be >= 1", details={"limit": limit})
    return min(limit, maximum)


def encode_cursor(source: str, **state: object) -> str:
    raw = json.dumps({"s": source, **state}, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, source: str) -> dict:
    if not cursor:
        return {}
    try:
        pad = "=" * (-len(cursor) % 4)
        state = json.loads(base64.urlsafe_b64decode(cursor + pad))
    except (ValueError, TypeError) as exc:
        raise InvalidInputError("malformed cursor", source=source) from exc
    if not isinstance(state, dict) or state.get("s") != source:
        raise InvalidInputError("cursor belongs to a different source or listing", source=source)
    return state


def offset_from(cursor: str | None, source: str, scope: str) -> int:
    state = decode_cursor(cursor, source)
    if state and state.get("scope") != scope:
        raise InvalidInputError("cursor belongs to a different listing", source=source)
    off = state.get("o", 0)
    if not isinstance(off, int) or off < 0:
        raise InvalidInputError("malformed cursor offset", source=source)
    return off


def next_offset_cursor(
    source: str, scope: str, offset: int, returned: int, total: int | None, more: bool | None = None
) -> str | None:
    nxt = offset + returned
    has_more = more if more is not None else (total is not None and nxt < total)
    return encode_cursor(source, scope=scope, o=nxt) if has_more and returned else None
