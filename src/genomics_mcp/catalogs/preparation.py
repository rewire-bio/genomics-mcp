"""Explicit NCBI Datasets genome preparation as a core component (`preparer:ncbi_genome_fasta`).

A genome package is a ZIP, never a ready FASTA. `NcbiGenomePreparer.prepare(accession, ctx)`
is the explicit step (for E3's reference preparation): it enforces the caller budget (default
`limits.max_transfer_bytes`, at most `limits.transfer_budget_ceiling_bytes`) across the ZIP and
the extracted FASTA together, refuses to start if the workspace quota would be exceeded, runs
under the call deadline, verifies member MD5s against the package's bounded `md5sum.txt`, and
removes partial files on any failure or cancellation.
"""

from __future__ import annotations

from typing import Any

from genomics_mcp.archives._common.core import SourceRuntime, share_limiters, to_core, to_core_error
from genomics_mcp.archives._common.errors import SourceError
from genomics_mcp.archives.ega.integration import _workspace_usage
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import BudgetExceededError
from genomics_mcp.models import LocalArtifact

PREPARER_COMPONENT = "preparer:ncbi_genome_fasta"


class NcbiGenomePreparer:
    def __init__(self, runtime: SourceRuntime, make_client: Any) -> None:
        self.runtime = runtime
        self.make_client = make_client

    async def prepare(
        self, accession: str, ctx: OperationContext, *, budget_bytes: int | None = None
    ) -> LocalArtifact:
        limits = ctx.settings.limits
        budget = budget_bytes or limits.max_transfer_bytes
        if budget > limits.transfer_budget_ceiling_bytes:
            raise BudgetExceededError(
                "budget exceeds transfer_budget_ceiling_bytes",
                source="ncbi_datasets",
                details={"ceiling": limits.transfer_budget_ceiling_bytes},
            )
        work = ctx.settings.paths.work_dir
        used = _workspace_usage(work) if work.exists() else 0
        if used + budget > limits.workspace_max_bytes:
            raise BudgetExceededError(
                "workspace quota would be exceeded by this preparation",
                source="ncbi_datasets",
                details={"workspace_max_bytes": limits.workspace_max_bytes},
            )
        async with self.runtime.client() as http:
            c = await self.make_client(http, ctx, self.runtime)
            share_limiters(self.runtime, ctx, c.http)
            try:
                art = await c.prepare_genome_fasta(
                    accession,
                    workspace=work / "catalogs" / "ncbi-genomes",
                    budget_bytes=budget,
                    timeout_s=self.runtime.timeout(ctx),
                )
            except SourceError as exc:
                raise to_core_error(exc) from None
        out = to_core(art)
        assert isinstance(out, LocalArtifact)
        return out
