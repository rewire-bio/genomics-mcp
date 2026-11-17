"""Curated catalogue of published synthetic-data protocols.

Each parameter cites where it was read: the pinned simulator source or documentation, or the
paper that reports the setting. Defaults are those of the pinned version, not of later releases.
Checked 2026-11-17.
"""

from __future__ import annotations

from genomics_mcp.synthetic.models import Citation, Parameter, Protocol, Recipe, Simulator

# ----------------------------------------------------------------------------- citations

EWING2015 = Citation(
    key="ewing2015",
    doi="10.1038/nmeth.3407",
    url="https://doi.org/10.1038/nmeth.3407",
    authors="Ewing AD et al.",
    title="Combining tumor genome simulation with crowdsourcing to benchmark somatic "
    "single-nucleotide-variant detection",
    venue="Nature Methods 12:623-630",
    year=2015,
)
FANG2021 = Citation(
    key="fang2021",
    doi="10.1038/s41587-021-00993-6",
    url="https://doi.org/10.1038/s41587-021-00993-6",
    authors="Fang LT et al.",
    title="Establishing community reference samples, data and call sets for benchmarking "
    "cancer mutation detection using whole-genome sequencing",
    venue="Nature Biotechnology 39:1151-1160",
    year=2021,
)
XIAO2021 = Citation(
    key="xiao2021",
    doi="10.1038/s41587-021-00994-5",
    url="https://doi.org/10.1038/s41587-021-00994-5",
    authors="Xiao W et al.",
    title="Toward best practice in cancer mutation detection with whole-genome and "
    "whole-exome sequencing",
    venue="Nature Biotechnology 39:1141-1150",
    year=2021,
)
SAHRAEIAN2022 = Citation(
    key="sahraeian2022",
    doi="10.1186/s13059-021-02592-9",
    url="https://doi.org/10.1186/s13059-021-02592-9",
    authors="Sahraeian SME et al.",
    title="Achieving robust somatic mutation detection with deep learning models derived "
    "from reference data sets of a cancer sample",
    venue="Genome Biology 23:12",
    year=2022,
)
WGSIM_REPO = Citation(
    key="wgsim",
    doi=None,
    url="https://github.com/lh3/wgsim",
    authors="Li H",
    title="wgsim: reads simulator (README and source; no dedicated publication)",
    venue="GitHub lh3/wgsim",
    year=2011,
)
HUANG2012 = Citation(
    key="huang2012",
    doi="10.1093/bioinformatics/btr708",
    url="https://doi.org/10.1093/bioinformatics/btr708",
    authors="Huang W et al.",
    title="ART: a next-generation sequencing read simulator",
    venue="Bioinformatics 28(4):593-594",
    year=2012,
)
GOURLE2019 = Citation(
    key="gourle2019",
    doi="10.1093/bioinformatics/bty630",
    url="https://doi.org/10.1093/bioinformatics/bty630",
    authors="Gourle H et al.",
    title="Simulating Illumina metagenomic data with InSilicoSeq",
    venue="Bioinformatics 35(3):521-522",
    year=2019,
)
STEPHENS2016 = Citation(
    key="stephens2016",
    doi="10.1371/journal.pone.0167047",
    url="https://doi.org/10.1371/journal.pone.0167047",
    authors="Stephens ZD et al.",
    title="Simulating Next-Generation Sequencing Datasets from Empirical Mutation and "
    "Sequencing Models",
    venue="PLOS ONE 11(11):e0167047",
    year=2016,
)
JEFFARES2017 = Citation(
    key="jeffares2017",
    doi="10.1038/ncomms14061",
    url="https://doi.org/10.1038/ncomms14061",
    authors="Jeffares DC et al.",
    title="Transient structural variations have strong effects on quantitative traits and "
    "reproductive isolation in fission yeast",
    venue="Nature Communications 8:14061",
    year=2017,
)

# ----------------------------------------------------------------------------- simulators

