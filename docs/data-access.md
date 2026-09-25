# Data access: storage, transfers, readers and signal (E2–E5)

Status 2026-09-25: implemented in this branch, not released. Evidence and limits are at the end.

All structured coordinates are 0-based half-open with an explicit assembly. Nothing is lifted
over, renamed (`chr1` vs `1`) or inferred.

## Providers and components

| Package | Epic | Registers |
| --- | --- | --- |
| `genomics_mcp.storage` | E2 | resolvers `file`, `http`, `https`, `s3`; `list_files` for `local`, `s3`; component `storage` |
| `genomics_mcp.artifacts` | E3 | `fetch_file`, `get_transfer_status`, `cancel_transfer`; `get_sequence/fasta`; `get_features/bed,gff3,gtf`; component `transfers` |
| `genomics_mcp.readers` | E4 | `get_reads`, `get_coverage`, `get_pileup` for `bam`, `cram`; `get_variants` for `vcf`, `bcf` |
| `genomics_mcp.signal` | E5 | `get_signal/bigwig`; `get_features/bigbed` |

`ftp` is not registered. ENA and NCBI serve the same files over HTTPS; use those URLs.

Other providers use the storage layer through the registry:

```python
storage = ctx.require_component("storage").get(ctx)  # StorageManager
resolved = await ctx.resolve_file(file, interval=interval)  # any registered resolver
storage.require_ready(resolved, needs_index=True)  # typed error unless queryable
async with storage.native_call(ctx) as call:  # owns this invocation's proxy routes
    params = await call.reader_params(resolved)  # remote URLs -> loopback proxy routes
    result = await call.run("genomics_mcp.<module>:<task>", params, what="...")
prov = storage.provenance(resolved, method="...", transformations=[...])
```

Readers accept a plain core `ResolvedFile` from other resolvers (e.g. E6 `htsget`/`ega`). If it
sets `region` and has no index, the slice is scanned and filtered to the interval. Readers use
the served format (`resolved.file.format`): EGA serves BAM slices for CRAM sources, so no CRAM
reference rules apply to them. Pileup indexes a private copy of an unindexed BAM slice.
Results include `region_slice` (served format, slice region, provider record counts).

## Archive integration (E6/E7)

- `ega://` region reads use the EGA resolver's bounded htsget slice; never a whole file.
- `fetch_file` for a scheme with a `transfer_backend:<scheme>` component (EGA) calls
  `describe` for size/MD5, applies the E3 budget and quota, then streams with `open(start, end)`
  in ranged chunks. Ranges start at 64 KiB-aligned offsets and never span the whole object:
  EGA answers a whole-object range with 200, and returned wrong bytes for a range starting
  at an unaligned offset (observed live 2026-09-25; the MD5 check caught it). Resume truncates
  the partial file to an aligned offset. The source MD5 is verified on the complete file.
- `fetch_file` with `prepare=true` on an NCBI Datasets `GENOME_FASTA` package calls
  `preparer:ncbi_genome_fasta` as a managed background job: it reserves its budget, can be
  cancelled, and keeps a job ID if the call times out. The preparer bounds the `.fai` before
  writing it, so ZIP + FASTA + `.fai` stay within the budget (scoped edit in
  `catalogs/ncbi_datasets.py`). `artifact_file` is the verified FASTA + `.fai`.
- ENA sequence FASTA records are ordinary HTTPS files. Sidecar probes that a server answers
  with a 4xx other than 401/403 (ENA returns 400) count as absent.

## Storage (E2)

### Local files
- `/abs/path`, `file:///abs/path` and `file://localhost/abs/path`. Other file URI hosts, queries
  and fragments are refused. Percent-escapes are decoded.
- The path must resolve, after symlinks, under `paths.allowed_roots` or the work dir.
- The first 64 KiB are read to identify content (BAM, CRAM, BCF, VCF, BGZF vs ordinary gzip,
  bigWig, bigBed, index kinds). Content that does not match the format is `invalid_input`.

### Public HTTP(S)
- Range support is proven with `GET Range: bytes=0-0` expecting `206` and a matching
  `Content-Range`. HEAD and `Accept-Ranges` are not trusted.
- A `200` reply is closed without reading the body; the file is `download_required`.
- Redirects are followed manually (at most 5). Every hop is checked before it is sent: cloud
  metadata names and any link-local address are refused (also when a hostname resolves to
  one, and again for the connected peer); https→http downgrades are refused.
- Loopback/private addresses and plain `http` need the host in `storage.local_network_hosts`.
  S3 profile endpoints are allowed implicitly.
- Native readers never receive an upstream URL (see "Native reader isolation"). Results and
  logs carry the original URL with signed query values redacted.
- No proxy, netrc or ambient credential is used (`trust_env=False`).

