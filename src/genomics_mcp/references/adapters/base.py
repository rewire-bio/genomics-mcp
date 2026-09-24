"""Shared adapter helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..http import SourceFailure, SourceHttp, failure
from ..models import SourceError

T = TypeVar("T")


class TtlValue:
    """Caches one successful async lookup (e.g. a source release) for ``ttl`` seconds."""

    def __init__(self, ttl: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._value: Any = None
        self._at: float | None = None
        self._lock = asyncio.Lock()

    async def get(self, loader: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            now = self._clock()
            if self._at is not None and now - self._at < self.ttl:
                return self._value
            value = await loader()
            self._value, self._at = value, now
            return value


async def optional(awaitable: Awaitable[T]) -> tuple[T | None, SourceError | None]:
    """Run a supporting request whose failure must not fail the main result."""
    try:
        return await awaitable, None
    except SourceFailure as exc:
        return None, exc.error


def require_dict(http: SourceHttp, value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise failure(http.source, operation, "invalid_response", f"expected a JSON object, got {type(value).__name__}")
    return value


def require_list(http: SourceHttp, value: Any, operation: str) -> list[Any]:
    if not isinstance(value, list):
        raise failure(http.source, operation, "invalid_response", f"expected a JSON array, got {type(value).__name__}")
    return value
