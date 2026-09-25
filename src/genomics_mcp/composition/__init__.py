"""E9 provider: inspect_locus and compare_samples.

Both run the registered genomics handlers (E3-E5) for each file concurrently under the
call's deadline, and inspect_locus consults reference evidence only through the E8
`reference_evidence` facade with per-call consent. See docs/composition.md.
"""

from __future__ import annotations

from genomics_mcp.composition.compare import compare_samples
from genomics_mcp.composition.inspect import inspect_locus
from genomics_mcp.registry import DEFAULT_KEY, Operation, Registry

__all__ = ["compare_samples", "inspect_locus", "register"]


def register(registry: Registry) -> None:
    registry.register(
        Operation.INSPECT_LOCUS,
        DEFAULT_KEY,
        inspect_locus,
        provider=__name__,
        description="E9 composition over registered readers and reference evidence",
    )
    registry.register(
        Operation.COMPARE_SAMPLES,
        DEFAULT_KEY,
        compare_samples,
        provider=__name__,
        description="E9 literal per-file/per-sample comparison",
    )
