"""E4 reader provider: BAM/CRAM reads, coverage and pileup; VCF/BCF variants.

Native decoding runs in isolated child processes through the `storage` component. CRAM
references are used only when explicitly supplied, local, and matching the header M5.
"""

from __future__ import annotations

from genomics_mcp.readers.handlers import get_coverage, get_pileup, get_reads, get_variants
from genomics_mcp.registry import Operation, Registry


def register(registry: Registry) -> None:
    for fmt in ("bam", "cram"):
        registry.register(Operation.GET_READS, fmt, get_reads, provider=__name__)
        registry.register(Operation.GET_COVERAGE, fmt, get_coverage, provider=__name__)
        registry.register(Operation.GET_PILEUP, fmt, get_pileup, provider=__name__)
    for fmt in ("vcf", "bcf"):
        registry.register(Operation.GET_VARIANTS, fmt, get_variants, provider=__name__)