### S3
- Anonymous public S3 by default (`storage.allow_public_s3`), unsigned requests to
  `storage.public_s3_endpoint`.
- Private buckets need `file.storage_profile` naming a `[storage.profiles.<name>]` entry with an
  explicit endpoint and credential variable names. Bucket allowlists are enforced.
- Clients come from an isolated botocore session: the configuration store ignores the
  environment, config and credential files are empty files in the work dir, the default
  credential chain and SSO token provider are replaced by refusing stubs, proxies are off.
- Readers never receive `s3://`. Data and index objects are presigned separately for 15
  minutes (`expires_at`) and then pass the same HTTP range preflight.
- Requester-pays is sent only when the profile sets `requester_pays = true`, as a signed
  query parameter (HTSlib and libBigWig cannot send extra headers).

### Indexes and readiness
- An explicit `index_uri` is read and checked: kind (BAI/CSI/CRAI/TBI/FAI), compression, and
  for TBI the preset it was built with (VCF/BED/GFF). A mismatch is `invalid_input`.
- Without `index_uri`, conventional sidecars are observed one by one (e.g. `x.bam.bai`,
  `x.bam.csi`, `x.bai`). Remote sidecars are fetched with one bounded request each. URLs with a
  query string are not probed; pass `index_uri`.
- BGZF FASTA also needs `.gzi`.
- `readiness.state`: `ready`, `download_required`, `index_required`, `not_locus_ready` (plain or
  ordinary-gzip text, FASTQ, SAM), `unsupported`, `unknown`, with reasons.
- Reader errors: no index → `preparation_required`; a sidecar that is present but invalid →
  `preparation_required` naming the problem; an explicit incompatible index → `invalid_input`.

### Listings
`list_files` with `source="local"` and a directory path, or `source="s3"` with
`s3://bucket/prefix` (and optional `storage_profile`). Each record has the `FileRef` (size,
readiness, observed `index_uri`) and a state: `available`, `missing` (broken link), `denied`
(permission or outside the roots), `unsupported`, `corrupt`. Unpaired index files and
subdirectories are listed separately. S3 listings pair indexes on the same page only and do not
open objects.

## Native reader isolation

### Loopback range proxy
HTSlib/libcurl and libBigWig follow redirects and resolve names themselves, so a preflight
alone cannot bound where they connect. Each remote data, index or companion URL of a call is
registered with a loopback proxy (`storage/proxy.py`) and the child receives
`http://127.0.0.1:<port>/<random token>/<name>`.

- Every upstream request, including every redirect hop and the connected peer, goes through
  the guarded Python transport. Redirects are never passed to the child.
- Upstream must answer ranges with 206; a 200 is refused.
- Routes belong to one reader invocation (a private lease, not the request id, so concurrent
  readers in one fan-out are independent), expire at the call deadline and are capped at 512 MiB relayed.
  Finishing, timing out or cancelling a call cancels in-flight relays and closes upstream
  connections.
- A refused hop (e.g. a redirect to an unapproved host) becomes the call's typed error; the
  unapproved host receives no request.

pysam/HTSlib and pyBigWig run in a child process per call (`python -I -m
genomics_mcp.storage._worker`), because a hung libcurl read cannot be interrupted from a thread.

- Environment from an allowlist (PATH, locale, TMPDIR, CA bundle variables). No `AWS_*`, cloud,
  proxy, netrc, `HTS_*` or `PYTHON*` variables. `HOME`, AWS config files and `REF_PATH`/`REF_CACHE`
  point at empty directories under `<work_dir>/.isolation`. EC2 metadata is disabled.
- Working directory is an empty directory. Parameters travel on stdin, never argv.
- The process group is killed at the call deadline (`timeout`). Tasks also stop at a soft
  deadline and mark results incomplete.
- stderr is captured (64 KiB), redacted and returned as `details.native_stderr` on errors.
- Only functions listed in a module's `NATIVE_TASKS` can run. There is no shell tool.

## Transfers (E3)

`fetch_file` starts or continues a transfer and waits up to 5 s; then use
`get_transfer_status`. Results are paths, never file bytes.

- Budget: `limits.max_transfer_bytes` (100 MiB) by default. A larger `budget_bytes` is accepted
  up to `limits.transfer_budget_ceiling_bytes`. Data and index bytes share one budget. A known
  size over budget is refused before any download.
- Quota: bytes in the work dir plus bytes reserved by active transfers may not exceed
  `limits.workspace_max_bytes`.
- Bytes go to `transfers/<id>/<role>.part`, then are renamed atomically into
  `artifacts/<id>/<safe name>`. Names are sanitized; no path can leave the artifact directory.
