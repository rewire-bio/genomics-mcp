"""Human assembly and contig naming.

RefSeq chromosome accessions are from the NCBI assembly reports for
GCF_000001405.40 (GRCh38.p14) and GCF_000001405.25 (GRCh37.p13), checked
2026-09-24. Contigs are reported in Ensembl/NCBI style ("7", "X", "MT").
"""

from __future__ import annotations

from .models import Assembly, Transformation

ASSEMBLY_ACCESSIONS: dict[str, str] = {
    "GRCh38": "GCF_000001405.40",
    "GRCh37": "GCF_000001405.25",
}

_CHROMS = [str(i) for i in range(1, 23)] + ["X", "Y", "MT"]

_GRCH38_VERSIONS = [
    11,
    12,
    12,
    12,
    10,
    12,
    14,
    11,
    12,
    11,
    10,
    12,
    11,
    9,
    10,
    10,
    11,
    10,
    10,
    11,
    9,
    11,
    11,
    10,
]
_GRCH37_VERSIONS = [
    10,
    11,
    11,
    11,
    9,
    11,
    13,
    10,
    11,
    10,
    9,
    11,
    10,
    8,
    9,
    9,
    10,
    9,
    9,
    10,
    8,
    10,
    10,
    9,
]


def _table(versions: list[int]) -> dict[str, str]:
    table = {str(i + 1): f"NC_{i + 1:06d}.{v}" for i, v in enumerate(versions[:22])}
    table["X"] = f"NC_000023.{versions[22]}"
    table["Y"] = f"NC_000024.{versions[23]}"
    table["MT"] = "NC_012920.1"
    return table


REFSEQ_CHROMOSOMES: dict[str, dict[str, str]] = {
    "GRCh38": _table(_GRCH38_VERSIONS),
    "GRCh37": _table(_GRCH37_VERSIONS),
}

ASSEMBLY_ALIASES: dict[str, Assembly] = {
    "grch38": "GRCh38",
    "hg38": "GRCh38",
    "grch37": "GRCh37",
    "hg19": "GRCh37",
}


class AssemblyError(ValueError):
    pass


def normalize_assembly(value: str | None, transformations: list[Transformation]) -> Assembly:
    if not value:
        raise AssemblyError(
            "assembly is required (GRCh38 or GRCh37); no default or liftover is applied"
        )
    key = value.strip().lower()
    if "." in key:
        key = key.split(".", 1)[0]
    assembly = ASSEMBLY_ALIASES.get(key)
    if assembly is None:
        raise AssemblyError(f"unsupported assembly {value!r}; use GRCh38 or GRCh37")
    if value != assembly:
        transformations.append(
            Transformation(
                operation="assembly_alias",
                detail=f"assembly {value!r} interpreted as {assembly}",
                before={"assembly": value},
                after={"assembly": assembly},
            )
        )
    return assembly


def refseq_to_contig(accession: str) -> list[tuple[Assembly, str]]:
    """All (assembly, contig) pairs whose chromosome accession matches exactly."""
    hits: list[tuple[Assembly, str]] = []
    for assembly, table in REFSEQ_CHROMOSOMES.items():
        for contig, acc in table.items():
            if acc == accession:
                hits.append((assembly, contig))  # type: ignore[arg-type]
    return hits


def normalize_contig(value: str, assembly: Assembly, transformations: list[Transformation]) -> str:
    raw = value.strip()
    if raw.upper().startswith("NC_"):
        hits = [c for a, c in refseq_to_contig(raw.upper()) if a == assembly]
        if not hits:
            raise AssemblyError(
                f"{raw} is not a {assembly} chromosome accession; no liftover is applied"
            )
        contig = hits[0]
        transformations.append(
            Transformation(
                operation="contig_from_refseq",
                detail=f"{raw} is {assembly} chromosome {contig}",
                before={"contig": raw},
                after={"contig": contig},
            )
        )
        return contig
    name = raw
    if name.lower().startswith("chr"):
        name = name[3:]
    upper = name.upper()
    if upper in ("M", "MT"):
        if assembly == "GRCh37" and raw.lower() in ("chrm", "m"):
            raise AssemblyError(
                "chrM is ambiguous for GRCh37 (UCSC hg19 chrM is not the rCRS MT sequence); use MT or NC_012920.1"
            )
        upper = "MT"
    if upper not in _CHROMS:
        raise AssemblyError(
            f"unsupported contig {raw!r}; only primary human chromosomes are supported"
        )
    if upper != raw:
        transformations.append(
            Transformation(
                operation="contig_renamed",
                detail=f"contig {raw!r} written as {upper!r} (Ensembl/NCBI naming)",
                before={"contig": raw},
                after={"contig": upper},
            )
        )
    return upper


def refseq_accession(assembly: Assembly, contig: str) -> str | None:
    return REFSEQ_CHROMOSOMES[assembly].get(contig)
