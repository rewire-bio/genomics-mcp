"""Pinned truth-set bundles: GIAB HG002 benchmarks and the HPRC release 2 HG002 assembly.

Every file of a bundle is on the bundle's assembly. `file_assembly` is the name the files
themselves declare (GIAB v4.2.1 VCF contig headers say human_GRCh38_no_alt_analysis_set.fasta
or human_hs37d5.fasta); FileRefs carry it so the readers' header check passes. GRCh38 files name contigs chr1..chr22,
GRCh37 (hs37d5) files name them 1..22, and the HPRC assembly uses PanSN names
(HG002#1#chr1). Nothing is lifted over or renamed.

URLs, sizes and digests were checked on 2026-11-17. GIAB publishes no md5 for these files,
so their md5 and sha256 were computed by streaming each pinned URL once; HPRC md5 values are
the publisher's .md5 files. S3 ETags are reported as etag (identity only, not an md5).
"""

from __future__ import annotations

from genomics_mcp.models import (
    AccessStatus,
    Checksum,
    Compression,
    FileFormat,
    FileRef,
    Visibility,
)
from genomics_mcp.synthetic.models import Citation, FileRole, TruthFile, TruthSet

CHECKED = "2026-11-17"

GIAB = "https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/"
HG002_DIR = GIAB + "AshkenazimTrio/HG002_NA24385_son/"
HPRC_S3 = "https://s3-us-west-2.amazonaws.com/human-pangenomics/"
HPRC_INDEX_COMMIT = "2a7e3a5b1ca10454762699176b1a0a1027ccbf21"
HPRC_INDEX = (
    "https://github.com/human-pangenomics/hprc_intermediate_assembly/blob/"
    f"{HPRC_INDEX_COMMIT}/data_tables/assemblies_release2_v1.0.index.csv"
)

# Primary chromosome lengths (UCSC hg38/hg19 chrom.sizes; GRCh37 uses the hs37d5 names).
_GRCH38 = {
    "chr1": 248_956_422, "chr2": 242_193_529, "chr3": 198_295_559, "chr4": 190_214_555,
    "chr5": 181_538_259, "chr6": 170_805_979, "chr7": 159_345_973, "chr8": 145_138_636,
    "chr9": 138_394_717, "chr10": 133_797_422, "chr11": 135_086_622, "chr12": 133_275_309,
    "chr13": 114_364_328, "chr14": 107_043_718, "chr15": 101_991_189, "chr16": 90_338_345,
    "chr17": 83_257_441, "chr18": 80_373_285, "chr19": 58_617_616, "chr20": 64_444_167,
    "chr21": 46_709_983, "chr22": 50_818_468, "chrX": 156_040_895, "chrY": 57_227_415,
}  # fmt: skip
_GRCH37 = {
    "1": 249_250_621, "2": 243_199_373, "3": 198_022_430, "4": 191_154_276,
    "5": 180_915_260, "6": 171_115_067, "7": 159_138_663, "8": 146_364_022,
    "9": 141_213_431, "10": 135_534_747, "11": 135_006_516, "12": 133_851_895,
    "13": 115_169_878, "14": 107_349_540, "15": 102_531_392, "16": 90_354_753,
    "17": 81_195_210, "18": 78_077_248, "19": 59_128_983, "20": 63_025_520,
    "21": 48_129_895, "22": 51_304_566, "X": 155_270_560, "Y": 59_373_566,
}  # fmt: skip
CONTIG_LENGTHS: dict[str, dict[str, int]] = {"GRCh38": _GRCH38, "GRCh37": _GRCH37}
AUTOSOMES = {
    "GRCh38": [f"chr{i}" for i in range(1, 23)],
    "GRCh37": [str(i) for i in range(1, 23)],
}

# ----------------------------------------------------------------------------- citations