- Resume: the job stores size, ETag and Last-Modified (never a signed URL). A resumed GET sends
  `Range: bytes=<done>-` and `If-Range`. A `200` reply, a different ETag, or a Content-Range that
  does not start at `<done>` restarts from zero (network bytes capped at twice the budget).
  Jobs interrupted by a server stop are reported `failed` with `resumable: true` and continue
  when `fetch_file` is called again with the same file.
- Checksums (`md5`, `sha1`, `sha256`, `sha512` from `file.checksums`) are computed over the
  complete file only. A mismatch fails the job, removes the bytes and is not resumable. `etag`
  and `crc32c` are not treated as content checksums.
- `cancel_transfer` stops the task and removes partial bytes. A cancelled job never becomes
  completed.
- A completed artifact is reused only when the source validators (ETag/Last-Modified; for
  local files nanosecond mtime and inode) and size are unchanged, the artifact file is
  unchanged, and any checksum requested now matches checksums of the source bytes. Otherwise
  the file is fetched (and verified) again.
- `artifact.checksums` are always computed on the returned bytes, after any preparation.
  `checksum_verified` is true only when those bytes are the verified source bytes.
  `source_verification` reports checksums of the source bytes as received.
- Failure or cancellation removes the job's own staged and prepared files; sources and other
  jobs' artifacts are never touched.
- A running job is shared only with requests whose checksum requirements it will verify;
  otherwise a separate job runs (the other job and its artifact are untouched).
- Local files are not copied unless `prepare=true` needs a copy to index; a no-copy fetch
  reserves no quota.
- Preparation outputs count against the job budget and the work dir quota: the worker caps
  the size of any file it writes and checks growth after each step; exceeding either fails
  with `budget_exceeded` and removes the outputs.
- `prepare=true` builds indexes on a copy in the work dir, never on the source: FASTA `.fai`
  (and `.gzi`), ordinary gzip → BGZF, plain/gzip VCF/BED/GFF3/GTF → BGZF + tabix (CSI for long
  contigs), BAM `.bai`, BCF `.csi`. Unsorted input fails with a hint; records are not reordered.
- `artifact_file` is a `FileRef` for the result with observed readiness, ready to pass to the
  genomics tools. Remote FASTA sequence queries use range reads; whole references are never
  fetched implicitly.

## Genomic readers (E3–E5)

Every result includes `interval`, `file` (redacted), `assembly` and `applied_filters` where
relevant. `assembly.status` is `file_header_declared` (e.g. BAM `@SQ AS`, VCF `##contig
assembly=`), `file_metadata_asserted` (`FileRef.assembly`) or `caller_asserted`. A declared
assembly that differs from the request is `invalid_input`. An interval past the contig end is
`invalid_input`; an unknown contig is `not_found`.

| Tool | Semantics |
| --- | --- |
| `get_reads` | Records overlapping the interval; `samtools view -f/-F/-q`. No sequence unless `include_sequence`. Mates, template length, flags, small tags. |
| `get_coverage` | `samtools depth -a` semantics with the same exclude flags, `-Q`, `-q`. Deletions and reference skips are not counted; overlapping mates are both counted. Per-base or `bin_size` bins (mean/min/max). `max_records` caps output only; the summary covers the whole interval. If the input cap (5,000,000 reads) or deadline stops processing, the result is `partial` and positions from the first unprocessed read are not computed (never reported as zero). |
| `get_pileup` | `samtools mpileup -B -A` semantics: no BAQ, orphans counted, overlap detection on. An entry is kept when `quality[qpos] >= min_base_quality`, `qpos` being the next query base for deletions/skips. Per position: kept depth, bases by strand, deletions, reference skips, insertions after, deletions starting after, low-quality exclusions, mates in column, `depth_limit_reached`. Reference base only when a reference is given. No calling. |
| `get_variants` | VCF POS kept as `pos`; `start`/`end` 0-based over REF (END-aware). REF, ALT list (symbolic and `*` kept), IDs, QUAL (null if missing), FILTER, INFO, FORMAT per sample. Genotype: exact GT text, allele indices (null = missing), allele strings, ploidy, per-separator phasing. TBI and CSI (long contigs). |
| `get_sequence` | Plain FASTA + `.fai`, or BGZF + `.fai` + `.gzi`; local or range-capable remote. Remote indexes are staged in the work dir only within the free quota (and 64 MiB), removed afterwards. Case preserved. |
| `get_features` | BGZF + tabix BED/GFF3/GTF. BED coordinates as-is; GFF3/GTF `start - 1`. Native line and attributes kept (GFF3 percent-decoded, multi-values split). `feature_types` for GFF3/GTF only. bigBed: columns named from the file's autoSql. |
| `get_signal` | bigWig exact summaries (`exact=True`) for the interval and optional `bins` (libBigWig edges `start + i*L//n`). Without bins: data intervals clipped to the interval. Missing data is `null`. |

