"""Operation catalogue and handler registry.

Independent epics plug in without editing server.py or service.py:

1. Create one of the modules in `PROVIDER_MODULES` (e.g. `genomics_mcp/readers/__init__.py`).
2. Expose `def register(registry: Registry) -> None` in it.
3. Inside, call `registry.register(Operation.GET_READS, "bam", handler, provider=__name__)`,
   `registry.register_resolver("s3", resolver, provider=__name__)` and/or
   `registry.register_source(SourceInfo(...))`.

Handlers are `async def handler(request, ctx: OperationContext) -> OperationOutput`.
A missing provider module is reported as not installed; any other import error propagates.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from genomics_mcp.context import OperationContext
    from genomics_mcp.contracts import FileResolver
    from genomics_mcp.result import OperationOutput

log = logging.getLogger("genomics_mcp.registry")


class Category(StrEnum):
    DISCOVERY = "discovery"
    TRANSFERS = "transfers"
    GENOMICS = "genomics"
    COMPOSITION = "composition"
    REFERENCE = "reference"


class Operation(StrEnum):
    LIST_SOURCES = "list_sources"
    SEARCH_DATASETS = "search_datasets"
    DESCRIBE_DATASET = "describe_dataset"
    LIST_FILES = "list_files"
    LIST_SAMPLES = "list_samples"
    GET_SAMPLE_METADATA = "get_sample_metadata"
    FETCH_FILE = "fetch_file"
    GET_TRANSFER_STATUS = "get_transfer_status"
    CANCEL_TRANSFER = "cancel_transfer"
    GET_READS = "get_reads"
    GET_COVERAGE = "get_coverage"
    GET_PILEUP = "get_pileup"
    GET_VARIANTS = "get_variants"
    GET_SEQUENCE = "get_sequence"
    GET_FEATURES = "get_features"
    GET_SIGNAL = "get_signal"
    INSPECT_LOCUS = "inspect_locus"
    COMPARE_SAMPLES = "compare_samples"
    RESOLVE_IDENTIFIER = "resolve_identifier"
    NORMALIZE_VARIANT = "normalize_variant"
    LOOKUP_VARIANT = "lookup_variant"
    LOOKUP_GENE = "lookup_gene"
    LOOKUP_PROTEIN = "lookup_protein"


Dispatch = Literal["builtin", "source", "format", "default", "fanout"]
DEFAULT_KEY = "default"


@dataclass(frozen=True)
class OperationSpec:
    operation: Operation
    category: Category
    dispatch: Dispatch
    """builtin: core; source: request.source; format: file format; default: single handler;
    fanout: a 'default' planner if registered, else every requested source handler concurrently."""
    planned_epics: tuple[str, ...]
    formats: tuple[str, ...] = ()
    """For format dispatch: formats this operation accepts at all."""


_O, _C = Operation, Category
OPERATIONS: dict[Operation, OperationSpec] = {
    s.operation: s
    for s in [
        OperationSpec(_O.LIST_SOURCES, _C.DISCOVERY, "builtin", ("E1",)),
        OperationSpec(_O.SEARCH_DATASETS, _C.DISCOVERY, "source", ("E6", "E7")),
        OperationSpec(_O.DESCRIBE_DATASET, _C.DISCOVERY, "source", ("E6", "E7")),
        OperationSpec(_O.LIST_FILES, _C.DISCOVERY, "source", ("E2", "E6", "E7")),
        OperationSpec(_O.LIST_SAMPLES, _C.DISCOVERY, "source", ("E6", "E7")),
        OperationSpec(_O.GET_SAMPLE_METADATA, _C.DISCOVERY, "source", ("E6", "E7")),
        OperationSpec(_O.FETCH_FILE, _C.TRANSFERS, "default", ("E3",)),
        OperationSpec(_O.GET_TRANSFER_STATUS, _C.TRANSFERS, "default", ("E3",)),
        OperationSpec(_O.CANCEL_TRANSFER, _C.TRANSFERS, "default", ("E3",)),
        OperationSpec(_O.GET_READS, _C.GENOMICS, "format", ("E4",), ("bam", "cram", "sam")),
        OperationSpec(_O.GET_COVERAGE, _C.GENOMICS, "format", ("E4",), ("bam", "cram")),
        OperationSpec(_O.GET_PILEUP, _C.GENOMICS, "format", ("E4",), ("bam", "cram")),
        OperationSpec(_O.GET_VARIANTS, _C.GENOMICS, "format", ("E4",), ("vcf", "bcf")),
        OperationSpec(_O.GET_SEQUENCE, _C.GENOMICS, "format", ("E3",), ("fasta",)),
        OperationSpec(
            _O.GET_FEATURES, _C.GENOMICS, "format", ("E3", "E5"), ("bed", "gff3", "gtf", "bigbed")
        ),
        OperationSpec(_O.GET_SIGNAL, _C.GENOMICS, "format", ("E5",), ("bigwig",)),
        OperationSpec(_O.INSPECT_LOCUS, _C.COMPOSITION, "default", ("E9",)),
        OperationSpec(_O.COMPARE_SAMPLES, _C.COMPOSITION, "default", ("E9",)),
        OperationSpec(_O.RESOLVE_IDENTIFIER, _C.REFERENCE, "fanout", ("E8",)),
        OperationSpec(_O.NORMALIZE_VARIANT, _C.REFERENCE, "fanout", ("E8",)),
        OperationSpec(_O.LOOKUP_VARIANT, _C.REFERENCE, "fanout", ("E8",)),
        OperationSpec(_O.LOOKUP_GENE, _C.REFERENCE, "fanout", ("E8",)),
        OperationSpec(_O.LOOKUP_PROTEIN, _C.REFERENCE, "fanout", ("E8",)),
    ]
}

# Provider modules, one per epic. Workers create these; nothing else needs editing.
PROVIDER_MODULES: dict[str, str] = {
    "genomics_mcp.storage": "E2",
    "genomics_mcp.artifacts": "E3",
    "genomics_mcp.readers": "E4",
    "genomics_mcp.signal": "E5",
    "genomics_mcp.archives": "E6",
    "genomics_mcp.catalogs": "E7",
    "genomics_mcp.evidence": "E8",
    "genomics_mcp.composition": "E9",
}

Handler = Callable[[Any, "OperationContext"], Awaitable["OperationOutput"]]
Shutdown = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class HandlerSpec:
    operation: Operation
    key: str
    handler: Handler
    provider: str
    description: str = ""


@dataclass
class SourceInfo:
    """Describes a data or reference source for list_sources and the status resource."""

    name: str
    title: str
    kind: Literal["archive", "catalog", "reference", "storage", "local"]
    planned_epic: str
    homepage: str | None = None
    terms_url: str | None = None
    auth: Literal["none", "optional_key", "required_key", "account", "explicit_profile"] = "none"
    notes: str = ""
    operations: list[str] = field(default_factory=list)


class Registry:
    def __init__(self) -> None:
        self._handlers: dict[Operation, dict[str, HandlerSpec]] = {op: {} for op in Operation}
        self._resolvers: dict[str, tuple[FileResolver, str]] = {}
        self._sources: dict[str, SourceInfo] = {}
        self._components: dict[str, Any] = {}
        self._shutdown: list[Shutdown] = []
        self.provider_status: dict[str, str] = {}

    # -- registration -------------------------------------------------------------
    def register(
        self,
        operation: Operation,
        key: str,
        handler: Handler,
        *,
        provider: str,
        description: str = "",
    ) -> None:
        spec = OPERATIONS[operation]
        if spec.dispatch == "builtin":
            raise ValueError(f"{operation} is built in and cannot be overridden")
        key = key.lower()
        if spec.dispatch == "format" and key not in spec.formats:
            raise ValueError(f"{operation} does not accept format {key!r}")
        if spec.dispatch == "default" and key != DEFAULT_KEY:
            raise ValueError(f"{operation} takes a single handler under key {DEFAULT_KEY!r}")
        if key in self._handlers[operation]:
            other = self._handlers[operation][key].provider
            raise ValueError(f"{operation}/{key} already registered by {other}")
        self._handlers[operation][key] = HandlerSpec(operation, key, handler, provider, description)

    def register_resolver(self, scheme: str, resolver: FileResolver, *, provider: str) -> None:
        scheme = scheme.lower()
        if scheme in self._resolvers:
            raise ValueError(
                f"resolver for {scheme!r} already registered by {self._resolvers[scheme][1]}"
            )
        self._resolvers[scheme] = (resolver, provider)

    def register_source(self, info: SourceInfo) -> None:
        self._sources[info.name] = info

    def provide(self, name: str, component: Any) -> None:
        if name in self._components:
            raise ValueError(f"component {name!r} already provided")
        self._components[name] = component

    def on_shutdown(self, callback: Shutdown) -> None:
        self._shutdown.append(callback)

    # -- lookup -------------------------------------------------------------------
    def handler(self, operation: Operation, key: str) -> HandlerSpec | None:
        return self._handlers[operation].get(key.lower())

    def keys(self, operation: Operation) -> list[str]:
        return sorted(self._handlers[operation])

    def resolver(self, scheme: str) -> FileResolver | None:
        entry = self._resolvers.get(scheme.lower())
        return entry[0] if entry else None

    def resolver_schemes(self) -> list[str]:
        return sorted(self._resolvers)

    def component(self, name: str) -> Any | None:
        return self._components.get(name)

    def sources(self) -> list[SourceInfo]:
        return [self._sources[k] for k in sorted(self._sources)]

    def source(self, name: str) -> SourceInfo | None:
        return self._sources.get(name)

    def is_available(self, operation: Operation) -> bool:
        return OPERATIONS[operation].dispatch == "builtin" or bool(self._handlers[operation])

    async def shutdown(self) -> None:
        for cb in reversed(self._shutdown):
            await cb()

    # -- providers ----------------------------------------------------------------
    def load_providers(self, modules: dict[str, str] | None = None) -> dict[str, str]:
        """Import provider modules and call their `register`. Returns module -> status."""
        for module, epic in (PROVIDER_MODULES if modules is None else modules).items():
            try:
                mod = importlib.import_module(module)
            except ModuleNotFoundError as exc:
                if exc.name != module:
                    raise
                self.provider_status[module] = f"not installed ({epic})"
                continue
            register = getattr(mod, "register", None)
            if not callable(register):
                raise TypeError(f"{module} must define register(registry)")
            register(self)
            self.provider_status[module] = "loaded"
            log.debug("loaded provider %s", module)
        return dict(self.provider_status)

    def capabilities(self) -> dict[str, Any]:
        ops = {}
        for op, spec in OPERATIONS.items():
            handlers = self._handlers[op]
            ops[op.value] = {
                "category": spec.category.value,
                "available": self.is_available(op),
                "dispatch": spec.dispatch,
                "keys": sorted(handlers),
                "accepted_formats": list(spec.formats),
                "planned_epics": list(spec.planned_epics),
            }
        return {
            "operations": ops,
            "file_schemes": self.resolver_schemes(),
            "providers": dict(self.provider_status),
        }
