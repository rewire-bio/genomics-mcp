"""Mock HTTP routing and synthetic BAM builder for archive tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

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
    return httpx.Response(
        status,
        content=json.dumps(data).encode(),
        headers={"content-type": "application/json", **(headers or {})},
    )


def make_bam(path: Path, *, assembly: str = "GRCh38") -> bytes:
    """Synthetic coordinate-sorted BAM on chr1 (10 kb) with reads around [100, 200).

    r_before  90..100 (10M)          ends exactly at 100 -> no overlap
    r_span    95..151 (3M50D3M)      starts before, deletion spans in -> overlaps (CIGAR end)
    r_in      150..160 (10M)         overlaps
    r_end     199..209 (10M)         overlaps last base
    r_after   200..210 (10M)         starts at end -> no overlap
    r_unmapped placed at 120, unmapped -> excluded
    """
    import pysam

    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [
            {"SN": "chr1", "LN": 10000, "AS": assembly},
            {"SN": "chr2", "LN": 5000, "AS": assembly},
        ],
    }
    reads = [
        ("r_before", 90, "10M", 10),
        ("r_span", 95, "3M50D3M", 6),
        ("r_unmapped", 120, None, 10),
        ("r_in", 150, "10M", 10),
        ("r_end", 199, "10M", 10),
        ("r_after", 200, "10M", 10),
    ]
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for name, pos, cigar, qlen in reads:
            a = pysam.AlignedSegment(out.header)
            a.query_name = name
            a.query_sequence = "A" * qlen
            a.query_qualities = pysam.qualitystring_to_array("I" * qlen)
            a.reference_id = 0
            a.reference_start = pos
            if cigar is None:
                a.flag = 4
            else:
                a.flag = 0
                a.cigarstring = cigar
                a.mapping_quality = 60
            out.write(a)
    return path.read_bytes()