### CRAM references
- A reference is used only when supplied explicitly (`reference` or `file.reference_uri`), local,
  and matching the header: contig present, length equal, and MD5 equal to `@SQ M5` (cached in the
  work dir by path, size and mtime). Mismatch → `invalid_input`.
- Without a reference, decoding succeeds only for CRAMs that embed their reference or store
  bases reference-free. Otherwise → `preparation_required`.
- HTSlib would otherwise find references by header M5 in `REF_CACHE`/`REF_PATH` (network by
  default) and then open the `@SQ UR` path. The child clears both variables, and for every contig
  without an approved reference places an empty file named by its M5 in a private `REF_CACHE`, so
  a slice needing it fails instead of reaching the UR fallback. `@SQ M5` values must be 32 hex
  digits (they are used as path components). A contig with a UR naming an existing local file
  but no M5 is refused.

## Core extensions (backward compatible)

- `storage.local_network_hosts: list[str] = []` (config).
- `FetchFileRequest.prepare: bool = False` and the `fetch_file` tool parameter.
- `ListFilesRequest.storage_profile: str | None = None` and the `list_files` tool parameter.

Covered by `tests/storage/test_formats.py::test_core_extensions_are_backward_compatible`.

## Evidence (2026-09-25, macOS arm64, Python 3.12, pysam 0.24.1/HTSlib 1.24, pyBigWig 0.3.26)

Command:

```sh
GENOMICS_MCP_NETWORK_TESTS=1 GENOMICS_MCP_TEST_S3_KEY=... GENOMICS_MCP_TEST_S3_SECRET=... \
GENOMICS_MCP_TEST_FIXTURES=<local copy of the MinIO fixtures> uv run pytest -q
```

Result on the E2–E5 stage: 238 passed, 0 skipped. After merging main and wiring EGA/ENA/NCBI: 555 passed, 6 skipped (other workers' opt-ins), 1 failed — E6's live test still asserts `fetch_file` is `unsupported` because E3 was absent; that assertion is obsolete. Without the opt-in variables the
MinIO and live tests are skipped.

- samtools 1.24 / bcftools 1.24 oracles on synthetic golden data: `samtools view` (6 flag/MAPQ
  combinations), `samtools depth` (6 combinations), `samtools mpileup` (5 combinations incl.
  overlap, orphans, max depth), `bcftools query` for VCF (CSI), VCF (TBI) and BCF; `tabix` for
  BED/GFF3/GTF. All exact.
- CRAM: correct reference (MD5 verified, records equal to BAM), wrong reference (same names and
  lengths, different bases) refused, missing reference refused although the header UR file
  exists and is readable, ambient `REF_PATH`/`REF_CACHE` holding the reference ignored,
  embedded-reference and reference-free CRAMs decoded, malformed M5 refused.
- Local vs HTTP range vs local MinIO (RELEASE.2025-10-15T17-29-55Z on 127.0.0.1:39000,
  explicit synthetic credentials): identical records for reads, coverage, pileup, VCF, BCF,
  BED, FASTA, bigWig and both CRAMs; dummy ambient AWS variables, config files and a dead proxy
  present and unused.
- Live EGA public test account (EGAF00007243773, GRCh38 chr10:[10000,10050)): 42 of 91
  received records returned, matching the independent count; pileup on the slice; whole-file
  `fetch_file` 194,821 bytes with source MD5 `ed365c71…` verified, 1,097 alignments, `.bai`
  prepared; also over MCP stdio. ENA DQ285577.1 FASTA fetched, indexed and queried. NCBI
  GCF_000819615.1 (phiX) prepared; NC_001422.1:[0,20) = GAGTTTTATCGCTTCCATGA.
- Live ENCODE ENCFF792QDS (GRCh38 bigWig, 1.4 GB, range reads through the resolved redirect):
  chr1:[1000000,1001000) exact mean 26.361254017233847 in about 4–5 s. ENCFF001JBR (mm9
  bigBed): features with autoSql names.

## Known limits

- A CRAM reference must be a local FASTA. Remote references must be fetched first.
- The native child is not network-sandboxed by the OS; it is only ever given local paths and
  loopback proxy routes. File formats read here do not carry URLs the readers would follow,
  and CRAM header reference paths are sealed off as described above.
- S3 listings pair indexes only within one page and do not check range support or content.
- Anonymous public S3 uses one configured endpoint/region; buckets in other regions return an
  `upstream_error` with a hint to configure a profile.
- VCF 4.4 per-allele phasing is kept in `genotype.text`/`separators`; `phased` is true only when
  all separators are `|`.
- Pileup with `min_base_quality=0` counts both copies of overlapping mate bases (as samtools
  does), because overlap detection lowers one copy's quality to 0.
- Preparation does not sort files and does not index CRAM.
- MinIO community edition is archived upstream; it is a local test dependency only.