WAGNER2022 = Citation(
    key="wagner2022",
    doi="10.1016/j.xgen.2022.100128",
    url="https://doi.org/10.1016/j.xgen.2022.100128",
    authors="Wagner J et al.",
    title="Benchmarking challenging small variants with linked and long reads",
    venue="Cell Genomics 2(5):100128",
    year=2022,
)
ZOOK2020 = Citation(
    key="zook2020",
    doi="10.1038/s41587-020-0538-8",
    url="https://doi.org/10.1038/s41587-020-0538-8",
    authors="Zook JM et al.",
    title="A robust benchmark for detection of germline large deletions and insertions",
    venue="Nature Biotechnology 38:1347-1355",
    year=2020,
)
HANSEN2026 = Citation(
    key="hansen2026",
    doi="10.1016/j.cell.2026.06.016",
    url="https://doi.org/10.1016/j.cell.2026.06.016",
    authors="Hansen NF et al.",
    title="A complete diploid human genome benchmark for personalized genomics",
    venue="Cell 189(16):4857-4875",
    year=2026,
)

# ----------------------------------------------------------------------------- digests

# url -> (size_bytes, md5, sha256, digest note). Filled from one streamed download per file.
_COMPUTED = "md5 and sha256 computed by streaming this URL on 2026-11-17; GIAB publishes none"
_DIGESTS: dict[str, tuple[int, str, str | None, str]] = {
    GIAB + "AshkenazimTrio/HG002_NA24385_son/NIST_SV_v0.6/HG002_SVs_Tier1_v0.6.bed": (
        725_960,
        "1c032ca1187c4fa128b26dc67f4d1984",
        "a9bf43a242e74d403cf78f158dbf5b657b5aa5b4961fbba7218bbfe54072d82c",
        _COMPUTED,
    ),
    GIAB + "AshkenazimTrio/HG002_NA24385_son/NIST_SV_v0.6/HG002_SVs_Tier1_v0.6.vcf.gz": (
        25_738_211,
        "78bceed7edda5c4ae373f5e4c31f72cd",
        "1bc8e515189e32eb02a8a2050a4f70e88a80d9ad1d9fafe65ca1931d6f7b7f13",
        _COMPUTED,
    ),
    GIAB
    + "AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/HG002_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed": (
        11_494_021,
        "97265e922a97c69a0391cf3f92a89b8b",
        "fba9a57c36ec88e5d14ea3e259c8866c7935f4998d3ec0fa2d6c3962da5b5575",
        _COMPUTED,
    ),
    GIAB
    + "AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh37/HG002_GRCh37_1_22_v4.2.1_benchmark_noinconsistent.bed": (
        9_799_769,
        "03de38ff97f02ed5a021419e69eaaa25",
        "e8c5926783b9b2cf0c181891a978959a8591862e444151cf55f4a073352d8115",
        _COMPUTED,
    ),
    GIAB
    + "AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz": (
        156_252_944,
        "dc750b3807d4af1f7ffec852e9c2f771",
        "adb4d4a50048aa13353a06b84fcfcbca09a5d17525efaa4cea44f8822e81175c",
        _COMPUTED,
    ),
    GIAB
    + "AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh37/HG002_GRCh37_1_22_v4.2.1_benchmark.vcf.gz": (
        176_274_136,
        "ca24fb3952107f01eecfe1b222a2e66c",
        "a40525db0ecba79a7e5c28e7765b97a265f3a143fd1141c66b1f5fd7c6092e69",
        _COMPUTED,
    ),
    GIAB + "references/GRCh38/GCA_000001405.15_GRCh38_no_alt_analysis_set.fasta.gz": (
        886_344_618,
        "3a3347eae0893f96ecf495d1c39e2284",
        "3c8def6d325c5d1e934b2dd530c4d1709f027677a15859467071fddf3fff2026",
        _COMPUTED,
    ),
    GIAB + "references/GRCh37/hs37d5.fa.gz": (
        892_326_179,
        "d10eebe06c0dbbcb04253e3294d63efc",
        "e9157e19a95e01dfc47080b5b6aa559c861de90b9934c2ea7c49cd5ec49e0285",
        _COMPUTED,
    ),
}


