# Cache and performance (#49, #50)

One bounded in-memory cache per server process (`genomics_mcp/cache.py`). Nothing is written to disk and there is no external service.

## What is cached

**File byte ranges.** Native readers (HTSlib, libBigWig) already reach remote files only through the loopback range proxy ([data access](data-access.md#loopback-range-proxy)). For eligible files the proxy serves ranges from 64 KiB aligned blocks. Each new tool call first revalidates the file: the data preflight (`bytes=0-0`) and a one-byte request before each index/sidecar read record the object's size and strong ETag. Blocks are keyed by URL, size and ETag, so a later call reuses header, index and data blocks of the same file version, including for a different interval. Each missing run is one request with `If-Range: <ETag>`. If the answer is not a 206 with that ETag and size, the file's blocks are dropped and the call fails with a retryable `upstream_error`. Blocks from different versions are never combined. Only complete blocks are stored.

**API responses.** Successful (HTTP 200) responses from public archive, catalogue and reference APIs, keyed by source, method, URL, parameters, `Accept` and the GraphQL body. Only GET is cached, plus POSTs that the adapter declares as read-only GraphQL queries (`mutation` is refused). JSON must parse. A GraphQL answer carrying `errors` is not stored.

## Coverage matrix

"Measured" means a deterministic local test or benchmark with request/byte counts from a counting server or transport. "Live" means the bounded run of 2026-09-28 below. Anything else shares the code path but has not been measured.

| Tool | Formats / source | Cached | Evidence |
| --- | --- | --- | --- |
| `get_signal` | bigWig over public HTTPS | ranges | measured; live (ENCODE ENCFF792QDS) |
| `get_reads`, `get_coverage`, `get_pileup` | BAM over public HTTPS | ranges | measured |
| `get_variants` | VCF.gz + TBI over public HTTPS | ranges | measured |
| `get_sequence` | FASTA + `.fai` over public HTTPS | ranges (the staged `.fai` too) | measured |
| `get_features` | BED.gz + TBI over public HTTPS | ranges | measured |
| `inspect_locus` | BAM + VCF + bigWig | ranges | measured |
| `get_features` (bigBed), CRAM, BCF, GFF3/GTF, BGZF FASTA, `compare_samples` | public HTTPS | ranges (same proxy path) | not measured |
| `list_files` etc. | ENCODE REST | API | measured (recorded response) |
| discovery ops | ENA portal GET, EGA metadata, GEO E-utilities, NCBI Datasets | API | not measured |
| `resolve_identifier`, `lookup_gene`, `lookup_protein` | HGNC | API, if allowed | measured (recorded); live HGNC sends `Cache-Control: no-store`, so it is never stored |
| `lookup_protein` etc. | UniProt | API | measured (recorded); live |
| reference ops | Ensembl REST, ClinVar/NCBI E-utilities | API | not measured |
| `lookup_variant`, `lookup_gene` | gnomAD, Open Targets (read-only GraphQL POST) | API | gnomAD helper tested with a counting mock transport |

## Deliberate exclusions

- Files not marked `visibility: "public"`. Files are private by default. Archive listings (ENCODE, GEO, NCBI, ENA, open EGA files) mark public files as public.
- Local files (no network), S3 objects (readers get presigned URLs), EGA and htsget (authenticated, region-only) and `fetch_file` transfers.
- URLs with a query string or userinfo, including a public URL that redirects to a signed URL.
- Objects without a strong ETag and size (weak ETag or `Last-Modified` only). Responses marked `Cache-Control: no-store`, `no-cache` or `private`, or carrying `Set-Cookie` or `Vary: *`. `max-age`/`s-maxage` shorten the lifetime.
- Requests with credentials: any header other than `Accept`, `Content-Type`, `User-Agent` or `Accept-Language`, or key/token/signature/`X-Amz-*` parameters. Configured NCBI, GEO or NCBI Datasets API keys therefore disable caching for those sources.
- `Range` requests through the API helpers, POSTs other than declared GraphQL queries (for example ENA's form POST search), `HEAD`, and every non-200 or unparseable response.
- Reference queries derived from private files, even with `allow_external_annotation`.
- AlphaGenome Atlas (keyed gRPC). `PublicHttpClient` (`public.py`) has no caller in this build.

## Guarantees on hits

- Destination and access policy run first. A source disabled in configuration, a host no longer allowed, or a response larger than the call's byte budget is not served from the cache.
- Range hits count against the per-call proxy byte cap and deadline. Cancelling a call cancels its relays as before. Native readers still run in a new child process per call.
- API hits do not wait on rate limits. Provenance and evidence keep the original retrieval time: records built in the call use the oldest reused response of that source. The envelope carries a `cache: ...` warning whenever a call reused anything.
- Stored values are immutable bytes. Every hit is decoded afresh, so one caller cannot alter another's result.

## Configuration, off switch and clearing

```toml
[cache]
enabled = true          # or GENOMICS_MCP_CACHE_ENABLED=0
max_bytes = 67108864    # memory bound for cached bytes (LRU eviction)
max_entries = 4096
ttl_s = 300             # longest reuse; Cache-Control may shorten it
```

Restart the server to clear the cache. There is no per-call bypass argument, so tool schemas are unchanged. `genomics://status` reports aggregate metrics only: entries, bytes, bounds and counts. These are range/api hits, misses, stores, evictions, expiries, invalidations, bypasses, and upstream bytes fetched into blocks. No URLs, identifiers, query values or bodies are reported.

## Known limits

- A warm call still sends one one-byte request per remote object (data file and each index) to revalidate it.
- HTSlib reads open-ended ranges. The proxy fetches missing blocks in growing runs (1 to 64 blocks) as the reader consumes them. Exact byte counts for BAM/FASTA vary a little with timing. Assertions therefore compare modes and never use exact counts for these readers.
- Concurrent misses for the same block are fetched twice; there is no request coalescing.
- API freshness is heuristic: entries live at most `ttl_s` (or `max-age`), with no conditional revalidation.
- With Streamable HTTP, all sessions share the process cache. The server has a single bearer token and user.

## Benchmark

```sh
uv run python scripts/benchmark_cache.py --samples 5 --out cache-local.json
uv run python scripts/benchmark_cache.py --live-only --samples 3 --out cache-live.json   # opt-in
```

The script builds deterministic synthetic files: a 2 Mb contig as bigWig, BAM (1 read/100 bp), VCF.gz, FASTA and BED.gz, about 4 MiB in total. It serves them from the counting range server in `tests/storage/support.py` and runs each workload through `GenomicsService.call`, the path MCP tools take. Modes: `disabled` (cache off), `cold` (new server per sample), `warm` (same query again on a primed server) and neighbour interval B (`chrP:1500000-1510000`) after a warm interval A (`chrP:100000-110000`), against B cold. The neighbour case is the one a whole-result cache cannot serve. API workloads replay recorded responses through a counting mock transport. The script checks that result digests are identical across modes and for four concurrent warm calls. CI runs the same workloads once (`tests/cache/test_range_cache.py`) and asserts only relationships (warm < cold, neighbour-after-warm < neighbour-cold), never wall-clock times.

## Results, 2026-09-28 (macOS arm64, Python 3.12.14, pysam 0.24.1 / samtools 1.24, pyBigWig 0.3.26)

### Local controlled fixtures

`demos/results/2026-09-28/cache-local.json`, 5 samples per mode. Cells show upstream requests / KiB served by the counting server; bytes are from the first sample. Every workload returned identical result digests with the cache off, cold and warm, and for four concurrent warm calls. Interval B returned the same digest after warm A as on a cold server. Timings are dominated by the per-call reader process (about 300 ms) and are diagnostic only.

| Workload | Disabled | Cold | Warm (same query) | Interval B cold | B after warm A | p50 ms off / cold / warm |
| --- | --- | --- | --- | --- | --- | --- |
| signal (bigWig) | 6 / 564 | 5 / 384 | 1 / 0 | 5 / 448 | 2 / 128 | 341 / 375 / 328 |
| reads (BAM) | 8 / 1,756 | 9 / 590 | 4 / 128 | 11 / 910 | 4 / 128 | 414 / 413 / 348 |
| coverage (BAM) | 7 / 1,755 | 10 / 910 | 4 / 128 | 11 / 974 | 2 / 0 | 372 / 379 / 350 |
| pileup (BAM) | 10 / 3,444 | 12 / 846 | 7 / 384 | 14 / 1,102 | 2 / 0 | 396 / 402 / 352 |
| variants (VCF.gz) | 9 / 923 | 6 / 215 | 2 / 0 | 6 / 215 | 2 / 0 | 324 / 325 / 305 |
| sequence (FASTA) | 6 / 2,432 | 8 / 448 | 3 / 64 | 11 / 896 | 7 / 640 | 332 / 350 / 304 |
| features (BED.gz) | 5 / 192 | 5 / 128 | 2 / 0 | 5 / 128 | 2 / 0 | 316 / 328 / 316 |
| inspect_locus (BAM+VCF+bigWig) | 28 / 4,995 | 26 / 1,382 | 11 / 256 | 34 / 2,675 | 8 / 128 | 466 / 470 / 435 |

A cell of 0 KiB means only the one-byte revalidation requests went upstream. For bigWig, interval B after A asked only for B's data blocks. No request for the header block was made after the revalidation byte (`tests/cache/test_range_cache.py`). Cold can use more requests than disabled because missing runs are fetched in bounded block-aligned requests. It transfers fewer bytes, since the uncached relay streams open-ended ranges.

Recorded API responses (counting mock transport, 5 samples): ENCODE `list_files` took 1 request per call when disabled or cold and 0 when warm. HGNC `resolve_identifier` took 3–4 and 0. UniProt `lookup_protein` took 1 and 0.

### Live public sources (opt-in)

`demos/results/2026-09-28/cache-live.json`, finished 13:23 UTC, with `--live-only --samples 3`. ENCODE ENCFF792QDS (GRCh38 bigWig, 1.4 GB, public S3), `get_signal` with 10 bins:

| Region | Cache off (ms) | Cache on (ms) |
| --- | --- | --- |
| chr1:1000000-1001000, 3 repeats | 5223, 4870, 4879 | 4103 (cold), 909, 502 |
| chr1:1001000-1002000 (adjacent) | 4942 | 509 |
| chr1:5000000-5001000 (distant) | 4834 | 1235 |

Results were identical with the cache on and off. The chr1:1000000-1001000 mean was 26.361254017233847, the value in the published ENCODE demo. The cached server fetched 704 KiB into 11 blocks for all five calls.

Live HGNC answered `Cache-Control: no-cache, no-store`, so `resolve_identifier` was correctly not cached. The repeat was not faster (174 ms, then 5038 ms; cause not investigated). UniProt `lookup_protein` P51587 took 130 ms, then 2 ms from the cache. The repeat kept the original `retrieved_at` and carried the `cache:` warning. Other providers were not run live for this change.
