"""Reference bases from a caller-named FASTA, read locally for REF checks and indel shifting.

The FASTA must have an explicit assembly matching the variant and an existing `.fai`
index (preparation is never implicit). Local paths must sit under an allowed root.
Reading it sends nothing to any external service.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from genomics_mcp.errors import GenomicsError, InvalidInputError
from genomics_mcp.models import FileRef
from genomics_mcp.references.assemblies import AssemblyError, normalize_assembly
from genomics_mcp.references.http import failure
from genomics_mcp.references.models import Assembly, Transformation
from genomics_mcp.references.variants import ReferenceWindow

if TYPE_CHECKING:
    from genomics_mcp.context import OperationContext

SOURCE = "local_fasta"


def fasta_assembly(file: FileRef, expected: str | None) -> Assembly:
    """The FASTA's declared assembly; it must be explicit and match the variant's."""
    if not file.assembly:
        raise InvalidInputError(
            "reference FASTA needs an explicit assembly (reference.assembly); none is guessed",
            source=SOURCE,
        )
    try:
        declared = normalize_assembly(file.assembly, [])
        wanted = normalize_assembly(expected, []) if expected else declared
    except AssemblyError as exc:
        raise InvalidInputError(str(exc), source=SOURCE) from None
    if declared != wanted:
        raise InvalidInputError(
            f"reference FASTA is {declared} but the variant is {wanted}; no liftover is applied",
            source=SOURCE,
        )
    return declared


def _contig_candidates(assembly: Assembly, contig: str) -> list[str]:
    names = [contig, f"chr{contig}"]
    if contig == "MT":
        names.append("NC_012920.1")
        if assembly == "GRCh38":
            names.append("chrM")  # hg38 chrM is rCRS; hg19 chrM is not, so never for GRCh37
    return names


class LocalFastaProvider:
    """`ReferenceProvider` over an indexed FASTA opened with pysam."""

    def __init__(self, ctx: OperationContext, file: FileRef, assembly: Assembly):
        self.ctx = ctx
        self.file = file
        self.assembly = assembly
        self.transformations: list[Transformation] = []

    async def _paths(self) -> tuple[str, str]:
        if self.file.is_local:
            path = self.ctx.resolve_local_path(self.file.uri)
            index = (
                self.ctx.resolve_local_path(self.file.index_uri, must_exist=False)
                if self.file.index_uri
                else Path(f"{path}.fai")
            )
            if not index.exists():
                raise failure(
                    SOURCE,
                    "faidx",
                    "unsupported",
                    "FASTA index (.fai) is missing; run `samtools faidx` or fetch_file with its index first",
                )
            return str(path), str(index)
        resolved = await self.ctx.resolve_file(self.file)
        target = str(resolved.local_path) if resolved.local_path else resolved.open_uri
        if resolved.index_open_uri is None and resolved.local_path is None:
            raise failure(SOURCE, "faidx", "unsupported", "remote FASTA has no resolved index")
        index = resolved.index_open_uri or f"{target}.fai"
        return target, index

    async def sequence(
        self,
        assembly: Assembly,
        contig: str,
        start: int,
        end: int,
        *,
        deadline: float | None = None,
    ) -> ReferenceWindow:
        if assembly != self.assembly:
            raise failure(
                SOURCE,
                "fetch",
                "invalid_input",
                f"local FASTA is {self.assembly}; {assembly} was requested and no other build is substituted",
            )
        try:
            path, index = await self._paths()
        except GenomicsError as exc:
            kind = "unauthorized" if exc.info.code == "unauthorized" else "invalid_input"
            if exc.info.code == "not_found":
                kind = "not_found"
            raise failure(SOURCE, "open", kind, exc.info.message) from None

        def read() -> tuple[str, str, int]:
            import pysam

            with pysam.FastaFile(path, filepath_index=index) as fasta:
                lengths = dict(zip(fasta.references, fasta.lengths, strict=True))
                for name in _contig_candidates(assembly, contig):
                    if name in lengths:
                        stop = min(end, lengths[name])
                        return name, fasta.fetch(name, start, stop).upper(), lengths[name]
            raise KeyError(contig)

        try:
            name, seq, length = await self.ctx.run_blocking(read)
        except KeyError:
            raise failure(
                SOURCE, "fetch", "not_found", f"contig {contig} is not in the local FASTA"
            ) from None
        except GenomicsError as exc:
            raise failure(
                SOURCE,
                "fetch",
                "timeout" if exc.info.code == "timeout" else "upstream",
                exc.info.message,
            ) from None
        except (OSError, ValueError) as exc:
            raise failure(
                SOURCE,
                "fetch",
                "invalid_response",
                f"cannot read local FASTA: {type(exc).__name__}",
            ) from None
        if name != contig:
            self.transformations.append(
                Transformation(
                    operation="contig_name_in_fasta",
                    source=SOURCE,
                    detail=f"contig {contig} read as {name!r} from the local FASTA",
                )
            )
        return ReferenceWindow(
            assembly=assembly,
            contig=contig,
            start=start,
            sequence=seq,
            source=SOURCE,
            at_contig_start=start == 0,
            at_contig_end=start + len(seq) >= length,
        )