def _file(
    url: str,
    *,
    provider: str,
    assembly: str,
    fmt: FileFormat,
    accession: str,
    compression: Compression,
    index: str | None = None,
    reference: str | None = None,
    etag: str | None = None,
    published_md5: str | None = None,
    size: int | None = None,
) -> FileRef:
    checksums: list[Checksum] = []
    native: dict[str, str] = {"checked": CHECKED}
    if published_md5:
        checksums.append(Checksum(algorithm="md5", value=published_md5))
        native["md5_source"] = "publisher .md5 file"
    if url in _DIGESTS:
        size, md5, sha256, note = _DIGESTS[url]
        if not published_md5:
            checksums.append(Checksum(algorithm="md5", value=md5))
        if sha256:
            checksums.append(Checksum(algorithm="sha256", value=sha256))
        native["digest_note"] = note
    if etag:
        checksums.append(Checksum(algorithm="etag", value=etag))
    return FileRef(
        uri=url,
        index_uri=index,
        reference_uri=reference,
        format=fmt,
        compression=compression,
        assembly=assembly,
        source=provider,
        accession=accession,
        access_status=AccessStatus.OPEN,
        visibility=Visibility.PUBLIC,
        size_bytes=size,
        checksums=checksums,
        native=native,
    )


def _giab_bundle(
    *,
    id: str,
    title: str,
    release: str,
    assembly: str,
    file_assembly: str,
    vcf: str,
    bed: str,
    reference: str,
    variant_types: list,
    contigs: list[str],
    citations: list[Citation],
    release_url: str,
    notes: list[str],
) -> TruthSet:
    def f(role: FileRole, url: str, fmt: FileFormat, comp: Compression, **kw) -> TruthFile:
        accession = url.removeprefix(GIAB)
        return TruthFile(
            role=role,
            file=_file(
                url,
                provider="giab",
                assembly=file_assembly,
                fmt=fmt,
                accession=accession,
                compression=comp,
                **kw,
            ),
        )

    return TruthSet(
        id=id,
        title=title,
        provider="giab",
        sample="HG002",
        release=release,
        assembly=assembly,
        file_assembly=file_assembly,
        contig_style="chr" if assembly == "GRCh38" else "plain",
        contigs=contigs,
        variant_types=variant_types,
        files=[
            f(
                "benchmark_vcf",
                vcf,
                FileFormat.VCF,
                Compression.BGZF,
                index=vcf + ".tbi",
                reference=reference,
            ),
            f("benchmark_regions", bed, FileFormat.BED, Compression.NONE),
            f(
                "reference_fasta",
                reference,
                FileFormat.FASTA,
                Compression.BGZF,
                index=reference + ".fai",
            ),
        ],
        citations=citations,
        release_url=release_url,
        notes=notes,
    )


REF_GRCH38 = GIAB + "references/GRCh38/GCA_000001405.15_GRCh38_no_alt_analysis_set.fasta.gz"
REF_GRCH37 = GIAB + "references/GRCh37/hs37d5.fa.gz"
_V421 = HG002_DIR + "NISTv4.2.1/"
_SV06 = HG002_DIR + "NIST_SV_v0.6/"

_V421_NOTES = [
    "Autosomes only (1-22). Variants outside the benchmark regions BED are not assessed.",
    "Compare calls with hap.py or vcfeval restricted to the benchmark regions BED.",
]

