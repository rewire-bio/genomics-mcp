# Synthetic data protocols and truth sets

Unreleased (#69). Package: `genomics_mcp.synthetic`. Tools: `list_synthetic_protocols`, `describe_synthetic_protocol`, `get_truth_set`.

These tools are read-only discovery. They do not run simulators, download files or create data. Running a protocol is the job of a rewirebench recipe; each protocol carries a reserved recipe ID with status `planned` until that recipe exists.

## Protocols

| ID | Method | Truth for | Simulator (pinned) | Main citation |
| --- | --- | --- | --- | --- |
| `bamsurgeon-snv-indel-spike-in` | spike-in | SNV, indel (somatic) | BAMSurgeon 1.4.1 | Ewing et al. 2015, ICGC-TCGA DREAM SMC |
| `seqc2-in-silico-tumour-normal` | read mixture | SNV, indel (somatic) | BAMSurgeon 1.4.1 | Fang et al. 2021; Sahraeian et al. 2022 |
| `wgsim-read-simulation` | read simulation | SNV, indel | wgsim 0.3.1-r13 (commit a12da33) | none published; repository |
| `art-illumina-read-simulation` | read simulation | none (read origin only) | ART MountRainier 2016.06.05 | Huang et al. 2012 |
| `insilicoseq-read-simulation` | read simulation | none (read origin only) | InSilicoSeq 2.0.1 | Gourle et al. 2019 |
| `neat-read-simulation` | read simulation | SNV, indel | NEAT 4.8.1 | Stephens et al. 2016 |
| `survivor-sv-simulation` | SV simulation | SV, SNV | SURVIVOR v1.0.6 | Jeffares et al. 2017 |

Each parameter records its option, the default in the pinned version, any value reported by a cited study, and where it was read. Settings from papers cite the paper that reports them. Usage suggestions that are not published (for example spiking into HG002 inside the GIAB benchmark regions) are marked as such in `caveats`.

## Truth sets

| ID | Files | Assembly | Contigs |
| --- | --- | --- | --- |
| `giab-hg002-v4.2.1-grch38` | benchmark VCF + TBI, benchmark regions BED, GRCh38 no-alt analysis set FASTA + FAI | GRCh38 | chr1-chr22 |
| `giab-hg002-v4.2.1-grch37` | the same on hs37d5 | GRCh37 | 1-22 |
| `giab-hg002-sv-v0.6-grch37` | Tier 1 SV VCF + TBI, Tier 1 BED, hs37d5 | GRCh37 | 1-22, X, Y |
| `hprc-hg002-q100-v1.1-pat` | HG002 Q100 v1.1 paternal haplotype FASTA + FAI (PanSN names) | GCA_018852605.3 | own |
| `hprc-hg002-q100-v1.1-mat` | HG002 Q100 v1.1 maternal haplotype FASTA + FAI (PanSN names) | GCA_018852615.3 | own |

Rules:

- Every file in a bundle has the bundle's assembly. FileRefs carry `file_assembly`, the name the files declare (`human_GRCh38_no_alt_analysis_set.fasta` and `human_hs37d5.fasta` in the v4.2.1 VCF headers), because the readers reject a requested assembly that differs from the header. `assembly` filters accept either name. A request that names a different assembly for a bundle is rejected; nothing is lifted over.
- Contig names are not translated. `contig="20"` on GRCh38 is rejected with a hint to use `chr20`, and the reverse on GRCh37.
- The HPRC assemblies are haplotype genomes in their own coordinates, not GRCh38. Contig lookup is not offered for them.
- URLs are GIAB's NCBI FTP over HTTPS and HPRC's public S3 bucket over HTTPS, read anonymously.

Digests: GIAB publishes no md5 for these files, so md5 and sha256 were computed by streaming each pinned URL once on 2026-11-17 (`native.digest_note` says so). HPRC md5 values are the publisher's `.md5` files. S3 ETags are reported with algorithm `etag`; multipart ETags are identity values, not md5. `fetch_file` verifies md5 and sha256.

## Example

An agent asked for "SNV and indel spike-in on HG002 chr20":

1. `list_synthetic_protocols(variant_type="snv", method="spike_in")` returns `bamsurgeon-snv-indel-spike-in`.
2. `describe_synthetic_protocol(protocol_id="bamsurgeon-snv-indel-spike-in")` returns citations, parameters, linked truth sets and recipe `rewirebench:synthetic/bamsurgeon-snv-indel-spike-in` (planned).
3. `get_truth_set(sample="HG002", assembly="GRCh38", variant_type="snv", contig="chr20")` returns the v4.2.1 GRCh38 bundle and the interval chr20:0-64444167 on `human_GRCh38_no_alt_analysis_set.fasta`. Its benchmark VCF FileRef works directly with `get_variants` for a bounded interval.

## Checks

- `tests/synthetic/test_synthetic.py`: catalogue integrity, filters, build and contig mismatches, and the example above over an in-memory MCP client. Offline.
- `tests/synthetic/test_synthetic_live.py`: HEAD request for every catalogue URL and a bounded `get_variants` on chr20 from the GIAB VCF. Opt in with `GENOMICS_MCP_LIVE=1`.
