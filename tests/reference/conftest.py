"""Shared helpers for reference tests: fixture loading, a URL router for
httpx.MockTransport and a fake clock so rate limits never sleep for real."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from genomics_mcp.references import ReferenceConfig, ReferenceService

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: calls public reference services (set GENOMICS_MCP_LIVE=1)")


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


Handler = Callable[[httpx.Request], httpx.Response]


class Router:
    """Routes by (method, regex on URL without query). Unmatched requests fail loudly."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler | list[Handler]]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, pattern: str, handler: Handler | httpx.Response | list[Any]) -> Router:
        if isinstance(handler, httpx.Response):
            response = handler
            handler = lambda request, r=response: r  # noqa: E731
        elif isinstance(handler, list):
            handler = [h if callable(h) else (lambda request, r=h: r) for h in handler]
        self.routes.append((method, re.compile(pattern), handler))
        return self

    def json(self, method: str, pattern: str, body: Any, status: int = 200, headers: dict[str, str] | None = None) -> Router:
        return self.add(method, pattern, httpx.Response(status, json=body, headers=headers))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        for method, pattern, handler in self.routes:
            if method == request.method and pattern.search(url):
                if isinstance(handler, list):
                    h = handler.pop(0) if len(handler) > 1 else handler[0]
                    return h(request)
                return handler(request)
        raise AssertionError(f"unrouted request {request.method} {request.url}")

    def called(self, pattern: str) -> list[httpx.Request]:
        rx = re.compile(pattern)
        return [r for r in self.calls if rx.search(str(r.url))]


def html_500() -> httpx.Response:
    return httpx.Response(500, text="<!doctype html><html><title>Error: 500</title></html>",
                          headers={"content-type": "text/html"})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def make_service(router: Router, clock: FakeClock):
    def build(config: ReferenceConfig | None = None, **kwargs: Any) -> ReferenceService:
        client = httpx.AsyncClient(transport=httpx.MockTransport(router.handle))
        return ReferenceService(client, config or ReferenceConfig(), clock=clock, sleep=clock.sleep, **kwargs)

    return build


def ncbi_fasta(accession: str, seq: str) -> httpx.Response:
    return httpx.Response(200, text=f">{accession}:1-{len(seq)} test\n{seq}\n", headers={"content-type": "text/plain"})


def ensembl_sequence_handler(assembly: str, contig_seq: dict[str, tuple[int, str]]) -> Handler:
    """Serve /sequence/region/human/{contig}:{s}..{e}:1 from synthetic sequence.

    ``contig_seq`` maps contig -> (0-based offset of the string, sequence).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/sequence/region/human/([^:]+):(\d+)\.\.(\d+):1", request.url.path)
        assert m, request.url
        contig, s1, e1 = m.group(1), int(m.group(2)), int(m.group(3))
        offset, seq = contig_seq[contig]
        start0, end0 = s1 - 1, e1
        # Bases outside the synthetic string are served as N so coordinates stay exact.
        bases = "".join(
            seq[i - offset] if offset <= i < offset + len(seq) else "N" for i in range(start0, end0)
        )
        return httpx.Response(200, json={
            "seq": bases,
            "id": f"chromosome:{assembly}:{contig}:{start0 + 1}:{end0}:1",
            "molecule": "dna",
        })

    return handler
