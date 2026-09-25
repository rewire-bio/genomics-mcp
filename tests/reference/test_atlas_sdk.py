"""The optional Atlas transport against the real `alphagenome` SDK protos (atlas extra).

No request is sent: gRPC channels connect lazily and no RPC is invoked here. A live
Atlas call needs an explicitly configured key and is not part of this suite.
"""

from __future__ import annotations

import sys

import pytest
from pydantic import SecretStr

from genomics_mcp.references.adapters.atlas import GrpcAtlasTransport, build_filter
from genomics_mcp.references.http import SourceFailure

pytestmark = pytest.mark.asyncio


async def test_sdk_request_is_built_from_official_protos() -> None:
    pytest.importorskip("alphagenome", reason="install the 'atlas' extra")
    transport = GrpcAtlasTransport(SecretStr("dummy-key-not-used-for-any-request"))
    try:
        req = transport.build_request(
            chromosome="chr7",
            position=140753336,
            reference_bases="A",
            alternate_bases="T",
            filter=build_filter(["SCORER_A"]),
        )
        assert type(req).__name__ == "GetDenseVariantScoresRequest"
        assert (req.variant.chromosome, req.variant.position) == ("chr7", 140753336)
        assert (req.variant.reference_bases, req.variant.alternate_bases) == ("A", "T")
        assert req.organism == 9606
        assert req.filter == '(scores.variant_scorer.name = "SCORER_A")'
        # Only the named precomputed RPCs exist on the transport.
        assert not hasattr(transport, "predict_variant")
    finally:
        await transport.aclose()


async def test_missing_sdk_is_an_explicit_unsupported_error(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "alphagenome.protos", None)
    with pytest.raises(SourceFailure) as exc:
        GrpcAtlasTransport(SecretStr("dummy-key-not-used-for-any-request"))
    assert exc.value.error.kind == "unsupported"
    assert "alphagenome" in exc.value.error.message
    assert "dummy-key" not in exc.value.error.model_dump_json()
