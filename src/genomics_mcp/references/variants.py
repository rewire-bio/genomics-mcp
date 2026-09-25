"""Variant input parsing and allele normalization.

Coordinate conventions
----------------------
* Structured input ``position`` is the 1-based VCF POS of the first REF base.
  Structured input ``start`` is the 0-based start (0-based half-open); only one
  of the two may be supplied.
* VCF strings (``7-140753336-A-T``, ``chr7:140753336:A:T``, ``7:140753336 A>T``)
  use 1-based POS.
* Canonical output is 0-based half-open with minimal alleles; ``ref``/``alt``
  may be empty for pure insertions/deletions.

Normalization follows the usual VCF convention (trim shared suffix, then shared
prefix, then shift pure indels left against the reference). HGVS genomic output
uses the 3' rule instead, so it is derived from a separately right-shifted form.
Nothing is shifted without actual reference sequence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .assemblies import (
    AssemblyError,
    normalize_assembly,
    normalize_contig,
    refseq_accession,
    refseq_to_contig,
)
from .models import (
    Assembly,
    CanonicalVariant,
    ReferenceCheck,
    Transformation,
    VcfRepresentation,
)

_BASES = re.compile(r"^[ACGTN]*$")

InputKind = Literal[
    "structured",
    "vcf",
    "spdi",
    "hgvs_genomic",
    "hgvs_coding",
    "hgvs_noncoding",
    "hgvs_protein",
    "hgvs_other",
    "rsid",
    "unknown",
]

RSID_RE = re.compile(r"^rs(\d+)$", re.IGNORECASE)
SPDI_RE = re.compile(r"^(NC_\d+\.\d+):(\d+):([ACGTNacgtn]*):([ACGTNacgtn]*)$")
HGVS_RE = re.compile(
    r"^(?P<acc>[A-Za-z0-9_.\-]+?)(?:\((?P<paren>[^)]*)\))?:(?P<kind>[gcnmrp])\.(?P<change>.+)$"
)
VCF_RE = re.compile(
    r"^(?P<contig>(?:chr)?(?:[0-9]{1,2}|X|Y|M|MT)|NC_\d+\.\d+)[:\-\s_]+(?P<pos>\d+)[:\-\s_]*"
    r"(?P<ref>[ACGTN]+)[:\-\s>_/]+(?P<alt>[ACGTN]+(?:,[ACGTN]+)*)$",
    re.IGNORECASE,
)


class VariantInputError(ValueError):
    pass


class NeedMoreReference(Exception):
    """Shifting reached the edge of the fetched reference window."""


def detect_kind(text: str) -> InputKind:
    value = text.strip()
    if RSID_RE.match(value):
        return "rsid"
    if SPDI_RE.match(value):
        return "spdi"
    m = HGVS_RE.match(value)
    if m:
        return {
            "g": "hgvs_genomic",
            "c": "hgvs_coding",
            "n": "hgvs_noncoding",
            "p": "hgvs_protein",
        }.get(m.group("kind"), "hgvs_other")  # type: ignore[return-value]
    if VCF_RE.match(value):
        return "vcf"
    return "unknown"


def accession_has_version(accession: str) -> bool:
    if accession.startswith("LRG_"):
        return True
    return bool(re.search(r"\.\d+$", accession))


@dataclass
class RawAllele:
    """An allele as supplied, 0-based half-open. ``ref`` is None when unknown."""

    assembly: Assembly
    contig: str
    start: int
    end: int
    ref: str | None
    alt: str
    origin: str
    transformations: list[Transformation] = field(default_factory=list)


@dataclass
class ReferenceWindow:
    """Reference bases for [start, start+len(sequence)) on one contig."""

    assembly: Assembly
    contig: str
    start: int
    sequence: str
    source: str
    at_contig_start: bool = False
    at_contig_end: bool = False

    @property
    def end(self) -> int:
        return self.start + len(self.sequence)

    def covers(self, start: int, end: int) -> bool:
        return self.start <= start and end <= self.end

    def get(self, start: int, end: int) -> str:
        if not self.covers(start, end):
            raise NeedMoreReference()
        return self.sequence[start - self.start : end - self.start]


def _bases(value: str, what: str) -> str:
    upper = value.strip().upper()
    if upper in (".", "*") or upper.startswith("<"):
        raise VariantInputError(
            f"{what} {value!r} is a symbolic/missing allele; only sequence alleles are supported"
        )
    if not _BASES.match(upper):
        raise VariantInputError(f"{what} {value!r} must contain only A, C, G, T or N")
    return upper


def structured_allele(
    *,
    assembly: str | None,
    contig: str,
    ref: str,
    alt: str,
    position: int | None = None,
    start: int | None = None,
) -> list[RawAllele]:
    transformations: list[Transformation] = []
    try:
        asm = normalize_assembly(assembly, transformations)
        chrom = normalize_contig(contig, asm, transformations)
    except AssemblyError as exc:
        raise VariantInputError(str(exc)) from None
    if (position is None) == (start is None):
        raise VariantInputError("give exactly one of position (1-based VCF POS) or start (0-based)")
    ref_u = _bases(ref, "ref")
    if not ref_u:
        raise VariantInputError(
            "ref must not be empty; use a VCF anchor base or a 0-based start with ref=''"
        )
    if position is not None:
        if position < 1:
            raise VariantInputError("position is 1-based and must be >= 1")
        zero = position - 1
        transformations.append(
            Transformation(
                operation="vcf_to_zero_based",
                detail="1-based VCF POS converted to 0-based half-open interval",
                before={"position": position, "ref": ref_u},
                after={"start": zero, "end": zero + len(ref_u)},
            )
        )
    else:
        assert start is not None
        if start < 0:
            raise VariantInputError("start is 0-based and must be >= 0")
        zero = start
    return _split_alts(asm, chrom, zero, ref_u, alt, "structured", transformations)


def _split_alts(
    assembly: Assembly,
    contig: str,
    start: int,
    ref: str,
    alt: str,
    origin: str,
    transformations: list[Transformation],
) -> list[RawAllele]:
    alts = [a for a in alt.split(",")]
    alleles = []
    for a in alts:
        alt_u = _bases(a, "alt")
        if alt_u == ref:
            raise VariantInputError(f"alt {a!r} equals ref; not a variant")
        steps = list(transformations)
        if len(alts) > 1:
            steps.append(
                Transformation(
                    operation="split_multiallelic",
                    detail=f"ALT {alt!r} split; this record is allele {alt_u!r}",
                    before={"alt": alt},
                    after={"alt": alt_u},
                )
            )
        alleles.append(
            RawAllele(
                assembly=assembly,
                contig=contig,
                start=start,
                end=start + len(ref),
                ref=ref,
                alt=alt_u,
                origin=origin,
                transformations=steps,
            )
        )
    return alleles


def parse_vcf_string(text: str, assembly: str | None) -> list[RawAllele]:
    m = VCF_RE.match(text.strip())
    if not m:
        raise VariantInputError(f"not a VCF-style variant: {text!r}")
    contig_raw = m.group("contig")
    transformations: list[Transformation] = []
    asm_value = assembly
    if contig_raw.upper().startswith("NC_"):
        asm_value = _assembly_from_refseq(contig_raw.upper(), assembly)
    try:
        asm = normalize_assembly(asm_value, transformations)
        chrom = normalize_contig(contig_raw, asm, transformations)
    except AssemblyError as exc:
        raise VariantInputError(str(exc)) from None
    pos = int(m.group("pos"))
    if pos < 1:
        raise VariantInputError("VCF POS is 1-based and must be >= 1")
    ref = _bases(m.group("ref"), "ref")
    transformations.append(
        Transformation(
            operation="vcf_to_zero_based",
            detail="1-based VCF POS converted to 0-based half-open interval",
            before={"pos": pos, "ref": ref},
            after={"start": pos - 1, "end": pos - 1 + len(ref)},
        )
    )
    return _split_alts(asm, chrom, pos - 1, ref, m.group("alt"), "vcf", transformations)


def _assembly_from_refseq(accession: str, assembly: str | None) -> str:
    hits = refseq_to_contig(accession)
    if not hits:
        raise VariantInputError(f"{accession} is not a known GRCh37/GRCh38 chromosome accession")
    assemblies = sorted({a for a, _ in hits})
    if assembly:
        tmp: list[Transformation] = []
        try:
            wanted = normalize_assembly(assembly, tmp)
        except AssemblyError as exc:
            raise VariantInputError(str(exc)) from None
        if wanted not in assemblies:
            raise VariantInputError(
                f"{accession} belongs to {', '.join(assemblies)}, not {wanted}; no liftover is applied"
            )
        return wanted
    if len(assemblies) > 1:
        raise VariantInputError(
            f"{accession} is shared by {', '.join(assemblies)}; supply assembly explicitly"
        )
    return assemblies[0]


def parse_spdi(text: str, assembly: str | None) -> list[RawAllele]:
    m = SPDI_RE.match(text.strip())
    if not m:
        raise VariantInputError(f"not an SPDI: {text!r}")
    accession, pos, deleted, inserted = m.groups()
    asm_value = _assembly_from_refseq(accession, assembly)
    transformations: list[Transformation] = []
    asm = normalize_assembly(asm_value, transformations)
    chrom = normalize_contig(accession, asm, transformations)
    start = int(pos)
    deleted_u = deleted.upper()
    inserted_u = inserted.upper()
    if deleted_u == inserted_u:
        raise VariantInputError("SPDI deleted and inserted sequences are equal; not a variant")
    return [
        RawAllele(
            assembly=asm,
            contig=chrom,
            start=start,
            end=start + len(deleted_u),
            ref=deleted_u,
            alt=inserted_u,
            origin="spdi",
            transformations=transformations,
        )
    ]


_G_SUB = re.compile(r"^(\d+)([ACGTN])>([ACGTN])$")
_G_DEL = re.compile(r"^(\d+)(?:_(\d+))?del([ACGTN]*)$")
_G_DUP = re.compile(r"^(\d+)(?:_(\d+))?dup([ACGTN]*)$")
_G_INS = re.compile(r"^(\d+)_(\d+)ins([ACGTN]+)$")
_G_DELINS = re.compile(r"^(\d+)(?:_(\d+))?delins([ACGTN]+)$")


def parse_hgvs_genomic(text: str, assembly: str | None) -> list[RawAllele]:
    m = HGVS_RE.match(text.strip())
    if not m or m.group("kind") != "g":
        raise VariantInputError(f"not a genomic HGVS expression: {text!r}")
    accession = m.group("acc").upper()
    change = m.group("change")
    change = re.sub(r"[acgtn]+$", lambda x: x.group(0).upper(), change)
    change = re.sub(r"(?<=\d)([acgtn])>([acgtn])$", lambda x: x.group(0).upper(), change)
    transformations: list[Transformation] = []
    if accession.startswith("NC_"):
        if not accession_has_version(accession):
            raise VariantInputError(
                f"{accession} has no version; a versioned chromosome accession is required"
            )
        asm_value = _assembly_from_refseq(accession, assembly)
        asm = normalize_assembly(asm_value, transformations)
        chrom = normalize_contig(accession, asm, transformations)
    else:
        try:
            asm = normalize_assembly(assembly, transformations)
            chrom = normalize_contig(accession, asm, transformations)
        except AssemblyError as exc:
            raise VariantInputError(
                f"genomic HGVS reference {accession!r} is not a chromosome accession or name: {exc}"
            ) from None
    origin = "hgvs_genomic"
    if (s := _G_SUB.match(change)) is not None:
        pos = int(s.group(1))
        return [
            RawAllele(asm, chrom, pos - 1, pos, s.group(2), s.group(3), origin, transformations)
        ]
    if (s := _G_DELINS.match(change)) is not None:
        first, last = int(s.group(1)), int(s.group(2) or s.group(1))
        _check_range(first, last)
        return [RawAllele(asm, chrom, first - 1, last, None, s.group(3), origin, transformations)]
    if (s := _G_DEL.match(change)) is not None:
        first, last = int(s.group(1)), int(s.group(2) or s.group(1))
        _check_range(first, last)
        given = s.group(3) or None
        if given is not None and len(given) != last - first + 1:
            raise VariantInputError("deleted sequence length does not match the deleted range")
        return [RawAllele(asm, chrom, first - 1, last, given, "", origin, transformations)]
    if (s := _G_DUP.match(change)) is not None:
        first, last = int(s.group(1)), int(s.group(2) or s.group(1))
        _check_range(first, last)
        given = s.group(3) or None
        if given is not None and len(given) != last - first + 1:
            raise VariantInputError("duplicated sequence length does not match the range")
        # Represent the duplication as the reference span replaced by two copies.
        dup = RawAllele(
            asm, chrom, first - 1, last, given, given * 2 if given else "", origin, transformations
        )
        dup.transformations = [
            *transformations,
            Transformation(
                operation="dup_as_span",
                detail="duplication represented as reference span replaced by two copies before trimming",
            ),
        ]
        return [dup]
    if (s := _G_INS.match(change)) is not None:
        first, second = int(s.group(1)), int(s.group(2))
        if second != first + 1:
            raise VariantInputError("HGVS insertion flanks must be adjacent positions")
        return [RawAllele(asm, chrom, first, first, "", s.group(3), origin, transformations)]
    raise VariantInputError(
        f"unsupported genomic HGVS change {m.group('change')!r}; supported: substitution, del, dup, ins, delins"
    )


def _check_range(first: int, last: int) -> None:
    if first < 1 or last < first:
        raise VariantInputError("invalid HGVS position range")


# ---------------------------------------------------------------------------
# Normalization


def trim_alleles(start: int, ref: str, alt: str) -> tuple[int, str, str, int, int]:
    """Trim shared suffix, then shared prefix. Returns (start, ref, alt, n_suffix, n_prefix)."""
    n_suffix = 0
    while ref and alt and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]
        n_suffix += 1
    n_prefix = 0
    while ref and alt and ref[0] == alt[0]:
        ref, alt = ref[1:], alt[1:]
        start += 1
        n_prefix += 1
    return start, ref, alt, n_suffix, n_prefix


def variant_class(ref: str, alt: str) -> str:
    if len(ref) == 1 and len(alt) == 1:
        return "SNV"
    if not ref:
        return "insertion"
    if not alt:
        return "deletion"
    if len(ref) == len(alt):
        return "MNV"
    return "delins"


def shift_left(start: int, ref: str, alt: str, window: ReferenceWindow) -> tuple[int, str, str]:
    """Shift a pure indel left. Raises NeedMoreReference at the window edge."""
    if ref and alt:
        return start, ref, alt
    allele = ref or alt
    while True:
        if start == 0:
            break  # contig start: keep the rotation applied so far
        if start - 1 < window.start:
            raise NeedMoreReference()
        prev = window.get(start - 1, start)
        if prev != allele[-1]:
            break
        allele = allele[-1] + allele[:-1]
        start -= 1
    return (start, allele, "") if ref else (start, "", allele)


def shift_right(start: int, ref: str, alt: str, window: ReferenceWindow) -> tuple[int, str, str]:
    """Shift a pure indel right (HGVS 3' rule)."""
    if ref and alt:
        return start, ref, alt
    allele = ref or alt
    end = start + len(ref)
    while True:
        if end >= window.end:
            if window.at_contig_end:
                break
            raise NeedMoreReference()
        nxt = window.get(end, end + 1)
        if nxt != allele[0]:
            break
        allele = allele[1:] + allele[0]
        start += 1
        end += 1
    return (start, allele, "") if ref else (start, "", allele)


@dataclass
class NormalizationOutcome:
    variant: CanonicalVariant
    transformations: list[Transformation]
    limitations: list[str]


def normalize_allele(
    raw: RawAllele, window: ReferenceWindow | None, *, allow_incomplete: bool = False
) -> NormalizationOutcome:
    """Normalize one allele, using ``window`` when real reference sequence is available.

    Raises ``NeedMoreReference`` when an indel shift reaches the window edge and
    ``allow_incomplete`` is false.
    """
    steps = list(raw.transformations)
    limitations: list[str] = []
    accession = refseq_accession(raw.assembly, raw.contig)
    ref_in = raw.ref
    check: ReferenceCheck
    if window is not None:
        if not window.covers(raw.start, raw.end):
            raise NeedMoreReference()
        observed = window.get(raw.start, raw.end)
        if ref_in is None:
            ref_in = observed
            steps.append(
                Transformation(
                    operation="ref_from_reference",
                    source=window.source,
                    detail="reference bases for the HGVS range taken from reference sequence",
                    after={"ref": observed},
                )
            )
            check = ReferenceCheck(
                status="verified",
                source=window.source,
                observed_ref=observed,
                detail="ref not supplied in input; filled from reference",
            )
        elif observed != ref_in:
            check = ReferenceCheck(
                status="mismatch",
                source=window.source,
                expected_ref=ref_in,
                observed_ref=observed,
                detail="input REF does not match the reference sequence",
            )
            return _mismatch(raw, accession, check, steps)
        else:
            check = ReferenceCheck(
                status="verified", source=window.source, expected_ref=ref_in, observed_ref=observed
            )
    else:
        if ref_in is None:
            raise VariantInputError(
                "deleted/replaced bases are not given; reference sequence is required"
            )
        check = ReferenceCheck(status="not_checked", detail="no reference sequence was available")

    alt_in = raw.alt
    # Duplications are stored as span -> two copies; make that concrete now.
    if (
        raw.origin == "hgvs_genomic"
        and raw.ref is None
        and alt_in == ""
        and any(t.operation == "dup_as_span" for t in raw.transformations)
    ):
        alt_in = ref_in * 2

    start, ref, alt, n_suffix, n_prefix = trim_alleles(raw.start, ref_in, alt_in)
    if not ref and not alt:
        raise VariantInputError("ref equals alt after trimming; not a variant")
    if n_suffix or n_prefix:
        steps.append(
            Transformation(
                operation="trim_shared_bases",
                detail=f"removed {n_suffix} shared suffix and {n_prefix} shared prefix base(s)",
                before={"start": raw.start, "ref": ref_in, "alt": alt_in},
                after={"start": start, "ref": ref, "alt": alt},
            )
        )
    klass = variant_class(ref, alt)
    pure_indel = not ref or not alt
    right: tuple[int, str, str] | None = None
    status: str
    if window is None:
        status = "trimmed_only"
        if pure_indel:
            limitations.append(
                "No reference sequence was used: the indel was trimmed but not left-aligned, so it may not match "
                "the normalized representation used by gnomAD/ClinVar."
            )
    elif pure_indel:
        try:
            left = shift_left(start, ref, alt, window)
            right = shift_right(start, ref, alt, window)
        except NeedMoreReference:
            if not allow_incomplete:
                raise
            status = "trimmed_only"
            limitations.append(
                "Repeat extends beyond the fetched reference window; the indel was not shifted."
            )
        else:
            status = "reference_normalized"
            if left[0] != start:
                steps.append(
                    Transformation(
                        operation="left_align",
                        source=window.source,
                        detail=f"indel shifted {start - left[0]} base(s) left using reference sequence",
                        before={"start": start, "ref": ref, "alt": alt},
                        after={"start": left[0], "ref": left[1], "alt": left[2]},
                    )
                )
            start, ref, alt = left
    else:
        status = "reference_normalized"

    end = start + len(ref)
    vcf = _vcf(raw, start, ref, alt, window)
    spdi = None
    if accession and (not pure_indel or status == "reference_normalized"):
        spdi = f"{accession}:{start}:{ref}:{alt}"
    ncbi_spdi = None
    hgvs_g = None
    if accession:
        if not pure_indel:
            ncbi_spdi = spdi
            hgvs_g = _hgvs_nonshifting(accession, start, end, ref, alt)
        elif status == "reference_normalized" and window is not None and right is not None:
            ncbi_spdi = _ncbi_canonical_spdi(accession, start, ref, alt, right, window)
            hgvs_g = _hgvs_indel(accession, right, window)
    if pure_indel and hgvs_g is None:
        limitations.append("HGVS genomic form not derived: the 3' rule needs reference sequence.")
    if vcf is None and pure_indel:
        if window is None:
            limitations.append(
                "VCF form not derived: the indel anchor base is unknown without reference sequence."
            )
        else:
            limitations.append(
                "VCF form not derived: no reference base is available on either side of the indel to anchor it."
            )
    variant = CanonicalVariant(
        assembly=raw.assembly,
        contig=raw.contig,
        refseq_accession=accession,
        start=start,
        end=end,
        ref=ref,
        alt=alt,
        variant_class=klass,
        normalization_status=status,  # type: ignore[arg-type]
        reference_check=check,
        vcf=vcf,
        spdi=spdi,
        ncbi_canonical_spdi=ncbi_spdi,
        hgvs_g=hgvs_g,
    )
    return NormalizationOutcome(variant=variant, transformations=steps, limitations=limitations)


def _mismatch(
    raw: RawAllele, accession: str | None, check: ReferenceCheck, steps: list[Transformation]
) -> NormalizationOutcome:
    ref = raw.ref or ""
    variant = CanonicalVariant(
        assembly=raw.assembly,
        contig=raw.contig,
        refseq_accession=accession,
        start=raw.start,
        end=raw.end,
        ref=ref,
        alt=raw.alt,
        variant_class=variant_class(ref, raw.alt),
        normalization_status="reference_mismatch",
        reference_check=check,
    )
    return NormalizationOutcome(
        variant=variant,
        transformations=steps,
        limitations=[
            "Input REF disagrees with the reference; the variant was not normalized or looked up."
        ],
    )


def _vcf(
    raw: RawAllele, start: int, ref: str, alt: str, window: ReferenceWindow | None
) -> VcfRepresentation | None:
    if ref and alt:
        return VcfRepresentation(contig=raw.contig, pos=start + 1, ref=ref, alt=alt)
    if start == 0:
        # No base precedes the event: VCF anchors on the first base after it.
        end = start + len(ref)
        after: str | None = None
        if window is not None and window.covers(end, end + 1):
            after = window.get(end, end + 1)
        elif window is None and raw.ref and raw.start <= end < raw.end:
            after = raw.ref[end - raw.start]
        if after is None or after == "":
            return None
        return VcfRepresentation(contig=raw.contig, pos=1, ref=ref + after, alt=alt + after)
    anchor: str | None = None
    if window is not None and window.covers(start - 1, start):
        anchor = window.get(start - 1, start)
    elif window is None and raw.ref and raw.start <= start - 1 < raw.end:
        # Anchor from the (unverified) input REF, e.g. an untrimmed VCF record.
        anchor = raw.ref[start - 1 - raw.start]
    if anchor is None:
        if window is None and raw.ref and raw.alt and raw.origin in ("vcf", "structured"):
            # Keep the caller's anchored record as given; it is not left-aligned.
            return VcfRepresentation(contig=raw.contig, pos=raw.start + 1, ref=raw.ref, alt=raw.alt)
        return None
    return VcfRepresentation(contig=raw.contig, pos=start, ref=anchor + ref, alt=anchor + alt)


def _hgvs_nonshifting(accession: str, start: int, end: int, ref: str, alt: str) -> str:
    if len(ref) == 1 and len(alt) == 1:
        return f"{accession}:g.{start + 1}{ref}>{alt}"
    span = f"{start + 1}" if end - start == 1 else f"{start + 1}_{end}"
    return f"{accession}:g.{span}delins{alt}"


def _hgvs_indel(accession: str, right: tuple[int, str, str], window: ReferenceWindow) -> str | None:
    start, ref, alt = right
    if ref:
        end = start + len(ref)
        span = f"{start + 1}" if len(ref) == 1 else f"{start + 1}_{end}"
        return f"{accession}:g.{span}del"
    n = len(alt)
    if start - n >= window.start and window.get(start - n, start) == alt:
        span = f"{start}" if n == 1 else f"{start - n + 1}_{start}"
        return f"{accession}:g.{span}dup"
    return f"{accession}:g.{start}_{start + 1}ins{alt}"


def _ncbi_canonical_spdi(
    accession: str,
    left_start: int,
    left_ref: str,
    left_alt: str,
    right: tuple[int, str, str],
    window: ReferenceWindow,
) -> str:
    """Fully justified SPDI (NCBI VOCA) spanning the whole ambiguous region."""
    r_start, r_ref, _r_alt = right
    if left_ref:
        region_end = r_start + len(r_ref)
        region = window.get(left_start, region_end)
        inserted = region[: len(region) - len(left_ref)]
        return f"{accession}:{left_start}:{region}:{inserted}"
    region = window.get(left_start, r_start)
    return f"{accession}:{left_start}:{region}:{left_alt + region}"
