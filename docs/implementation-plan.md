# Approved implementation plan

Approved 2026-09-24. Repository: rewire-bio/genomics-mcp. Distribution: rewire-genomics-mcp 0.1.0, MIT, Python 3.12. This is a unified genomic data and reference MCP, independent of the Rewire benchmark database. Benchmark generation is a downstream use case, not the product's tool model.

## Product path

Discover an archive study/dataset, inspect actual sample and available phenotype metadata, identify downloadable or indexed files, retrieve bounded genomic data or local artifacts, and consult versioned public reference evidence. Support direct local/HTTPS/S3 files without requiring users to deploy htsget. Provider-hosted EGA htsget is supported.

## Sources and formats

- EGA first-class: studies, datasets, samples, phenotypes, files and access policies from public metadata; authorized real file and regional retrieval through existing services. Test only documented public EGA data/account: EGAD00001003338, candidate file EGAF00001775036. Never fetch the entire approximately 1 TB dataset.
- ENA: public studies/runs/analyses, available samples and submitted files, checksums/indexes. Resolve compatible SRA accessions through ENA. FASTQ is downloadable but not locus-ready.
- ENCODE and GEO: experiments/samples/processed files, actual downloadable artifacts and region queries for supported formats.
- NCBI Datasets and Ensembl: versioned assemblies, sequences and annotations, explicit local preparation/indexing when needed.
- References: HGNC, Ensembl, ClinVar, gnomAD, UniProt and Open Targets. Optional explicitly keyed AlphaGenome Atlas precomputed predictions; no implicit live inference. COSMIC deferred due entitlement/licensing.
- Local storage, anonymous public HTTPS/S3, and explicitly configured personal S3/S3-compatible endpoints. No ambient cloud credentials, no work AWS, no requester-pays by default.
- BAM/CRAM + indexes, VCF/BCF + indexes, FASTA + indexes, indexed BED/GFF/GTF, bigWig/bigBed. Remote readers must prove range support. Ordinary gzip is not BGZF.

## Tool contract

Discovery: list_sources, search_datasets, describe_dataset, list_files, list_samples, get_sample_metadata.

Transfers: fetch_file, get_transfer_status, cancel_transfer. Files become local artifacts, never binary blobs in MCP text.

Genomics: get_reads, get_coverage, get_pileup, get_variants, get_sequence, get_features, get_signal.

Composition: inspect_locus, compare_samples.

Reference: resolve_identifier, normalize_variant, lookup_variant, lookup_gene, lookup_protein.

Read-only MCP resources expose capabilities, source status and JSON schemas. Use stable typed envelopes with distinct unauthorized/missing/unsupported/upstream failures and explicit truncation.

## Shared correctness

- Separate Study, Dataset, Sample, File and Reference entities, preserve native accessions and documented relationships. Return phenotype values and linked phenotype files only when actually supplied by the archive.
- Explicit assembly/version and 0-based half-open intervals. Conversion traces, reference validation, transcript ambiguity; no silent liftover.
- CRAM requires exact reference checksum matching unless self-contained. Validate missing/wrong/corrupt indexes. Preparation is visible, never guessed.
- Default region 1 Mb, 10,000 records, response 1 MiB, interactive deadline 30 s, transfer 100 MiB. Budgets and truncation are visible. Larger downloads require explicit caller budget, progress, resume, checksums and provenance.
- Preserve ClinVar individual assertions/conflicts and germline/somatic/oncogenicity distinctions; gnomAD denominators and prediction provenance. No invented consensus/confidence.
- No annotation egress derived from private files without explicit per-call permission. Per-source timeout/rate limits, partial-result isolation, conservative cache policy, redacted logs.
- Source data is read-only. No public service, external database requirement, arbitrary shell tool, clinical verdict or variant caller.

## Epics and dependencies

