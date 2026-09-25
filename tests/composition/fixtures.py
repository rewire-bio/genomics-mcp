"""Tiny real synthetic files (FASTA, BAM, VCF.gz, bigWig) with known values.

Contig "7" of length 1000 on a caller-asserted GRCh38 (synthetic sequence; tests are about
composition mechanics, consent and bookkeeping, not biology of chr7).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import pysam

CONTIG = "7"
LENGTH = 1000
ASSEMBLY = "GRCh38"
READ_LEN = 50


def reference_sequence() -> str:
    rng = random.Random(7)
    return "".join(rng.choice("ACGT") for _ in range(LENGTH))


REF = reference_sequence()


@dataclass
class Fixture:
    root: Path
    fasta: Path
    deep_bam: Path
    shallow_bam: Path
    cohort_vcf: Path
    second_vcf: Path
    bigwig: Path | None
    deep_starts: list[int]
    shallow_starts: list[int]

    def depth(self, which: str, start: int, end: int) -> list[int]:
        starts = self.deep_starts if which == "deep" else self.shallow_starts
        return [sum(1 for s in starts if s <= p < s + READ_LEN) for p in range(start, end)]


def _bam(path: Path, starts: list[int], sample: str) -> None:
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": CONTIG, "LN": LENGTH}],
        "RG": [{"ID": sample, "SM": sample}],
    }
    unsorted = path.with_suffix(".unsorted.bam")
    with pysam.AlignmentFile(str(unsorted), "wb", header=header) as out:
        for i, s in enumerate(sorted(starts)):
            a = pysam.AlignedSegment(out.header)
            a.query_name = f"{sample}_r{i}"
            a.flag = 16 if i % 2 else 0
            a.reference_id = 0
            a.reference_start = s
            a.mapping_quality = 60
            a.cigarstring = f"{READ_LEN}M"
            a.query_sequence = REF[s : s + READ_LEN]
            a.query_qualities = pysam.qualitystring_to_array("I" * READ_LEN)
            a.set_tag("RG", sample)
            out.write(a)
    pysam.sort("-o", str(path), str(unsorted))
    unsorted.unlink()
    pysam.index(str(path))


def _vcf(path: Path, samples: list[str], rows: list[tuple[int, str, str, list[str]]]) -> Path:
    lines = [
        "##fileformat=VCFv4.2",
        f"##contig=<ID={CONTIG},length={LENGTH}>",
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">',
        "\t".join(
            ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", *samples]
        ),
    ]
    for pos, alt, fmt, calls in rows:
        ref = REF[pos - 1]
        lines.append("\t".join([CONTIG, str(pos), ".", ref, alt, "50", "PASS", ".", fmt, *calls]))
    path.write_text("\n".join(lines) + "\n")
    pysam.tabix_index(str(path), preset="vcf", force=True)
    return path.with_name(path.name + ".gz")


def other_base(pos: int, skip: str = "") -> str:
    ref = REF[pos - 1]
    return next(b for b in "ACGT" if b != ref and b not in skip)


COHORT_ROWS = {
    # pos: (S1 GT, S2 GT)
    121: ("0/1", "0/0"),
    131: ("1|1", "./."),
    141: ("1/2", "0|2"),  # multi-allelic
    151: ("1", "0"),  # haploid calls
}
SECOND_ROWS = {121: "1/1", 201: "0/1"}


def build(root: Path) -> Fixture:
    root.mkdir(parents=True, exist_ok=True)
    fasta = root / "ref.fa"
    body = "\n".join(REF[i : i + 60] for i in range(0, LENGTH, 60))
    fasta.write_text(f">{CONTIG}\n{body}\n")
    pysam.faidx(str(fasta))

    deep = [100 + (i % 20) * 5 for i in range(40)]
    shallow = [100, 110, 120, 130]
    _bam(root / "deep.bam", deep, "deep")
    _bam(root / "shallow.bam", shallow, "shallow")

    rows = []
    for pos, (s1, s2) in COHORT_ROWS.items():
        alt = other_base(pos)
        if pos == 141:
            alt = f"{alt},{other_base(pos, alt)}"
        rows.append((pos, alt, "GT:DP", [f"{s1}:10", f"{s2}:12"]))
    cohort = _vcf(root / "cohort.vcf", ["S1", "S2"], rows)
    second = _vcf(
        root / "second.vcf",
        ["S1"],
        [(pos, other_base(pos), "GT", [gt]) for pos, gt in SECOND_ROWS.items()],
    )

    bw_path: Path | None = root / "signal.bw"
    try:
        import pyBigWig

        bw = pyBigWig.open(str(bw_path), "w")
        bw.addHeader([(CONTIG, LENGTH)])
        bw.addEntries([CONTIG, CONTIG], [100, 150], ends=[150, 200], values=[2.0, 4.0])
        bw.close()
    except Exception:
        bw_path = None
    return Fixture(
        root, fasta, root / "deep.bam", root / "shallow.bam", cohort, second, bw_path, deep, shallow
    )


def dense_vcf(root: Path, n: int, *, sample: str = "S1") -> Path:
    rows = [(200 + i * 3, other_base(200 + i * 3), "GT", ["0/1"]) for i in range(n)]
    return _vcf(root / f"dense{n}.vcf", [sample], rows)