BAMSURGEON = Simulator(
    name="BAMSurgeon",
    version="1.4.1",
    homepage="https://github.com/adamewing/bamsurgeon",
    source_url="https://github.com/adamewing/bamsurgeon/blob/1.4.1/bin/addsnv.py",
    license="MIT",
    install="bioconda::bamsurgeon=1.4.1",
)
_BS_SNV = "https://github.com/adamewing/bamsurgeon/blob/1.4.1/bin/addsnv.py"
_BS_INDEL = "https://github.com/adamewing/bamsurgeon/blob/1.4.1/bin/addindel.py"
_BS_MANUAL = "https://github.com/adamewing/bamsurgeon/blob/1.4.1/doc/Manual.tex"

WGSIM_COMMIT = "a12da3375ff3b51a5594d4b6fa35591173ecc229"
_WGSIM_SRC = f"https://github.com/lh3/wgsim/blob/{WGSIM_COMMIT}/wgsim.c"

_ART_HOME = "https://www.niehs.nih.gov/research/resources/software/biostatistics/art"
_ISS_APP = "https://github.com/HadrienG/InSilicoSeq/blob/2.0.1/iss/app.py"
_NEAT_README = "https://github.com/ncsa/NEAT/blob/4.8.1/README.md"
_SURVIVOR_SRC = (
    "https://github.com/fritzsedlazeck/SURVIVOR/blob/v1.0.6/src/simulator/SV_Simulator.cpp"
)
_SURVIVOR_MAIN = "https://github.com/fritzsedlazeck/SURVIVOR/blob/v1.0.6/src/SURVIVOR.cpp"
_SURVIVOR_WIKI = "https://github.com/fritzsedlazeck/SURVIVOR/wiki"

_GERMLINE_GRCH38 = "giab-hg002-v4.2.1-grch38"
_GERMLINE_GRCH37 = "giab-hg002-v4.2.1-grch37"


def _recipe(protocol_id: str) -> Recipe:
    return Recipe(id=f"rewirebench:synthetic/{protocol_id}")


_BS_COMMON = [
    Parameter(
        name="bam",
        option="-f/--bamfile",
        meaning="Input SAM/BAM to mutate. The unmodified input (or a separate replicate) is "
        "the matched normal.",
        source=_BS_SNV,
    ),
    Parameter(
        name="reference",
        option="-r/--reference",
        meaning="Reference FASTA indexed with both bwa index and samtools faidx.",
        source=_BS_SNV,
    ),
    Parameter(
        name="aligner",
        option="--aligner",
        default="backtrack",
        meaning="Aligner used to realign the altered reads.",
        source=_BS_SNV,
    ),
    Parameter(
        name="min_depth",
        option="--mindepth",
        default="10",
        meaning="Minimum read depth at a site for a mutation to be made.",
        source=_BS_SNV,
    ),
    Parameter(
        name="max_depth",
        option="--maxdepth",
        default="2000",
        meaning="Maximum read depth at a site for a mutation to be made.",
        source=_BS_SNV,
    ),
    Parameter(
        name="min_mutated_reads",
        option="--minmutreads",
        default="3",
        meaning="Minimum number of mutated reads to output per site.",
        source=_BS_SNV,
    ),
    Parameter(
        name="procs",
        option="-p/--procs",
        default="1",
        meaning="Number of parallel processes.",
        source=_BS_SNV,
    ),
    Parameter(
        name="seed",
        option="--seed",
        default="none",
        meaning="Random seed. Set it for reproducible read selection.",
        source=_BS_SNV,
    ),
    Parameter(
        name="cnv_file",
        option="-c/--cnvfile",
        default="none",
        meaning="Tabix-indexed absolute copy number; adjusts the achieved allele fraction.",
        source=_BS_SNV,
    ),
]

