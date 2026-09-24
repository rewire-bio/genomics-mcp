"""Mock HTTP routing for catalog tests."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

Handler = Callable[[httpx.Request], httpx.Response]


class Router:
    """Routes by (method, exact URL without query) and records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, url: str, handler: Handler | httpx.Response | dict | list) -> None:
        if isinstance(handler, httpx.Response):
            resp = handler
            handler = lambda r, _resp=resp: _resp  # noqa: E731
        elif isinstance(handler, dict | list):
            body = handler
            handler = lambda r, _b=body: httpx.Response(200, json=_b)  # noqa: E731
        self.routes[(method.upper(), url)] = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, str(request.url.copy_with(query=None)))
        if key not in self.routes:
            return httpx.Response(599, text=f"unrouted {key}")
        return self.routes[key](request)

    def hits(self, url_prefix: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).startswith(url_prefix)]

    def client(self, **kw) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), trust_env=False, **kw)


def json_response(data, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode(), headers={
        "content-type": "application/json", **(headers or {})})


