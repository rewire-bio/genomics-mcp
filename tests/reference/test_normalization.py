"""Coordinate conventions, parsing and reference-based normalization."""

from __future__ import annotations

import pytest

from genomics_mcp.references.assemblies import REFSEQ_CHROMOSOMES
from genomics_mcp.references.variants import (
    NeedMoreReference,
    ReferenceWindow,
    VariantInputError,
    detect_kind,
    normalize_allele,
    parse_hgvs_genomic,
    parse_spdi,
    parse_vcf_string,
    structured_allele,
)

# 0-based offsets: 1000 G, 1001 G, 1002 C, 1003 T, 1004 C, 1005 A, 1006 C, 1007 A, 1008 C, 1009 A, 1010 G, 1011 T, 1012 T
SEQ = "GGCTCACACAGTT"
WINDOW = ReferenceWindow(assembly="GRCh38", contig="7", start=1000, sequence=SEQ, source="test")


def one(alleles):
    assert len(alleles) == 1
    return alleles[0]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("7-140753336-A-T", "vcf"),
        ("chr7:140753336A>T", "vcf"),
        ("7 140753336 A T", "vcf"),
        ("NC_000007.14:140753335:A:T", "spdi"),
        ("NC_000007.14:g.140753336A>T", "hgvs_genomic"),
        ("NM_004333.6(BRAF):c.1799T>A", "hgvs_coding"),
        ("NP_004324.2:p.Val600Glu", "hgvs_protein"),
        ("rs113488022", "rsid"),
        ("BRCA2", "unknown"),
    ],
)
def test_detect_kind(text: str, kind: str) -> None:
    assert detect_kind(text) == kind


def test_vcf_position_is_one_based_and_canonical_is_zero_based_half_open() -> None:
    raw = one(parse_vcf_string("chr7:1004 T>G", "GRCh38"))
    assert (raw.contig, raw.start, raw.end) == ("7", 1003, 1004)
    ops = [t.operation for t in raw.transformations]
    assert ops == ["contig_renamed", "vcf_to_zero_based"]
    out = normalize_allele(raw, WINDOW)
    v = out.variant
    assert (v.start, v.end, v.ref, v.alt) == (1003, 1004, "T", "G")
    assert v.vcf.pos == 1004
    assert v.spdi == v.ncbi_canonical_spdi == "NC_000007.14:1003:T:G"
    assert v.hgvs_g == "NC_000007.14:g.1004T>G"
    assert v.reference_check.status == "verified"


def test_structured_start_and_position_agree() -> None:
    a = one(structured_allele(assembly="GRCh38", contig="7", position=1004, ref="T", alt="G"))
    b = one(structured_allele(assembly="GRCh38", contig="7", start=1003, ref="T", alt="G"))
    assert (a.start, a.end) == (b.start, b.end) == (1003, 1004)
    with pytest.raises(VariantInputError):
        structured_allele(
            assembly="GRCh38", contig="7", position=1004, start=1003, ref="T", alt="G"
        )


def test_assembly_is_required_and_aliases_are_reported() -> None:
    with pytest.raises(VariantInputError, match="assembly is required"):
        parse_vcf_string("7-1004-T-G", None)
    raw = one(parse_vcf_string("7-1004-T-G", "hg38"))
    assert raw.assembly == "GRCh38"
    assert raw.transformations[0].operation == "assembly_alias"


def test_deletion_in_repeat_is_left_aligned_with_reference() -> None:
    # VCF record placed at the right end of the CA repeat.
    raw = one(parse_vcf_string("7-1008-ACA-A", "GRCh38"))
    out = normalize_allele(raw, WINDOW)
    v = out.variant
    assert v.normalization_status == "reference_normalized"
    assert (v.start, v.end, v.ref, v.alt) == (1004, 1006, "CA", "")
    assert (v.vcf.pos, v.vcf.ref, v.vcf.alt) == (1004, "TCA", "T")
    assert v.spdi == "NC_000007.14:1004:CA:"
    # NCBI fully-justified form spans the whole repeat; HGVS uses the 3' rule.
    assert v.ncbi_canonical_spdi == "NC_000007.14:1004:CACACA:CACA"
    assert v.hgvs_g == "NC_000007.14:g.1009_1010del"
    assert "left_align" in [t.operation for t in out.transformations]


def test_insertion_matching_preceding_bases_is_hgvs_dup() -> None:
    raw = one(parse_hgvs_genomic("NC_000007.14:g.1005_1006dup", None))
    assert raw.assembly == "GRCh38"
    v = normalize_allele(raw, WINDOW).variant
    assert (v.start, v.end, v.ref, v.alt) == (1004, 1004, "", "CA")
    assert (v.vcf.pos, v.vcf.ref, v.vcf.alt) == (1004, "T", "TCA")
    assert v.hgvs_g == "NC_000007.14:g.1009_1010dup"
    assert v.ncbi_canonical_spdi == "NC_000007.14:1004:CACACA:CACACACA"


def test_plain_insertion_is_hgvs_ins() -> None:
    v = normalize_allele(one(parse_vcf_string("7-1003-C-CGG", "GRCh38")), WINDOW).variant
    assert (v.start, v.ref, v.alt) == (1003, "", "GG")
    assert v.hgvs_g == "NC_000007.14:g.1003_1004insGG"