BAMSURGEON_SPIKE_IN = Protocol(
    id="bamsurgeon-snv-indel-spike-in",
    title="BAMSurgeon SNV and indel spike-in (ICGC-TCGA DREAM SMC)",
    method="spike_in",
    setting="somatic",
    variant_types=["snv", "indel"],
    input="An aligned, high-coverage BAM of a normal sample and its reference FASTA.",
    output="A mutated BAM (synthetic tumour) and a VCF of the spiked-in variants with their "
    "expected allele fractions. The unmodified BAM is the matched normal.",
    summary="Adds SNVs (addsnv.py) and indels (addindel.py) at chosen allele fractions to "
    "existing reads, then realigns the altered reads. Used to build the in silico tumours of "
    "the ICGC-TCGA DREAM Somatic Mutation Calling Challenge.",
    simulator=BAMSURGEON,
    citations=[EWING2015, SAHRAEIAN2022],
    parameters=[
        Parameter(
            name="snv_targets",
            option="addsnv.py -v/--varfile",
            meaning="Target sites, one per line: chromosome, start, end, optional VAF.",
            source=_BS_MANUAL,
        ),
        Parameter(
            name="indel_targets",
            option="addindel.py -v/--varfile",
            meaning="chromosome, start, end, VAF (required), INS or DEL, and the inserted "
            "sequence for INS.",
            source=_BS_MANUAL,
        ),
        Parameter(
            name="allele_fraction",
            option="-m/--mutfrac",
            default="0.5",
            meaning="Allele fraction when the target line gives none.",
            source=_BS_SNV,
        ),
        Parameter(
            name="snv_coverage_ratio",
            option="addsnv.py -d/--coverdiff",
            default="0.9",
            meaning="Allowed output/input coverage difference after realignment, SNVs.",
            source=_BS_SNV,
        ),
        Parameter(
            name="indel_coverage_ratio",
            option="addindel.py -d/--coverdiff",
            default="0.1",
            meaning="Allowed output/input coverage difference after realignment, indels.",
            source=_BS_INDEL,
        ),
        Parameter(
            name="max_mutations",
            option="-n/--numsnvs",
            default="0 (all targets)",
            meaning="Maximum number of mutations to try.",
            source=_BS_SNV,
        ),
        *_BS_COMMON,
    ],
    published_settings=[
        "IS1: 3,537 somatic SNVs, 100% tumour cellularity, no subclonality; background "
        "HCC1143 BL; about 450 million 2x101 bp read pairs per sample (ewing2015).",
        "IS3: 7,903 somatic SNVs in three subpopulations at different allele fractions "
        "(ewing2015).",
        "DREAM stage 3: 7,903 SNVs and 7,604 indels spiked at allele fractions of 50%, 33% "
        "and 20% (sahraeian2022, describing the challenge data).",
    ],
    truth_sets=[_GERMLINE_GRCH38, _GERMLINE_GRCH37],
    recipe=_recipe("bamsurgeon-snv-indel-spike-in"),
    caveats=[
        "Spike-ins inherit the error profile of the input reads but not tumour biology "
        "(copy number, clonal structure) unless supplied with --cnvfile and allele fractions.",
        "Not every target is made: sites failing depth, nearby-SNP or coverage checks are "
        "skipped. Use the output VCF, not the input targets, as truth.",
        "With a GIAB sample as background, restrict targets to the benchmark regions BED and "
        "away from the sample's own benchmark variants so germline and spiked truth do not "
        "overlap. This is a usage suggestion, not a published protocol.",
    ],
)

