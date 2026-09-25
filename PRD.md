# Genomics MCP: product requirements

Status: approved scope; E0–E9 implemented; E10–E11 release prepared. Version 0.1.0 is **not released**. Approved 2026-09-24.
Repository: `rewire-bio/genomics-mcp`. Distribution: `rewire-genomics-mcp` (MIT, Python 3.12). Registry name: `io.github.rewire-bio/genomics-mcp`.

This document replaces `docs/prd-original.md` (kept for history). Reasons for the changes are in `docs/prd-review.md`. Delivery order and epics are in `docs/implementation-plan.md`; module contracts are in `docs/architecture.md`.

## 1. Purpose

An MCP server that lets an agent go from a public or private genomic dataset to bounded, real data at a locus, and then to versioned, source-attributed reference evidence, without writing integration code:

1. Discover an archive study or dataset (EGA, ENA, ENCODE, GEO, NCBI Datasets).
2. Inspect its samples, the phenotype values the archive actually supplies, and its files.
3. Identify files that are downloadable or indexed for region queries.
4. Retrieve bounded data (reads, coverage, pileup, variants, sequence, features, signal) or fetch files as local artifacts.
5. Consult reference sources (HGNC, Ensembl, ClinVar, gnomAD, UniProt, Open Targets, optional AlphaGenome Atlas).

It is a research tool. It does not diagnose, classify variants clinically, call variants, or operate as a hosted service.

## 2. Users

- Computational biologists who want an agent to pull real regions from archive or local files and cite sources.
- Researchers asking natural-language questions about a locus, gene or variant.
- Agent developers who need a small, typed, predictable tool surface.

## 3. Scope

### In scope