def test_without_reference_indel_is_only_trimmed_and_marked() -> None:
    raw = one(parse_vcf_string("7-1008-ACA-A", "GRCh38"))
    out = normalize_allele(raw, None)
    v = out.variant
    assert v.normalization_status == "trimmed_only"
    assert v.reference_check.status == "not_checked"
    assert v.spdi is None and v.ncbi_canonical_spdi is None and v.hgvs_g is None
    # The caller's VCF record is kept as given, not claimed to be left-aligned.
    assert (v.vcf.pos, v.vcf.ref, v.vcf.alt) == (1008, "ACA", "A")
    assert any("not left-aligned" in lim for lim in out.limitations)


def test_snv_without_reference_is_not_verified_but_needs_no_shift() -> None:
    v = normalize_allele(one(parse_vcf_string("7-1004-T-G", "GRCh38")), None).variant
    assert v.normalization_status == "trimmed_only"
    assert v.reference_check.status == "not_checked"
    assert v.ncbi_canonical_spdi == "NC_000007.14:1003:T:G"


def test_reference_mismatch_is_reported_not_normalized() -> None:
    out = normalize_allele(one(parse_vcf_string("7-1004-A-G", "GRCh38")), WINDOW)
    v = out.variant
    assert v.normalization_status == "reference_mismatch"
    assert v.reference_check.expected_ref == "A"
    assert v.reference_check.observed_ref == "T"
    assert v.vcf is None


def test_multiallelic_vcf_is_split_into_snv_and_indel() -> None:
    raws = parse_vcf_string("7-1004-T-G,TCA", "GRCh38")
    assert [r.alt for r in raws] == ["G", "TCA"]
    assert all("split_multiallelic" in [t.operation for t in r.transformations] for r in raws)
    snv, ins = (normalize_allele(r, WINDOW).variant for r in raws)
    assert snv.variant_class == "SNV"
    assert ins.variant_class == "insertion"
    assert (ins.start, ins.alt) == (1004, "CA")  # left-aligned in the CA repeat


def test_shift_at_window_edge_requests_more_reference() -> None:
    narrow = ReferenceWindow(
        assembly="GRCh38", contig="7", start=1004, sequence="CACACAG", source="test"
    )
    raw = one(parse_vcf_string("7-1008-ACA-A", "GRCh38"))
    with pytest.raises(NeedMoreReference):
        normalize_allele(raw, narrow)
    out = normalize_allele(raw, narrow, allow_incomplete=True)
    assert out.variant.normalization_status == "trimmed_only"
    assert out.variant.reference_check.status == "verified"


def test_hgvs_deletion_needs_reference_for_deleted_bases() -> None:
    raw = one(parse_hgvs_genomic("NC_000007.14:g.1005_1006del", None))
    assert raw.ref is None
    with pytest.raises(VariantInputError, match="reference sequence is required"):
        normalize_allele(raw, None)
    v = normalize_allele(raw, WINDOW).variant
    assert v.ref == "CA" and v.hgvs_g == "NC_000007.14:g.1009_1010del"


def test_refseq_accession_implies_assembly_and_blocks_liftover() -> None:
    assert one(parse_hgvs_genomic("NC_000007.13:g.140453136A>T", None)).assembly == "GRCh37"
    with pytest.raises(VariantInputError, match="no liftover"):
        parse_hgvs_genomic("NC_000007.13:g.140453136A>T", "GRCh38")
    with pytest.raises(VariantInputError, match="no version"):
        parse_hgvs_genomic("NC_000007:g.140453136A>T", None)


def test_mitochondrial_accession_is_shared_and_needs_explicit_assembly() -> None:
    with pytest.raises(VariantInputError, match="supply assembly"):
        parse_spdi("NC_012920.1:72:T:C", None)
    assert one(parse_spdi("NC_012920.1:72:T:C", "GRCh37")).contig == "MT"
    with pytest.raises(VariantInputError, match="chrM is ambiguous"):
        parse_vcf_string("chrM-73-T-C", "GRCh37")
    assert one(parse_vcf_string("chrM-73-T-C", "GRCh38")).contig == "MT"


@pytest.mark.parametrize("alt", ["<DEL>", "*", "."])
def test_symbolic_alleles_are_rejected(alt: str) -> None:
    with pytest.raises(VariantInputError):
        structured_allele(assembly="GRCh38", contig="7", position=1004, ref="T", alt=alt)


def test_refseq_table_matches_ncbi_assembly_reports() -> None:
    assert REFSEQ_CHROMOSOMES["GRCh38"]["7"] == "NC_000007.14"
    assert REFSEQ_CHROMOSOMES["GRCh37"]["7"] == "NC_000007.13"
    assert REFSEQ_CHROMOSOMES["GRCh38"]["X"] == "NC_000023.11"
    assert REFSEQ_CHROMOSOMES["GRCh37"]["Y"] == "NC_000024.9"
    assert REFSEQ_CHROMOSOMES["GRCh38"]["17"] == "NC_000017.11"