SEQC2_IN_SILICO = Protocol(
    id="seqc2-in-silico-tumour-normal",
    title="SEQC2 in silico tumour-normal pairs and read mixtures (HCC1395/HCC1395BL)",
    method="read_mixture",
    setting="somatic",
    variant_types=["snv", "indel"],
    input="Two or more sequencing replicates of the same normal sample (SEQC2 used "
    "HCC1395BL), plus tumour replicates for purity mixtures.",
    output="In silico tumour-normal pairs with known spiked-in somatic variants; tumour or "
    "normal BAMs at reduced purity made by mixing reads.",
    summary="Spike synthetic SNVs and indels into one normal replicate with BAMSurgeon and pair "
    "it with a different normal replicate, so only spiked variants are true somatic calls. "
    "Purity is varied by mixing reads from tumour and normal alignments.",
    simulator=BAMSURGEON,
    citations=[FANG2021, SAHRAEIAN2022, XIAO2021],
    parameters=[
        Parameter(
            name="spiked_snvs",
            published_value="about 100,000 (fang2021); about 92,000 per WGS pair (sahraeian2022)",
            meaning="SNVs spiked into the pseudo-tumour replicate.",
            source="fang2021",
        ),
        Parameter(
            name="spiked_indels",
            published_value="about 20,000 (fang2021); about 22,000 per WGS pair (sahraeian2022)",
            meaning="Indels spiked into the pseudo-tumour replicate.",
            source="fang2021",
        ),
        Parameter(
            name="allele_fraction_distribution",
            published_value="Beta(2, 5); Beta(2, 18) for the 10% purity sample",
            meaning="Distribution the spike-in allele fractions are drawn from.",
            source="sahraeian2022",
        ),
        Parameter(
            name="impure_normal",
            published_value="95% normal reads + 5% tumour reads",
            meaning="Normal contaminated with tumour, made by mixing reads.",
            source="sahraeian2022",
        ),
        Parameter(
            name="low_purity_tumour",
            published_value="10% tumour reads + 90% normal reads",
            meaning="Low-purity tumour, made by mixing reads.",
            source="sahraeian2022",
        ),
        *_BS_COMMON,
    ],
    published_settings=[
        "Training sets A and B: normal replicate 2 (or 3) spiked as in silico tumour versus "
        "normal replicate 1 (or 2), with different mutations in each set (fang2021).",
        "Ten WGS in silico pairs from replicates at 40x-95x, two merged to about 220x tumour "
        "and 170x normal (sahraeian2022).",
        "Wet-lab purity series for comparison, not in silico: tumour:normal DNA at 1:0, 3:1, "
        "1:1, 1:4, 1:9, 1:19 and 0:1 (xiao2021).",
    ],
    truth_sets=[],
    recipe=_recipe("seqc2-in-silico-tumour-normal"),
    caveats=[
        "Needs two independent normal replicates; splitting one BAM in two halves its depth.",
        "Read mixing changes depth and purity together; downsample to hold depth fixed.",
        "The SEQC2 HCC1395 somatic reference call set is not in get_truth_set yet.",
    ],
)

WGSIM = Protocol(
    id="wgsim-read-simulation",
    title="wgsim short-read simulation with random SNVs and indels",
    method="read_simulation",
    setting="germline",
    variant_types=["snv", "indel"],
    input="A reference FASTA.",
    output="Paired FASTQ files. Simulated polymorphisms are written to standard output, and "
    "each read name records its true origin and error counts.",
    summary="Simulates a diploid genome with random SNPs and indels from a reference and samples "
    "paired reads with uniform substitution errors.",
    simulator=Simulator(
        name="wgsim",
        version=f"0.3.1-r13 (commit {WGSIM_COMMIT[:7]})",
        homepage="https://github.com/lh3/wgsim",
        source_url=_WGSIM_SRC,
        license="MIT",
        install="bioconda::wgsim",
    ),
    citations=[WGSIM_REPO],
    parameters=[
        Parameter(
            name="base_error_rate",
            option="-e",
            default="0.020",
            meaning="Base error rate.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="outer_distance",
            option="-d",
            default="500",
            meaning="Outer distance between the two ends.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="distance_sd",
            option="-s",
            default="50",
            meaning="Standard deviation of the outer distance.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="read_pairs",
            option="-N",
            default="1000000",
            meaning="Number of read pairs.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="read1_length",
            option="-1",
            default="70",
            meaning="Length of read 1.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="read2_length",
            option="-2",
            default="70",
            meaning="Length of read 2.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="mutation_rate",
            option="-r",
            default="0.0010",
            meaning="Rate of simulated polymorphisms.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="indel_fraction",
            option="-R",
            default="0.15",
            meaning="Fraction of polymorphisms that are indels.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="indel_extension",
            option="-X",
            default="0.30",
            meaning="Probability an indel is extended.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="seed",
            option="-S",
            default="-1 (time-based)",
            meaning="Random seed.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="max_ambiguous",
            option="-A",
            default="0.05",
            meaning="Discard reads with a higher fraction of ambiguous bases.",
            source=_WGSIM_SRC,
        ),
        Parameter(
            name="haplotype_mode",
            option="-h",
            default="off",
            meaning="Haplotype mode.",
            source=_WGSIM_SRC,
        ),
    ],
    truth_sets=[],
    recipe=_recipe("wgsim-read-simulation"),
    caveats=[
        "No indel sequencing errors and no quality model: reads are cleaner than real data.",
        "Polymorphisms are random, not drawn from a population or a GIAB sample.",
        "The repository has no tags; bioconda labels its package 1.0 while the source "
        "reports 0.3.1-r13.",
    ],
)

