"""Versioned public reference evidence and variant normalization (epic E8).

Entry points for core wiring:

* ``ReferenceService(httpx.AsyncClient, ReferenceConfig(...))`` - the five tools.
* ``tools.TOOLS`` / ``tools.call_tool(service, name, arguments)`` - registry and dispatch.
* ``tools.source_status(service)`` - non-secret source capability view.
"""

from .schemas import (
    LookupGeneRequest,
    LookupProteinRequest,
    LookupVariantRequest,
    NormalizeVariantRequest,
    ResolveIdentifierRequest,
)
from .service import ReferenceConfig, ReferenceProvider, ReferenceService
from .tools import TOOLS, call_tool, input_schemas, output_schemas, source_status

__all__ = [
    "TOOLS",
    "LookupGeneRequest",
    "LookupProteinRequest",
    "LookupVariantRequest",
    "NormalizeVariantRequest",
    "ReferenceConfig",
    "ReferenceProvider",
    "ReferenceService",
    "ResolveIdentifierRequest",
    "call_tool",
    "input_schemas",
    "output_schemas",
    "source_status",
]
