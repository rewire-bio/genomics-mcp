"""One deadline per source operation (token, tickets, pages, blocks and parsing together)."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from genomics_mcp.archives._common.errors import DeadlineExceededError

T = TypeVar("T")
DEFAULT_OPERATION_TIMEOUT_S = 30.0
DEFAULT_TRANSFER_TIMEOUT_S = 600.0


Method = Callable[..., Awaitable[T]]


def operation(attr: str = "operation_timeout_s") -> Callable[[Method], Method]:
    """Bound a whole client method by `self.<attr>` seconds (overridable with `timeout_s=`).

    Threads started with asyncio.to_thread (pysam post-filtering) cannot be interrupted; they
    only ever parse data already bounded by the byte budget, and their result is discarded."""

    def wrap(fn: Method) -> Method:
        @functools.wraps(fn)
        async def inner(self: Any, *args: Any, timeout_s: float | None = None, **kwargs: Any) -> T:
            limit = timeout_s if timeout_s is not None else getattr(self, attr)
            if limit <= 0:
                raise DeadlineExceededError("timeout_s must be positive", retryable=False)
            try:
                async with asyncio.timeout(limit):
                    return await fn(self, *args, **kwargs)
            except TimeoutError as exc:
                raise DeadlineExceededError(
                    f"{getattr(self, 'source', 'source')}: {fn.__name__} exceeded {limit:g}s",
                    source=getattr(self, "source", None),
                ) from exc

        return inner

    return wrap