ART = Protocol(
    id="art-illumina-read-simulation",
    title="ART Illumina read simulation",
    method="read_simulation",
    setting=None,
    variant_types=[],
    input="A FASTA to sample reads from: a reference, or a genome already carrying known "
    "variants (for example a SURVIVOR or NEAT output).",
    output="FASTQ reads with empirical Illumina error profiles; with -sam, a SAM file of each "
    "read's true alignment.",
    summary="Samples reads with platform-specific, empirically trained error and quality "
    "models. It does not add variants itself; truth is the input genome and read origin.",
    simulator=Simulator(
        name="ART (art_illumina)",
        version="MountRainier 2016.06.05",
        homepage=_ART_HOME,
        source_url=_ART_HOME,
        license=None,
        install="bioconda::art=2016.06.05",
    ),
    citations=[HUANG2012],
    parameters=[
        Parameter(name="input", option="-i", meaning="Input sequence FASTA.", source=_ART_HOME),
        Parameter(name="read_length", option="-l", meaning="Read length.", source=_ART_HOME),
        Parameter(name="fold_coverage", option="-f", meaning="Fold coverage.", source=_ART_HOME),
        Parameter(
            name="output_prefix", option="-o", meaning="Output file prefix.", source=_ART_HOME
        ),
        Parameter(
            name="paired", option="-p", meaning="Simulate paired-end reads.", source=_ART_HOME
        ),
        Parameter(
            name="fragment_mean",
            option="-m",
            meaning="Mean fragment length (paired).",
            source=_ART_HOME,
        ),
        Parameter(
            name="fragment_sd",
            option="-s",
            meaning="Fragment length standard deviation (paired).",
            source=_ART_HOME,
        ),
        Parameter(
            name="sam_output",
            option="-sam",
            meaning="Also write true alignments as SAM.",
            source=_ART_HOME,
        ),
    ],
    truth_sets=[],
    recipe=_recipe("art-illumina-read-simulation"),
    caveats=[
        "Last release 2016-06-05; check that a built-in profile matches the instrument "
        "being modelled."
    ],
)

INSILICOSEQ = Protocol(
    id="insilicoseq-read-simulation",
    title="InSilicoSeq Illumina read simulation",
    method="read_simulation",
    setting=None,
    variant_types=[],
    input="One or more genome FASTA files (metagenome or a single genome).",
    output="Paired FASTQ files and an abundance table.",
    summary="Generates Illumina reads with kernel-density error models trained on MiSeq, HiSeq, "
    "NextSeq or NovaSeq runs. It does not add variants; truth is the input genomes and their "
    "abundances.",
    simulator=Simulator(
        name="InSilicoSeq (iss generate)",
        version="2.0.1",
        homepage="https://github.com/HadrienG/InSilicoSeq",
        source_url=_ISS_APP,
        license="MIT",
        install="bioconda::insilicoseq=2.0.1",
    ),
    citations=[GOURLE2019],
    parameters=[
        Parameter(
            name="genomes", option="--genomes/-g", meaning="Input genome FASTA(s).", source=_ISS_APP
        ),
        Parameter(
            name="n_reads",
            option="--n_reads/-n",
            default="1000000",
            meaning="Number of reads; accepts k/M/G suffixes.",
            source=_ISS_APP,
        ),
        Parameter(
            name="mode",
            option="--mode/-e",
            default="kde",
            meaning="Error model: kde, basic or perfect.",
            source=_ISS_APP,
        ),
        Parameter(
            name="model",
            option="--model/-m",
            default="none",
            meaning="Pre-computed "
            "model (HiSeq, NextSeq, NovaSeq, MiSeq, MiSeq-20/24/28/32) or a .npz from iss "
            "model.",
            source=_ISS_APP,
        ),
        Parameter(
            name="gc_bias",
            option="--gc_bias/-c",
            default="off",
            meaning="Drop reads with abnormal GC content.",
            source=_ISS_APP,
        ),
        Parameter(
            name="sequence_type",
            option="--sequence_type/-t",
            default="metagenomics",
            meaning="metagenomics or amplicon.",
            source=_ISS_APP,
        ),
        Parameter(
            name="seed",
            option="--seed",
            default="none",
            meaning="Seed for all random number generators.",
            source=_ISS_APP,
        ),
        Parameter(
            name="cpus", option="--cpus/-p", default="2", meaning="Number of CPUs.", source=_ISS_APP
        ),
    ],
    truth_sets=[],
    recipe=_recipe("insilicoseq-read-simulation"),
    caveats=[
        "Designed for metagenomes; for human variant benchmarks pair it with a genome "
        "that already carries known variants."
    ],
)