GIAB_V421_GRCH38 = _giab_bundle(
    id="giab-hg002-v4.2.1-grch38",
    title="GIAB HG002 small-variant benchmark v4.2.1 on GRCh38",
    release="NISTv4.2.1",
    assembly="GRCh38",
    file_assembly="human_GRCh38_no_alt_analysis_set.fasta",
    vcf=_V421 + "GRCh38/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz",
    bed=_V421 + "GRCh38/HG002_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed",
    reference=REF_GRCH38,
    variant_types=["snv", "indel"],
    contigs=AUTOSOMES["GRCh38"],
    citations=[WAGNER2022],
    release_url=_V421 + "GRCh38/",
    notes=_V421_NOTES,
)
GIAB_V421_GRCH37 = _giab_bundle(
    id="giab-hg002-v4.2.1-grch37",
    title="GIAB HG002 small-variant benchmark v4.2.1 on GRCh37 (hs37d5)",
    release="NISTv4.2.1",
    assembly="GRCh37",
    file_assembly="human_hs37d5.fasta",
    vcf=_V421 + "GRCh37/HG002_GRCh37_1_22_v4.2.1_benchmark.vcf.gz",
    bed=_V421 + "GRCh37/HG002_GRCh37_1_22_v4.2.1_benchmark_noinconsistent.bed",
    reference=REF_GRCH37,
    variant_types=["snv", "indel"],
    contigs=AUTOSOMES["GRCh37"],
    citations=[WAGNER2022],
    release_url=_V421 + "GRCh37/",
    notes=_V421_NOTES,
)
GIAB_SV06_GRCH37 = _giab_bundle(
    id="giab-hg002-sv-v0.6-grch37",
    title="GIAB HG002 structural-variant benchmark v0.6 (Tier 1) on GRCh37 (hs37d5)",
    release="NIST_SV_v0.6",
    assembly="GRCh37",
    file_assembly="GRCh37",
    vcf=_SV06 + "HG002_SVs_Tier1_v0.6.vcf.gz",
    bed=_SV06 + "HG002_SVs_Tier1_v0.6.bed",
    reference=REF_GRCH37,
    variant_types=["sv"],
    contigs=[*AUTOSOMES["GRCh37"], "X", "Y"],
    citations=[ZOOK2020],
    release_url=_SV06,
    notes=[
        "Insertions and deletions of 50 bp or more; only PASS records in the VCF are the "
        "Tier 1 benchmark.",
        "GRCh37 only. The release README suggests truvari with --passonly, --includebed and "
        "-r 2000. HG002_SVs_Tier1_noVDJorXorY_v0.6.2.bed is a stricter BED without VDJ, X "
        "and Y.",
    ],
)


def _hprc_haplotype(hap: int, name: str, accession: str, md5: str, etag: str) -> TruthSet:
    url = f"{HPRC_S3}working/HPRC_PLUS/HG002/assemblies/Q100/pansn/{name}.PanSN.fa.gz"
    parent = "paternal" if hap == 1 else "maternal"
    return TruthSet(
        id=f"hprc-hg002-q100-v1.1-{'pat' if hap == 1 else 'mat'}",
        title=f"HG002 Q100 v1.1 {parent} haplotype assembly (HPRC release 2)",
        provider="hprc",
        sample="HG002",
        release="HPRC release 2 (assemblies_release2_v1.0 index)",
        assembly=accession,
        file_assembly=accession,
        contig_style="assembly",
        contigs=None,
        variant_types=[],
        files=[
            TruthFile(
                role="assembly_fasta",
                file=_file(
                    url,
                    provider="hprc",
                    assembly=accession,
                    fmt=FileFormat.FASTA,
                    accession=accession,
                    compression=Compression.BGZF,
                    index=url + ".fai",
                    etag=etag,
                    published_md5=md5,
                    size=_HPRC_SIZES[name],
                ),
            )
        ],
        citations=[HANSEN2026],
        release_url=HPRC_INDEX,
        notes=[
            f"Haplotype {hap} ({parent}) of the T2T/GIAB HG002 Q100 genome, in its own "
            f"coordinates ({name}, GenBank {accession}). It is not GRCh38.",
            f"Contigs use PanSN names, e.g. HG002#{hap}#chr1.",
            "Use it as the true genome to simulate reads from or to assess assemblies. "
            "Variant truth on GRCh37/GRCh38 is in the GIAB bundles.",
        ],
    )


_HPRC_SIZES = {"hg002v1.1.pat": 773_659_256, "hg002v1.1.mat_MT": 804_179_719}

HPRC_HG002_PAT = _hprc_haplotype(
    1,
    "hg002v1.1.pat",
    "GCA_018852605.3",
    md5="6c1e01bd72fa38186b0db46d20595f66",
    etag="1011b9559caf63b1038ad3d7d5eae80b-93",
)
HPRC_HG002_MAT = _hprc_haplotype(
    2,
    "hg002v1.1.mat_MT",
    "GCA_018852615.3",
    md5="5cc5e57aee2c01fbed49cff254792cf6",
    etag="f3180ff5c64c3d751120916bb540322f-96",
)

TRUTH_SETS: dict[str, TruthSet] = {
    t.id: t
    for t in (GIAB_V421_GRCH38, GIAB_V421_GRCH37, GIAB_SV06_GRCH37, HPRC_HG002_PAT, HPRC_HG002_MAT)
}
