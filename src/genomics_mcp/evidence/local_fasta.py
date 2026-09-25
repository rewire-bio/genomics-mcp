"""Reference bases from a caller-named FASTA, read locally for REF checks and indel shifting.

The FASTA must be a local file with an explicit assembly matching the variant and an
existing `.fai` index (plus `.gzi` for BGZF). Preparation is never implicit: the native
reader is always given explicit index paths, so htslib cannot create a missing index.
The FASTA and every auxiliary file it may open are checked against the allowed roots
after resolving symlinks. Remote references are refused before any resolver or network
use: fetch/prepare them locally first. Reading sends nothing to any external service.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from genomics_mcp.errors import GenomicsError, InvalidInputError, PreparationRequiredError
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


def require_local_reference(file: FileRef) -> None:
    """Refuse non-local references up front: no resolver call, no network, no remote reads."""
    if not file.is_local:
        raise PreparationRequiredError(
            f"reference FASTA must be a local file; {file.scheme!r} references are not read here",
            source=SOURCE,
            hint="fetch the FASTA with its .fai (and .gzi if BGZF) using fetch_file, then pass "
            "the local path; or omit `reference` to use Ensembl/NCBI reference sources",
        )


_GZIP_MAGIC = b"\x1f\x8b"


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
        require_local_reference(file)
        self.ctx = ctx
        self.file = file
        self.assembly = assembly
        self.transformations: list[Transformation] = []

    def _aux(self, candidates: list[str], what: str) -> Path:
        """First existing auxiliary file, each candidate checked (symlinks resolved) against the
        allowed roots before any existence test or read."""
        for raw in candidates:
            resolved = self.ctx.resolve_local_path(raw, must_exist=False)
            if resolved.exists():
                return resolved
        raise failure(
            SOURCE,
            "faidx",
            "unsupported",
            f"FASTA {what} is missing; run `samtools faidx` or fetch_file with its index first "
            "(indexes are never created implicitly)",
        )

    def _paths(self) -> tuple[str, str, str | None]:
        require_local_reference(self.file)
        given = self.file.uri.removeprefix("file://")
        path = self.ctx.resolve_local_path(given)
        names = list(dict.fromkeys([given, str(path)]))
        if self.file.index_uri:
            fai = self._aux([self.file.index_uri], "index (.fai)")
        else:
            fai = self._aux([f"{n}.fai" for n in names], "index (.fai)")
        with path.open("rb") as fh:
            compressed = fh.read(2) == _GZIP_MAGIC
        gzi = self._aux([f"{n}.gzi" for n in names], "BGZF index (.gzi)") if compressed else None
        return str(path), str(fai), str(gzi) if gzi else None

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
            path, index, gzi = self._paths()
        except GenomicsError as exc:
            kinds = {"unauthorized": "unauthorized", "not_found": "not_found",
                     "preparation_required": "unsupported"}  # fmt: skip
            raise failure(SOURCE, "open", kinds.get(exc.info.code, "invalid_input"),
                          exc.info.message) from None  # fmt: skip
        except OSError as exc:
            raise failure(SOURCE, "open", "invalid_response",
                          f"cannot read local FASTA: {type(exc).__name__}") from None  # fmt: skip

        def read() -> tuple[str, str, int]:
            import pysam

            # Explicit index paths only: htslib would otherwise build a missing .fai itself.
            extra = {"filepath_index_compressed": gzi} if gzi else {}
            with pysam.FastaFile(path, filepath_index=index, **extra) as fasta:
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