NEAT = Protocol(
    id="neat-read-simulation",
    title="NEAT read simulation with a golden variant VCF",
    method="read_simulation",
    setting="germline",
    variant_types=["snv", "indel"],
    input="A reference FASTA and a YAML config; optionally a VCF of variants to include.",
    output="FASTQ, and on request a golden BAM and a golden VCF of every inserted variant.",
    summary="Simulates reads with empirical mutation, error, fragment-length and GC models, "
    "inserting random variants and any supplied VCF variants, and writes the true variants.",
    simulator=Simulator(
        name="NEAT (neat read-simulator)",
        version="4.8.1",
        homepage="https://github.com/ncsa/NEAT",
        source_url=_NEAT_README,
        license="BSD-3-Clause (LICENSE.md)",
        install="bioconda::neat=4.8.1",
    ),
    citations=[STEPHENS2016],
    parameters=[
        Parameter(
            name="reference", option="reference", meaning="Reference FASTA.", source=_NEAT_README
        ),
        Parameter(
            name="read_length",
            option="read_len",
            default="151",
            meaning="Read length.",
            source=_NEAT_README,
        ),
        Parameter(
            name="coverage",
            option="coverage",
            default="10",
            meaning="Mean coverage.",
            source=_NEAT_README,
        ),
        Parameter(
            name="ploidy",
            option="ploidy",
            default="2",
            meaning="Copies of each chromosome.",
            source=_NEAT_README,
        ),
        Parameter(
            name="paired",
            option="paired_ended",
            default="False",
            meaning="Paired-end "
            "reads; needs fragment_mean and fragment_st_dev or a fragment model.",
            source=_NEAT_README,
        ),
        Parameter(
            name="fragment_mean",
            option="fragment_mean",
            meaning="Mean fragment length.",
            source=_NEAT_README,
        ),
        Parameter(
            name="fragment_sd",
            option="fragment_st_dev",
            meaning="Fragment length standard deviation.",
            source=_NEAT_README,
        ),
        Parameter(
            name="golden_vcf",
            option="produce_vcf",
            default="False",
            meaning="Write the VCF of true variants.",
            source=_NEAT_README,
        ),
        Parameter(
            name="golden_bam",
            option="produce_bam",
            default="False",
            meaning="Write the BAM of true alignments.",
            source=_NEAT_README,
        ),
        Parameter(
            name="include_vcf",
            option="include_vcf",
            meaning="VCF of variants to insert as given.",
            source=_NEAT_README,
        ),
        Parameter(
            name="target_bed",
            option="target_bed",
            meaning="Regions to simulate; coverage is 0 elsewhere.",
            source=_NEAT_README,
        ),
        Parameter(
            name="mutation_rate",
            option="mutation_rate",
            default="from mutation model",
            meaning="Random mutation rate, 0 to 0.3.",
            source=_NEAT_README,
        ),
        Parameter(name="seed", option="rng_seed", meaning="Random seed.", source=_NEAT_README),
    ],
    truth_sets=[_GERMLINE_GRCH38, _GERMLINE_GRCH37],
    recipe=_recipe("neat-read-simulation"),
    caveats=[
        "Passing a GIAB benchmark VCF as include_vcf with target_bed set to its benchmark "
        "regions gives reads whose truth is that VCF inside those regions. This is a usage "
        "suggestion, not a published protocol.",
    ],
)