| Epic | Deliverable | Dependencies |
| --- | --- | --- |
| E0 | Preserve original PRD; critical review and enhanced PRD; MIT repo, AGENTS, plan | none |
| E1 | Core MCP/CLI/config/contracts/errors/limits/auth/resources/capabilities | E0 |
| E2 | Storage resolvers/discovery/explicit credentials/file-index associations/readiness | E1 |
| E3 | Transfers/resume/artifacts/references/FASTA/indexed annotation/preparation | E2 |
| E4 | BAM/CRAM + VCF/BCF/read/coverage/pileup/genotypes/reference checks | E2, E3 |
| E5 | bigWig/bigBed/signal/remote capability | E2 |
| E6 | EGA/ENA metadata, phenotype relationships, actual file/region retrieval | E1, E2, E3 |
| E7 | ENCODE/GEO/NCBI Datasets discovery and retrieval | E1, E2, E3 |
| E8 | Reference APIs/normalization/provenance/optional Atlas | E1 |
| E9 | inspect_locus/compare_samples composition and failure isolation | E4, E5, E6, E7, E8 |
| E10 | Verification, five demos, docs, clean package/container/platform checks | E1-E9 |
| E11 | Release and eligible registry submissions/status ledger | E10 |

Each epic has a GitHub parent issue, concrete child tasks, dependencies and acceptance criteria. Claude Code implements all epics in isolated branches/worktrees. Record session IDs and actual test evidence; independently review before integration and return defects to Claude.

## Release gates

1. Golden synthetic fixtures checked against samtools/bcftools with matching filters. Include multiallelic/ploidy, deletions/skips/mates, contig/interval boundaries and truncation.
2. Equivalent local, HTTP byte-range and local MinIO results; missing/corrupt indexes, ignored ranges, expired auth/URLs, interrupted resume and checksum failures.
3. CRAM correct/wrong/missing/embedded references. bigWig and bigBed remote capability tested.
4. Inject dummy ambient AWS credentials and prove they are unused. Never inspect real work accounts. Test access denials, relationships, secrets and egress boundaries.
5. Real MCP initialize/list/call round-trips using stdio and authenticated Streamable HTTP.
6. Clean install demonstrations: bounded real EGA test-data region; real ENA retrieval; public ENCODE signal; reference sequence plus real ClinVar evidence; local MinIO with no AWS account. Record commands/results and dates. Mocks alone do not establish source readiness.

## Distribution and registry inventory

Publish PyPI rewire-genomics-mcp, GHCR OCI image and a versioned GitHub release. Include server.json with namespace io.github.rewire-bio/genomics-mcp and required README/OCI metadata. Windows uses WSL2/container.

Submit to eligible unpaid routes: official MCP Registry, BioContextAI, Glama (check automatic ingestion first), Smithery local bundle, Docker MCP Catalog, mcpservers.org, punkpeye awesome list after a real Glama listing, and GitHub MCP Registry onboarding. Track version, URL, date and submitted/approved status separately. PulseMCP new submissions are currently paused; MCP.so currently requires $39, outside the approved unpaid launch. Do not invent approvals or pay for listings. External account/maintainer blockers remain explicit.

## Primary documentation

- EGA public metadata: https://ega-archive.org/discovery/metadata/public-metadata-api/ and https://metadata.ega-archive.org/spec/
- EGA download/htsget/test account: https://github.com/EGA-archive/ega-download-client
- ENA reports: https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/file-reports.html
- ENCODE REST: https://www.encodeproject.org/help/rest-api/
- GEO access: https://www.ncbi.nlm.nih.gov/geo/info/geo_paccess.html
- NCBI Datasets: https://www.ncbi.nlm.nih.gov/datasets/docs/v2/
- pysam: https://pysam.readthedocs.io/en/latest/api.html (0.24.1 fixes Linux S3 packaging)
- HTSlib: https://www.htslib.org/doc/ and https://www.htslib.org/doc/reference_seqs.html
- pyBigWig: https://github.com/deeptools/pyBigWig
- ClinVar: https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/
- HGNC: https://www.genenames.org/help/rest/
- UniProt: https://www.uniprot.org/help/api
- Ensembl: https://rest.ensembl.org/documentation/
- gnomAD: https://github.com/broadinstitute/gnomad-browser (public 10 requests/minute)
- AlphaGenome Atlas: https://www.alphagenomedocs.com/api/atlas.html (optional key and source terms)
- MCP registry: https://modelcontextprotocol.io/registry/quickstart

Reusable MIT implementations worth inspecting with attribution: BAMCP (https://github.com/RTrentJones/BAMCP) and BioMCP (https://github.com/genomoncology/biomcp). Do not claim novelty for API federation.
