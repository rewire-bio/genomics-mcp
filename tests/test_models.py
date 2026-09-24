import pytest
from pydantic import ValidationError

from genomics_mcp.errors import InvalidInputError
from genomics_mcp.models import (
    FileFormat,
    FileRef,
    Interval,
    VariantSpec,
    Visibility,
    infer_format,
)


def test_interval_is_zero_based_half_open():
    iv = Interval(contig="chr1", start=0, end=10, assembly="GRCh38")
    assert iv.length == 10
    # samtools region chr1:1-10 covers the same 10 bases.
    assert iv.to_region() == "chr1:1-10"
    assert Interval.parse_region("chr1:1-10", "GRCh38") == iv


@pytest.mark.parametrize(
    "kwargs",
    [
        {"contig": "chr1", "start": 5, "end": 5, "assembly": "GRCh38"},  # empty
        {"contig": "chr1", "start": 6, "end": 5, "assembly": "GRCh38"},  # reversed
        {"contig": "chr1", "start": -1, "end": 5, "assembly": "GRCh38"},
        {"contig": "chr 1", "start": 0, "end": 5, "assembly": "GRCh38"},
        {"contig": "chr1", "start": 0, "end": 5, "assembly": ""},
        {"contig": "chr1", "start": 0, "end": 5},  # assembly is never defaulted
    ],
)
def test_interval_rejects_invalid(kwargs):
    with pytest.raises(ValidationError):
        Interval(**kwargs)


def test_region_parsing_commas_and_braced_contigs():
    iv = Interval.parse_region("chr7:140,753,336-140,753,336", "GRCh38")
    assert (iv.start, iv.end) == (140_753_335, 140_753_336)
    hla = Interval.parse_region("{HLA-A*01:01:01:01}:1-100", "GRCh38")
    assert hla.contig == "HLA-A*01:01:01:01"
    assert hla.to_region() == "{HLA-A*01:01:01:01}:1-100"
    with pytest.raises(InvalidInputError):
        Interval.parse_region("chr1:0-10", "GRCh38")  # 1-based input cannot start at 0
    with pytest.raises(InvalidInputError):
        Interval.parse_region("chr1", "GRCh38")


def test_overlap_requires_same_assembly():
    a = Interval(contig="chr1", start=0, end=10, assembly="GRCh38")
    assert a.overlaps(Interval(contig="chr1", start=9, end=20, assembly="GRCh38"))
    assert not a.overlaps(Interval(contig="chr1", start=10, end=20, assembly="GRCh38"))
    assert not a.overlaps(Interval(contig="chr1", start=0, end=10, assembly="GRCh37"))


def test_variant_spec_vcf_pos_to_interval():
    # Deletion: VCF POS 100, REF ACG -> 0-based [99, 102)
    v = VariantSpec(assembly="GRCh38", contig="chr1", pos=100, ref="ACG", alt="A")
    assert (v.interval.start, v.interval.end) == (99, 102)
    VariantSpec(assembly="GRCh38", contig="chr1", pos=1, ref="N", alt="<DEL>")
    with pytest.raises(ValidationError):
        VariantSpec(assembly="GRCh38", contig="chr1", pos=0, ref="A", alt="T")
    with pytest.raises(ValidationError):
        VariantSpec(assembly="GRCh38", contig="chr1", pos=1, ref="A", alt="T,C")


def test_file_ref_defaults_private_and_rejects_embedded_credentials():
    f = FileRef(uri="/data/x.bam")
    assert f.visibility is Visibility.PRIVATE
    assert f.is_local and f.effective_format() is FileFormat.BAM
    with pytest.raises(ValidationError):
        FileRef(uri="https://user:pw@example.org/x.bam")
    with pytest.raises(ValidationError):
        FileRef(uri="relative/x.bam")
    with pytest.raises(ValidationError):
        FileRef(uri="gopher://example.org/x.bam")


def test_display_uri_redacts_signed_urls():
    f = FileRef(
        uri="https://bucket.s3.amazonaws.com/a.bam?X-Amz-Credential=AKIAXXXXXXXXXXXXXXXX&X-Amz-Signature=abc"
    )
    assert "AKIA" not in f.display_uri()
    assert "abc" not in f.display_uri()


@pytest.mark.parametrize(
    ("name", "fmt"),
    [
        ("s3://b/x.vcf.gz", FileFormat.VCF),
        ("/d/x.bcf", FileFormat.BCF),
        ("/d/GRCh38.fa.gz", FileFormat.FASTA),
        ("https://h/p/x.bigWig?download=1", FileFormat.BIGWIG),
        ("/d/genes.gff3.gz", FileFormat.GFF3),
        ("/d/reads.fastq.gz", FileFormat.FASTQ),
        ("/d/unknown.dat", None),
    ],
)
def test_infer_format(name, fmt):
    assert infer_format(name) is fmt


def test_all_published_schemas_generate():
    from genomics_mcp.models import Page, Study
    from genomics_mcp.server import SCHEMA_MODELS

    for name, model in SCHEMA_MODELS.items():
        assert model.model_json_schema()["type"] == "object", name
    page = Page[Study](items=[Study(accession="PRJEB1", source="ena")], next_cursor="c2")
    assert page.model_dump()["items"][0]["kind"] == "study"
