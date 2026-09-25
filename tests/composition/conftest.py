from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from genomics_mcp import composition
from genomics_mcp.config import Settings, load_settings
from genomics_mcp.evidence import ReferenceRuntime
from genomics_mcp.evidence import register as register_evidence
from genomics_mcp.public import PublicHttpClient
from genomics_mcp.registry import Registry
from genomics_mcp.service import GenomicsService

from .doubles import REAL_MODULES, real_providers, register_doubles
from .fixtures import ASSEMBLY, CONTIG, Fixture, build


@dataclass
class Spy:
    """Records every outgoing HTTP request; answers with an empty ClinVar-style 404."""

    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "esearch" in request.url.path:
            return httpx.Response(
                200, json={"esearchresult": {"count": "0", "idlist": [], "retmax": "0"}}
            )
        return httpx.Response(404, json={})

    @property
    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests}


async def _no_sleep(seconds: float) -> None:
    pass


@pytest.fixture
def fx(data_root: Path) -> Fixture:
    return build(data_root / "fx")


@pytest.fixture
def spy() -> Spy:
    return Spy()


@pytest.fixture
async def make_service(settings: Settings, spy: Spy):
    made: list[GenomicsService] = []

    def make(
        cfg: Settings | None = None,
        extra: Callable[[Registry], None] | None = None,
        *,
        readers: bool = True,
    ) -> GenomicsService:
        cfg = cfg or settings
        reg = Registry()
        if readers:
            if real_providers():
                reg.load_providers(REAL_MODULES)
            else:
                register_doubles(reg)
        if extra:
            extra(reg)
        client = httpx.AsyncClient(transport=httpx.MockTransport(spy.handle))
        register_evidence(
            reg, runtime=ReferenceRuntime(client=client, service_kwargs={"sleep": _no_sleep})
        )
        composition.register(reg)
        http = PublicHttpClient(cfg, transport=httpx.MockTransport(spy.handle))
        svc = GenomicsService(cfg, reg, load_providers=False, http=http)
        made.append(svc)
        return svc

    yield make
    for svc in made:
        await svc.aclose()


def configured(tmp_path: Path, data_root: Path, **overrides: Any) -> Settings:
    base = {"paths": {"allowed_roots": [str(data_root)], "work_dir": str(tmp_path / "work")}}
    return load_settings(env={}, overrides={**base, **overrides})


def iv(start: int, end: int, assembly: str = ASSEMBLY) -> dict[str, Any]:
    return {"contig": CONTIG, "start": start, "end": end, "assembly": assembly}


def ref(path: Path, **kw: Any) -> dict[str, Any]:
    return {"uri": str(path), **kw}