- **Archives and catalogues:** EGA (public metadata; authorized file and region retrieval via EGA's services; provider-hosted htsget), ENA (studies, runs, analyses, samples, submitted files, checksums, SRA accessions resolved through ENA), ENCODE and GEO (experiments, samples, processed files), NCBI Datasets (assemblies, sequences, annotation).
- **Reference evidence:** HGNC, Ensembl, ClinVar, gnomAD, UniProt, Open Targets; optional AlphaGenome Atlas precomputed predictions with a user key.
- **Storage:** local files under configured roots; anonymous public HTTPS and S3; explicitly configured personal S3/S3-compatible endpoints (for example MinIO).
- **Formats:** BAM/CRAM with indexes, VCF/BCF with indexes, FASTA with `.fai`, indexed BED/GFF3/GTF, bigWig/bigBed. FASTQ is downloadable but not queryable by locus.
- **Transports:** stdio, and Streamable HTTP with a bearer token, loopback by default.

### Out of scope

Clinical verdicts, ACMG/AMP automation, treatment recommendations, variant calling, arbitrary shell or passthrough tools, a hosted/operated service, COSMIC (licence; deferred), live AlphaGenome inference, silent liftover, population-scale batch annotation, and requirements for an external database. Windows is supported through WSL2 or a container only.

## 4. Tools

All tools return the same envelope (section 6). 23 tools in five groups:

| Group | Tools |
| --- | --- |
| Discovery | `list_sources`, `search_datasets`, `describe_dataset`, `list_files`, `list_samples`, `get_sample_metadata` |
| Transfers | `fetch_file`, `get_transfer_status`, `cancel_transfer` |
| Genomics | `get_reads`, `get_coverage`, `get_pileup`, `get_variants`, `get_sequence`, `get_features`, `get_signal` |
| Composition | `inspect_locus`, `compare_samples` |
| Reference | `resolve_identifier`, `normalize_variant`, `lookup_variant`, `lookup_gene`, `lookup_protein` |

Read-only resources: `genomics://capabilities`, `genomics://status`, `genomics://schemas`, `genomics://schemas/{name}`.

Transfers produce local files; binary data is never embedded in MCP text. A tool whose backend is not implemented in the running build says so in its description and returns `unsupported`.

## 5. Data model and conventions

- Separate entities: Study, Dataset, Sample, File (`FileRef`), Reference (assembly/sequence set). Native accessions are preserved verbatim; relationships are reported only when the source asserts them.
- Phenotype values and linked phenotype files are returned only when the archive supplies them. Nothing is inferred.
- `Interval` = `contig`, `start`, `end`, `assembly`; **0-based half-open**; assembly always explicit. `VariantSpec` uses VCF `pos` (1-based) and converts explicitly.
- No silent liftover. Identifier and variant conversions return a trace, report transcript ambiguity, and check REF against a named reference.
- CRAM decoding requires a reference whose MD5 matches the header, unless the CRAM embeds its reference. Missing, wrong or corrupt indexes are errors, not guesses. Preparation (indexing, reference download) is an explicit, visible step.
- Files are `private` unless marked `public`. Values derived from private files are not sent to external sources without per-call consent.
- ClinVar: individual submissions (SCV), conflicts, and the separate germline / somatic clinical impact / oncogenicity classifications are preserved. gnomAD: dataset version, AC/AN/AF, hemizygous/homozygous counts and filters. Predictions are labelled as predictions. No invented consensus or confidence.

## 6. Result envelope and errors

`ToolResult`: `status` (`ok` | `partial` | `error`), `data`, `error`, per-source `errors`, `source_status`, `provenance`, `warnings`, `truncation`, applied `limits`. Error codes: `not_found`, `unauthorized`, `unsupported`, `invalid_input`, `preparation_required`, `upstream_error`, `timeout`, `budget_exceeded`, `consent_required`, `internal_error`. An unexpected failure is `internal_error`, never an empty success.

## 7. Limits

Defaults: region 1 Mb, 10,000 records, response 1 MiB, interactive deadline 30 s, transfer 100 MiB. Callers may lower limits per call. Larger transfers need an explicit caller budget, up to a configured ceiling, with progress, resume, checksum verification and provenance. Truncation is always reported. Local cache/workspace writes are bounded; source storage is read-only.

## 8. Security

- No ambient cloud credentials: the default boto3/HTSlib chains, `~/.aws`, instance metadata and `AWS_*` variables are never used. Personal credentials are named explicitly per storage profile. Requester-pays is off unless a profile enables it.
- HTTP transport refuses to start without a bearer token of at least 32 characters, and refuses non-loopback binding unless explicitly allowed. All HTTP routes require the token.
- Logs go to stderr, are redacted (tokens, signed URL parameters, configured secrets), and exclude query content by default.
- Per-source rate and concurrency limits, bounded response sizes, deadlines and retries only on safe requests.

## 9. Acceptance criteria

Per epic (see `docs/implementation-plan.md`):

- **E0** Original PRD preserved unchanged; review, this PRD, MIT licence, README with honest status.
- **E1** Package, CLI, config, contracts, errors, limits, auth, resources and capabilities; real stdio and authenticated Streamable HTTP initialize/list/call tests; `uv sync`, `ruff`, `pytest` pass.
- **E2** Storage resolvers for local, HTTPS (range support proven), anonymous S3 and explicit S3 profiles; file/index association and readiness; dummy ambient AWS credentials shown unused.
- **E3** Transfers with budget, progress, resume, cancel, checksum verification; reference preparation; FASTA and indexed BED/GFF/GTF queries.
- **E4** BAM/CRAM/VCF/BCF readers matching samtools/bcftools on golden fixtures (multiallelic, ploidy, deletions, skips, mates, boundaries, truncation); CRAM reference checks (correct, wrong, missing, embedded).
- **E5** bigWig/bigBed local and remote, with remote capability tested.
- **E6** EGA and ENA metadata, phenotype relationships as supplied, real file and bounded region retrieval (EGA public test data only: EGAD00001003338, BAM file EGAF00007243773; never the whole dataset).
- **E7** ENCODE, GEO, NCBI Datasets discovery and retrieval of real artifacts.
- **E8** Reference adapters with provenance, ClinVar/gnomAD semantics above, variant normalization with traces; optional Atlas.
- **E9** `inspect_locus` and `compare_samples` with failure isolation and egress consent.
- **E10** Five dated demonstrations from a clean install: bounded EGA test region; real ENA retrieval; public ENCODE signal; reference sequence plus real ClinVar evidence; local MinIO with no AWS account. Clean package, container and platform checks.
- **E11** Release (PyPI, GHCR, GitHub release, `server.json`) and registry submissions with a status ledger.

Release gates are listed in `docs/implementation-plan.md` and apply unchanged.

## 10. Current factual limitations (2026-09-25)

- All 23 tools are implemented. Version 0.1.0 is not yet published to PyPI, GHCR, a GitHub release or any MCP registry; release status is tracked in `docs/registry-ledger.md`.
- Clean-install demonstrations passed on 2026-09-25 (macOS arm64) against live EGA, ENA, ENCODE, NCBI and ClinVar and a local MinIO: `demos/results/2026-09-25/`. Live sources can change or be unavailable; failures are reported as errors.
- Tested platforms: macOS arm64 (local suite, clean install, demos) and Linux x86_64 (CI suite with `pyBigWig.remote == 1`). The linux/amd64 container and MCPB bundle are exercised by `package.yml`, which had not yet run when this was written. Linux arm64 and Intel macOS are not tested. Windows only through WSL2 or the container.
- Remote bigWig/bigBed needs pyBigWig built with libcurl. The published Linux pyBigWig wheel lacks it, so installs build pyBigWig from source (C compiler, libcurl and zlib headers). There is no compiler-free native install.
- A file's reference/header build label (for example `AS:GRCh38`) is reported as a label, not as proof of a precise assembly accession or patch.
- PulseMCP new submissions are paused; MCP.so requires payment and is outside the approved unpaid launch. PyPI (trusted publisher) and Smithery (namespace) need personal account setup outside this repository.
- COSMIC is deferred. AlphaGenome Atlas is optional, keyed, and verified only offline against the SDK.
- EGA controlled data requires the user's own approved account; development uses the public test account and dataset only.
- gnomAD's public API is rate limited (about 10 requests/minute), so gnomAD-backed lookups are slow by design.
- Remote region reads depend on the server honouring HTTP range requests; files are checked, not assumed.
