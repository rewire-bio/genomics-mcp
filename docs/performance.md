# Cache and performance (#49, #50)

One bounded in-memory cache per server process (`genomics_mcp/cache.py`). Nothing is written to disk and there is no external service.

## What is cached

**File byte ranges.** Native readers (HTSlib, libBigWig) already reach remote files only through the loopback range proxy ([data access](data-access.md#loopback-range-proxy)). For eligible files the proxy serves ranges from 64 KiB aligned blocks. Each new tool call first revalidates the file: the data preflight (`bytes=0-0`) and a one-byte request before each index/sidecar read record the object's size and strong ETag. Blocks are keyed by URL, size and ETag, so a later call reuses header, index and data blocks of the same file version, including for a different interval. Each missing run is one request with `If-Range: <ETag>`. If the answer is not a 206 with that ETag, size and identity encoding, the file's blocks are dropped and the call fails with a retryable `upstream_error`. Blocks from different versions are never combined. Each block response's own policy is checked too. If it forbids sharing (`no-store`, `private`, `Set-Cookie`, a redirect to a signed URL, or a client holding cookies), the bytes still reach the reader, but nothing is stored and the file is dropped from the cache. Only complete blocks are stored.

**API responses.** Successful (HTTP 200) responses from public archive, catalogue and reference APIs. The key is a SHA-256 digest of the request exactly as the HTTP client would send it: method, URL with parameters, every header including the client's own defaults and cookies, and body. Different `Accept`, `Accept-Language` or `Accept-Encoding` values therefore never share an entry. Because every request header is in the key, a response's `Vary` is always satisfied. `Vary: *` is never stored. Only GET is cached, plus POSTs that the adapter declares as read-only GraphQL queries (`mutation` is refused). JSON must parse whenever JSON was requested or declared. HTML pages and GraphQL answers carrying `errors` are never stored. A stored body that an adapter later rejects is rejected again on a hit. The cache never turns it into a success.

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
- File ranges only: URLs with a query string or userinfo, including a public URL that redirects to a signed URL. API query parameters are normal and are part of the API key, unless they look like credentials (below).
- File objects without a strong ETag and size (weak ETag or `Last-Modified` only), with a non-identity `Content-Encoding`, or reached through a client that holds cookies.
- Responses marked `Cache-Control: no-store`, `no-cache` or `private`, or `Pragma: no-cache`, or carrying `Set-Cookie` or `Vary: *`. Freshness is `s-maxage`/`max-age`, else `Expires` − `Date`, minus the upstream `Age`, capped at `ttl_s`. An already stale or unparseable value means no reuse.
- Requests with credentials: any header other than `Accept`, `Accept-Encoding`, `Accept-Language`, `Content-Type` or `User-Agent` (checked on the request as sent, so client-level headers and cookies count), client-level auth, key/token/signature/`X-Amz-*` parameters, or URL userinfo. Configured NCBI, GEO or NCBI Datasets API keys therefore disable caching for those sources. A shared client that has received a cookie stops caching.
- API responses that were redirected to a URL with credential-like parameters.
- `Range` requests through the API helpers, POSTs other than declared GraphQL queries (for example ENA's form POST search), `HEAD`, and every non-200 or unparseable response.
- Reference queries derived from private files, even with `allow_external_annotation`.
- AlphaGenome Atlas (keyed gRPC). `PublicHttpClient` (`public.py`) has no caller in this build.

## Guarantees on hits

- Destination and access policy run first. A source disabled in configuration, a host no longer allowed, a response larger than the call's byte budget, or a reference call whose deadline has passed is not served from the cache.
- Range hits count against the per-call proxy byte cap and deadline. Cancelling a call cancels its relays as before. Native readers still run in a new child process per call.
- API hits do not wait on rate limits. Retrieval time is source-level, not per response. Archive provenance and reference evidence built during a call report the earliest time at which upstream produced any response of that source used by the call, cached or fresh. For a reused response this is the original time. A single cached response is therefore reported with the same time cold and warm, and a hit never looks fresh. The envelope carries a `cache: ...` warning whenever a call reused anything.
- Stored values are immutable bytes. Every hit is decoded afresh, so one caller cannot alter another's result.

## Configuration, off switch and clearing

```toml
[cache]
enabled = true          # or GENOMICS_MCP_CACHE_ENABLED=0
max_bytes = 67108864    # bound on cached bodies/blocks plus their keys and metadata (LRU)
max_entries = 4096
ttl_s = 300             # longest reuse; Cache-Control may shorten it
```

`max_bytes` counts each entry's payload, its key (API keys are fixed-size digests; range keys hold the URL and ETag), stored response headers and URL, plus a fixed per-entry overhead. It is not a measure of process RSS. An entry larger than `max_bytes` is never stored. Restart the server to clear the cache. There is no per-call bypass argument, so tool schemas are unchanged. `genomics://status` reports aggregate metrics only: entries, bytes, bounds and counts. These are range/api hits, misses, stores, evictions, expiries, invalidations, bypasses, and upstream bytes fetched into blocks. No URLs, identifiers, query values or bodies are reported.

## Known limits

- A warm call still sends one one-byte request per remote object (data file and each index) to revalidate it.
- HTSlib reads open-ended ranges. The proxy fetches missing blocks in growing runs (1 to 64 blocks) as the reader consumes them. Exact byte counts for BAM/FASTA vary a little with timing. Assertions therefore compare modes and never use exact counts for these readers.
- Concurrent misses for the same block are fetched twice; there is no request coalescing.
- API freshness: entries live at most `ttl_s` or the upstream's remaining freshness, with no conditional revalidation. Without upstream freshness headers, `ttl_s` is a heuristic.
- With Streamable HTTP, all sessions share the process cache. The server has a single bearer token and user.
- Response age includes now − `Date`, so a local clock running well ahead of an upstream server shortens or disables reuse. This fails closed.

## Benchmark

```sh
uv run python scripts/benchmark_cache.py --samples 5 --out cache-local.json
uv run python scripts/benchmark_cache.py --live-only --samples 3 --out cache-live.json   # opt-in
```

The script builds deterministic synthetic files: a 2 Mb contig as bigWig, BAM (1 read/100 bp), VCF.gz, FASTA and BED.gz, about 4 MiB in total. It serves them from the counting range server in `tests/storage/support.py` and runs each workload through `GenomicsService.call`, the path MCP tools take. Modes: `disabled` (cache off), `cold` (new server per sample), `warm` (same query again on a primed server) and neighbour interval B (`chrP:1500000-1510000`) after a warm interval A (`chrP:100000-110000`), against B cold. The neighbour case is the one a whole-result cache cannot serve. API workloads replay recorded responses through a counting mock transport. The command fails if any result digest differs across modes, for the neighbour interval, or for four concurrent warm calls, for both file and API workloads. CI runs the same workloads once (`tests/cache/test_range_cache.py`) and asserts only relationships (warm < cold, neighbour-after-warm < neighbour-cold), never wall-clock times.

## Results, 2026-09-28 (macOS arm64, Python 3.12.14, pysam 0.24.1 / samtools 1.24, pyBigWig 0.3.26)

### Local controlled fixtures

`demos/results/2026-09-28/cache-local.json`, 5 samples per mode. Cells show upstream requests / KiB served by the counting server; bytes are from the first sample. Every workload returned identical result digests with the cache off, cold and warm, and for four concurrent warm calls. Interval B returned the same digest after warm A as on a cold server. Timings are dominated by the per-call reader process (about 300 ms) and are diagnostic only. After the review fixes a one-sample rerun gave identical digests and the same request/byte counts for signal, variants and features. BAM and FASTA counts varied within the read-ahead variation described above. The fixture server sends none of the headers the fixes act on, so this file was not regenerated.

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

`demos/results/2026-09-28/cache-live.json`, run with `--live-only --samples 3` after the review fixes, finished 13:49 UTC. An earlier run at 13:23 UTC, before the fixes, gave the same results and similar timings. ENCODE ENCFF792QDS (GRCh38 bigWig, 1.4 GB, public S3), `get_signal` with 10 bins:

| Region | Cache off (ms) | Cache on (ms) |
| --- | --- | --- |
| chr1:1000000-1001000, 3 repeats | 5122, 4871, 4844 | 4259 (cold), 832, 550 |
| chr1:1001000-1002000 (adjacent) | 4891 | 508 |
| chr1:5000000-5001000 (distant) | 4961 | 1178 |

Results were identical with the cache on and off. The chr1:1000000-1001000 mean was 26.361254017233847, the value in the published ENCODE demo. The S3 block responses passed the per-response checks, and the cached server fetched 704 KiB into 11 blocks for all five calls.

Live HGNC answered `Cache-Control: no-cache, no-store`, so `resolve_identifier` was correctly not cached: 175 ms, then 57 ms, both from upstream. The earlier run's repeat took 5038 ms; the cause was not investigated. UniProt (`max-age=43200`) `lookup_protein` P51587 took 107 ms, then 3 ms from the cache. Both calls reported the same `retrieved_at`, and the repeat carried the `cache:` warning. Other providers were not run live for this change.