SURVIVOR = Protocol(
    id="survivor-sv-simulation",
    title="SURVIVOR structural variant simulation",
    method="sv_simulation",
    setting="germline",
    variant_types=["sv", "snv"],
    input="A reference FASTA and a SURVIVOR parameter file.",
    output="A modified FASTA, a BED and a VCF of simulated SVs, and the inserted sequences. "
    "Reads are then simulated from the modified FASTA with any read simulator.",
    summary="Inserts deletions, insertions, duplications, inversions and translocations (and "
    "optionally SNPs) of configured sizes and counts into a reference.",
    simulator=Simulator(
        name="SURVIVOR (simSV)",
        version="v1.0.6",
        homepage="https://github.com/fritzsedlazeck/SURVIVOR",
        source_url=_SURVIVOR_SRC,
        license="MIT",
        install="bioconda::survivor (1.0.7 packaged; parameters read from v1.0.6 source)",
    ),
    citations=[JEFFARES2017],
    parameters=[
        Parameter(
            name="parameter_template",
            option="SURVIVOR simSV <parameter_file>",
            meaning="Writes a template parameter file. Keep its line order.",
            source=_SURVIVOR_WIKI,
        ),
        Parameter(
            name="snp_frequency",
            option="simSV positional 3",
            meaning="SNP mutation frequency, 0 to 1.",
            source=_SURVIVOR_MAIN,
        ),
        Parameter(
            name="coordinate_mode",
            option="simSV positional 4",
            meaning="0: simulate reads from the modified genome; 1: map real reads to it.",
            source=_SURVIVOR_MAIN,
        ),
        Parameter(
            name="duplications",
            option="DUPLICATION_minimum_length / _maximum_length / _number",
            default="100 / 10000 / 3",
            meaning="Duplication sizes and count.",
            source=_SURVIVOR_SRC,
        ),
        Parameter(
            name="indels",
            option="INDEL_minimum_length / _maximum_length / _number",
            default="20 / 500 / 1",
            meaning="Insertion/deletion sizes and count.",
            source=_SURVIVOR_SRC,
        ),
        Parameter(
            name="translocations",
            option="TRANSLOCATION_minimum_length / _maximum_length / _number",
            default="1000 / 3000 / 2",
            meaning="Translocation sizes and count.",
            source=_SURVIVOR_SRC,
        ),
        Parameter(
            name="inversions",
            option="INVERSION_minimum_length / _maximum_length / _number",
            default="600 / 800 / 4",
            meaning="Inversion sizes and count.",
            source=_SURVIVOR_SRC,
        ),
        Parameter(
            name="inverted_deletions",
            option="INV_del_minimum_length / _maximum_length / _number",
            default="600 / 800 / 2",
            meaning="Inversion-deletion sizes and count.",
            source=_SURVIVOR_SRC,
        ),
        Parameter(
            name="inverted_duplications",
            option="INV_dup_minimum_length / _maximum_length / _number",
            default="600 / 800 / 2",
            meaning="Inversion-duplication sizes and count.",
            source=_SURVIVOR_SRC,
        ),
        Parameter(
            name="evaluation",
            option="SURVIVOR eval <calls.vcf> <simulated.bed> <max_distance> <prefix>",
            meaning="Scores a caller against the simulated "
            "SVs, allowing max_distance bp of breakpoint wobble.",
            source=_SURVIVOR_WIKI,
        ),
    ],
    truth_sets=["giab-hg002-sv-v0.6-grch37"],
    recipe=_recipe("survivor-sv-simulation"),
    caveats=[
        "SV positions are uniform random; they do not follow real SV size or repeat distributions.",
        "Translocations need at least two chromosomes in the reference.",
    ],
)

PROTOCOLS: dict[str, Protocol] = {
    p.id: p for p in (BAMSURGEON_SPIKE_IN, SEQC2_IN_SILICO, WGSIM, ART, INSILICOSEQ, NEAT, SURVIVOR)
}
