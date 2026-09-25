# Composition tools (E9)

Package: `genomics_mcp.composition`. Tools: `inspect_locus`, `compare_samples` (dispatch key `default`).

## How they run

- Each file gets a positional ID (`f0`, `f1`, …; `reference` for the call's reference FASTA). Components are named `<file>.<role>`, for example `f0.reads`, `f1.variants`. Duplicate paths or accessions get separate IDs and a warning.
- Every component calls the registered genomics handler for its operation and file format (`registry.handler(op, format)`) with a typed sub-request. It uses the same format and region checks as the service. Nothing calls `GenomicsService`, and nothing opens files itself: storage, native-process isolation, index/readiness checks, CRAM reference M5 checks and redaction all come from the E2–E5 providers.
- Components run concurrently under core `fan_out`. Each one is bounded by the shortest of:
  - the configured `[sources.<name>] timeout_s` for the file's `source` and storage backend (`local`, `https`, `s3`, or the scheme);
  - the call deadline.

  A failing or stalled component is reported as its own error or timeout, and the others still return. Cancelling the call cancels every component, and the E2 native runner kills its reader processes.
- A file whose `source` or storage backend is disabled in configuration is never read. Its planned components keep their roles and report `disabled`.
- Checked before any read:
  - the region limit;
  - `limits.max_files_per_call` (default 16; more files → `budget_exceeded`);
  - every file and reference that declares an assembly must equal `interval.assembly` exactly, otherwise `invalid_input`. There is no liftover, build substitution or contig renaming.

## Result layout

- `data.records` holds every returned record as `{component, type, record}`. `record` is the handler's native record, unchanged.
  - `inspect_locus` interleaves records round-robin across components, so response-byte trimming cuts evenly instead of dropping whole files.
  - `compare_samples` returns bin rows, then variant-site rows.
- The caller's `max_records` is the aggregate cap. Each component gets a share: at most 200 reads, 500 variants, 200 features and 20 bins. The envelope `truncation.limit` is the aggregate cap; each component's own truncation is in its entry.
- `data.components` (`inspect_locus`) and `data.files` (`compare_samples`) hold one small entry per component:
  - status and `records_returned`;
  - per-component truncation and errors;
  - the handler's `assembly` identity: `caller_asserted`, `file_metadata_asserted` or `file_header_declared`;
  - applied filters, summaries and provenance.
- If this metadata would use more than half of `max_response_bytes`, entries are shortened to essentials: status, counts, truncation, error codes, assembly status, summaries and compact provenance. `data.metadata_compacted` says so. A 4 KiB cap still returns data.
- `errors` and `source_status` are per component (`source` = component ID; any original source is kept in `details.origin_source`). If no component produced data, the call is an error, never an empty success.

## inspect_locus

| Format | Components |
| --- | --- |
| bam, cram | `get_reads` (no read sequences or qualities) and `get_coverage` (≤ 20 bins plus a summary, with the reader's `complete`/`complete_until`) |
| vcf, bcf | `get_variants` with genotypes as the reader returns them; the first 20 samples per file, with the rest counted as omitted |
| fasta, `reference` | `get_sequence`, only up to `min(10,000 bp, max_response_bytes / 8)`; longer intervals are an explicit omission |
| bed, gff3, gtf, bigbed | `get_features` |
| bigwig | `get_signal` (binned mean plus summary) |
| fastq, tsv, other, unknown | explicit per-file `unsupported` / `invalid_input` |

For CRAM, the file's own `reference_uri` is used. Otherwise the call's `reference` is passed, and the reader accepts it only if its MD5 matches the header M5.

`data.consistency` compares contig lengths across components. A disagreement is reported as an error, because the files are probably on different builds.

### Reference evidence and consent

- Evidence is consulted only when `reference_sources` is given, and only through the E8 `reference_evidence` facade. The public reference handlers are never used for file-derived values.
- Usable sources:
  - `ensembl`, `clinvar`, `gnomad`, `alphagenome_atlas`: `lookup_variant`;
  - `local_fasta`: local normalization against `reference`.

  Other names are reported as `not_implemented`. Disabled sources report `disabled` and are not contacted.
- Only alleles observed in the inspected VCF/BCF files are looked up. With no variant file (a bare locus), the call reports that these sources cannot answer a locus. It does not invent a gene, variant or phenotype.
- At most 5 alleles are looked up: the first 5 distinct base-level alleles in (POS, REF, ALT) order. Multi-allelic ALTs count separately; symbolic, `*` and breakend alleles are skipped. `annotation.selection` lists the selected and omitted alleles. Each lookup returns at most 50 evidence records.
- Consent is checked before each facade call. The egress context covers the allele's files plus `reference`, because REF checks read it. If any of them is `private` (the default for every FileRef) and `allow_external_annotation` is false:
  - no external request is made;
  - one `consent_required` error is returned, and the external sources are marked `skipped`;
  - local file data is still returned;
  - `local_fasta` normalization still runs.

  The facade enforces the same rule a second time.
- With consent, only the requested sources are sent the alleles. A local `reference` avoids remote reference-sequence fetches.

## compare_samples

- Accepts bam, cram, vcf, bcf and bigwig. Any other format is rejected before work. No external source is ever contacted, since this tool has no consent input.
- BAM/CRAM: read depth summary and bins from `get_coverage`, with its default filters reported. bigWig: signal mean from `get_signal`. The bin count divides the interval (at most 20), so bins align exactly across files. A `null` signal value means no data.
- VCF/BCF: genotypes merged into sites keyed by (contig, POS, REF). Per-file records keep their own ALT lists; allele indexes refer to that file's ALTs, and `allele_bases` are included.
  - Each call keeps the reader's GT text, allele indexes, ploidy, phasing, missingness and FORMAT fields.
  - `call_class` is a literal label from the allele indexes: `homozygous_reference`, `heterozygous`, `homozygous_alternate`, `haploid_*`, `partially_missing` or `missing`.
- Sample keys are always file-qualified (`f1:S1`). The same name in different files is listed in `duplicate_sample_names` and not assumed to be the same individual.
- `samples` selects VCF/BCF sample columns; names absent from a file are retried without them and reported. At most 50 samples per file are shown; capped samples are counted as omitted, never reported as missing.
- Alignment and signal files are compared per file. `sample_links` appear only when the FileRef carries source-asserted sample relationships; read-group names are not used.
- Observation codes:
  - `called`;
  - `missing_call`: the record exists but GT is missing;
  - `no_record`: the file has no record at this site. This is not a reference call;
  - `not_read`: the file was truncated before this position.

  Failed or disabled files appear in `unavailable_files`, never as zero depth or a reference genotype.
- `normalization.library_size_normalized` is `false`: depths are raw counts. `interpretation` states that differences are literal observations, not association, causal or clinical findings.

## Tests (`tests/composition`)

- The fixtures are tiny real files written with pysam/pyBigWig: FASTA, two BAMs with known contrasting depth, a two-sample VCF (phased, missing, multi-allelic, haploid), a second VCF that reuses a sample name, a bigWig, a 51-sample VCF and a dense 30-variant VCF.
- When `genomics_mcp.readers` and the other E2–E5 providers are installed (they are on this branch), the tests use the real registered providers. `tests/composition/doubles.py` is only a fallback for builds without them.
- HTTP spies wrap both the E8 runtime client and the core `PublicHttpClient`.
- Coverage:
  - end-to-end BAM+VCF+FASTA;
  - contrasting coverage and signal;
  - missing call versus no record versus homozygous reference;
  - sample order keeping each GT with its sample;
  - duplicate sample names, the samples filter and the 51-sample cap;
  - failed and disabled files, with zero reads of disabled files;
  - a stalled file under a per-source timeout and under the overall deadline;
  - wrong assembly and unsupported formats rejected before work;
  - private/no-consent zero HTTP, local-only normalization, consent limited to the requested source, and a private reference blocking public alleles;
  - a bare locus, and dense-region bounding and determinism;
  - lowered `max_records` and response caps (including 4 KiB);
  - signed URLs never echoed;
  - task cancellation, and native reader processes killed on cancellation;
  - in-process MCP calls, and a stdio subprocess with the default providers.
