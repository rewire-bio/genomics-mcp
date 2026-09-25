# Archive and catalog sources (E6/E7)

Status (2026-09-25): wired into core. The `genomics_mcp.archives` and `genomics_mcp.catalogs` providers register the five discovery tools for `ega`, `ena`, `encode`, `geo` and `ncbi_datasets`. They also register the region-only `ega://` resolver, the `transfer_backend:ega` component and the `preparer:ncbi_genome_fasta` component. Discovery works end to end over MCP. Retrieval tools (`fetch_file`, `get_reads`, …) belong to E3/E4, which are not in this build, so EGA regions, EGA whole files and genome preparation are reachable today only through the resolver and components below, not through an MCP tool. See [Remaining integration](#remaining-integration).

Code: `src/genomics_mcp/archives/` (EGA, ENA, shared `_common`) and `src/genomics_mcp/catalogs/` (ENCODE, GEO, NCBI Datasets). Every client takes an injected `httpx.AsyncClient`. `_common.http.make_client()` builds one with `trust_env=False`: no proxy variables, no `.netrc`, no ambient credentials, no automatic redirects.

## Operations

| Source | search_datasets | describe_dataset | list_files | list_samples | get_sample_metadata | Retrieval |
|---|---|---|---|---|---|---|
| EGA | EGA accessions only (no free-text search in the API; free text is `unsupported`) | EGAD + studies, policy, DAC (contacts omitted) | EGAD; public or authorised metadata | EGAD/EGAS/EGAF | EGAN | `get_region` (htsget), `fetch_file`, `check_file` |
| ENA (+SRA/DDBJ accessions) | study accession or text | study (resolves SRP/ERP to PRJ) | study/sample/experiment/run/analysis, INSDC sequence | study | sample (all XML attributes) | `fetch_file` (+index), `fetch_sequence_fasta`, `check_file` |
| ENCODE | Experiment/Annotation/... search | ENCSR | ENCSR or ENCFF | ENCSR → replicate → library → biosample | ENCBS/ENCDO | `check_file` (range + magic) |
| GEO | E-utilities db=gds (GSE) | GSE only; `describe_platform` for GPL | GSE (series + per-GSM files) or GSM | GSE | GSM | `check_file` |
| NCBI Datasets | taxon or assembly accession | GCF/GCA (versioned) | genome package contents (ZIP) | the assembly's BioSample, if named | SAMN/SAMEA/SAMD | `prepare_genome_fasta` |

Results use models whose field names and enum values match core `genomics_mcp.models`. Integration converts them with `Core.model_validate(x.model_dump())`, checked against `main` (9209995) for FileRef, Sample, Dataset, Study, Reference, Interval and LocalArtifact. Errors use core error codes: `not_found`, `unauthorized`, `unsupported`, `invalid_input`, `preparation_required`, `upstream_error`, `timeout`, `budget_exceeded`.

## Core integration

- **Handlers** (`archives/_common/handlers.py`):
  - One `DiscoverySource` per source.
  - Records go under `data.records`, converted to core models (`FileRef`, `Dataset`, `Study`, `Sample`, `Reference`). `describe_dataset` returns `{dataset, studies, related}`, and `get_sample_metadata` returns one `Sample`.
  - Source paging is explicit: `data.next_cursor`/`data.total` plus `truncation{reason: "source_page", next_cursor}`.
  - Page size is the lowest of the caller's `max_records`, the configured `max_records` and the source's own maximum. The service then applies `max_response_bytes`.
  - The `formats` filter is applied per page, with a warning when it removes records.
  - The `assembly`/`organism` search filters work only where the source supports them: ENCODE takes both, GEO takes `organism`. Elsewhere they return `unsupported` instead of silently unfiltered results.
- **Errors:** source errors map one-to-one to core codes (`not_found`, `unauthorized`, `unsupported`, `invalid_input`, `preparation_required`, `upstream_error`, `timeout`, `budget_exceeded`). `native` fields are passed through core `redact_obj`.
- **Limits:**
  - Each call's HTTP work is bounded by the call deadline, lowered by `[sources.<name>].timeout_s`.
  - `requests_per_minute` can only lower the built-in spacing; limiters are shared across calls.
  - Disabled sources are refused by the service before any request.
  - Every hop also passes core `check_network_destination`.
  - Clients use `trust_env=False`.
- **`ega://` resolver** (`archives/ega/integration.py`):
  - `resolve_region(file, interval, ctx)` needs `file.format` (bam/cram → htsget reads returned as BAM; vcf/bcf → htsget variants returned as VCF). It checks `ctx.check_region`, the workspace quota (`limits.workspace_max_bytes`) and a region budget of `min(16 MiB, limits.max_transfer_bytes)`.
  - It sends the exact 0-based half-open coordinates and writes a private artifact under `<work_dir>/archives/ega-regions/`.
  - It returns `ResolvedFile(region=<request interval>, local_path, open_uri, readiness=ready)`. The returned `file` gets the slice's own SHA-256/MD5, and the source whole-file checksums move to `native.source_checksums`.
  - `native.region_artifact` records:
    - the provider and endpoint;
    - bytes, checksums and blocks;
    - records received versus overlapping;
    - header evidence, noting that `AS:GRCh38` is a name, not an assembly accession or patch;
    - provenance.
  - Payloads are identified by magic bytes before pysam opens them. A CRAM or anything other than BAM/VCF is refused, so no archive-side htslib ever follows a CRAM header `UR`.
  - A definite assembly or contig mismatch with the header is `invalid_input`, and the artifact is removed.
  - `resolve()` without an interval raises `preparation_required`; it never downloads a whole file.
  - `stat()` uses metadata, plus a header-only htsget ticket when credentials exist.
- **`transfer_backend:ega`** — the minimal stream interface for the E3 transfer manager, in lieu of E3 contracts:

  ```python
  # TransferDescription: plain size_bytes, plain MD5, resumable, safe name, notes; no download
  desc = await backend.describe(file, ctx)
  async with backend.open(file, ctx, start=offset, end=desc.size_bytes) as body:
      async for chunk in body:
          ...  # authorised plain bytes [start, end), 206-checked
  ```

  - EGA v2 metadata `fileSize` is the stored size. The plain size is `fileSize - 16`, as the official pyega3 plain download computes it (`libs/data_file.py`); both values are reported.
  - The bearer token is sent only to the EGA origin.
  - The manager keeps ownership of budget checks (`describe().size_bytes` before any byte), workspace quota, resume (`start`), cancellation (leaving the context closes the stream) and MD5 verification.
  - `EgaClient.fetch_file` and `EnaClient.fetch_file` remain standalone helpers; no MCP path calls them.
- **`preparer:ncbi_genome_fasta`**: `await preparer.prepare(accession, ctx, budget_bytes=None)` returns a core `LocalArtifact` (FASTA + `.fai`).
  - Budget: defaults to `max_transfer_bytes`, is capped by `transfer_budget_ceiling_bytes`, and is shared by the ZIP and the extracted FASTA.
  - The workspace quota is checked before starting, and the whole step runs under the call deadline.
  - `md5sum.txt` is read with a 1 MiB decompressed cap.
  - Cancellation stops the extraction thread, and partial files are removed on any failure.

- **Source switch:** `[sources.<name>].enabled = false` stops source-owned access paths as well as discovery. The EGA resolver (`stat`, `resolve`, `resolve_region`), `transfer_backend:ega` (`describe`, `open`) and `preparer:ncbi_genome_fasta` return `unsupported` before any configuration read, authentication or request. Disabling EGA does not affect other sources or storage schemes.
- **Setup errors:** failures while building a source client keep their typed core code and `source`. For example, an incomplete EGA login (only one of username/password) is `unauthorized` with a hint. An outage of the pinned public-test configuration is `upstream_error` with the HTTP status. Submitted secret values are redacted. Unexpected exceptions are not caught here and still surface as `internal_error`.

## EGA access configuration

Explicit only; read from values core `Settings` already retains:

| Mode | Configuration |
|---|---|
| anonymous (default) | public metadata only |
| bearer token | `[sources.ega] api_key_env = "MY_EGA_TOKEN"` (personal token) |
| personal account | `GENOMICS_MCP_EGA_USERNAME` + `GENOMICS_MCP_EGA_PASSWORD` (pyega3-style password grant) |
| public test account | `GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1` (default off) |

The pyega3 public client secret, and in public-test mode the documented test account, are fetched in memory from the official repository at the pinned commit `5ec6c4cd37cc67142285051bdbd725c824da1384` and checked against recorded SHA-256 hashes. That makes the public demo reproducible from a clean install. `GENOMICS_MCP_EGA_CLIENT_SECRET` overrides the client secret. Nothing is written to disk. Every secret is registered with core redaction, and tokens are cached in memory only. Controlled files keep `visibility: private` even when their metadata is public.

## Source behaviour relied on (checked 2026-09-24/25)

- **EGA public metadata** (`https://metadata.ega-archive.org`, API v3):
  - Lists page with `limit`/`offset` and answer 206 for partial pages.
  - `HEAD` returns `EGA-API-Total-Count`.
  - Unknown accessions answer **200 with an empty body**, reported as `not_found`.
  - There is no free-text search.
  - `num_samples` (6) and the samples endpoint count (2,516) disagree for EGAD00001003338. Both are returned, unreconciled.
- **EGA download API** (`https://ega.ebi.ac.uk:8443/v2`):
  - Auth follows the official pyega3 client: an OpenID password grant with pyega3's public client id.
  - Credentials are only an explicit token, an explicit `EgaPasswordGrant`, or pyega3 config files at paths the caller names (e.g. the documented public test account).
  - Status codes: 401 = invalid authentication, 403 `PermissionDenied`, 404 `NotFound`.
  - Authorised `/metadata/datasets/{id}/files` honours `limit` but ignores `offset`. Pages fetch `offset+limit` rows and slice them, up to 10,000.
  - `/files/{id}?destinationFormat=plain` returns a 206 with an inconsistent Content-Range. Downloads use pyega3's rule: an explicit `Range: bytes=0-(fileSize-16-1)`, then MD5 verification.
- **EGA htsget**:
  - Needs `Accept: application/vnd.ga4gh.htsget.v1.0.0+json` (`application/json` gets 406).
  - Tickets use `urlClass` and `data:base64,<payload>`. Both this and the standard `data:<type>;base64,` form are decoded exactly once, then concatenated.
  - Coordinates are 0-based half-open, passed unchanged.
  - Records are post-filtered by CIGAR-derived overlap. Records received are reported separately from records overlapping the interval.
- **ENA Portal API**:
  - No `offset` parameter, and row order changes with `limit`. Pages are therefore cut from the full sorted accession list (≤10,000), and details come from `POST /search includeAccessions=`.
  - filereport answers `[]` for unknown accessions, so existence is confirmed through the Browser API (404).
  - `ftp.sra.ebi.ac.uk` and `ftp.ebi.ac.uk` paths are served over HTTPS with ranges. Other hosts stay `ftp://` with `download_required`.
- **ENCODE**:
  - `from=` offsets are refused (403), so pages are cut from a sorted accession list.
  - `href` redirects to a short-lived signed S3 URL. `cloud_metadata.url` is the unsigned public object, and it is range-readable anonymously. Signed URLs are never returned, and `azure_uri` (SAS-signed) is dropped.
- **GEO**:
  - `acc.cgi` SOFT for GSE/GSM. Unknown accessions give an HTML "Could not find" page, reported as `not_found`.
  - GPL SOFT lists every sample (13 MB for GPL16791), so platforms use esummary (uid = 100000000 + n, accession re-checked).
  - Supplementary `ftp://ftp.ncbi.nlm.nih.gov` URLs are converted to HTTPS.
  - Contact fields are omitted.
  - Platforms are kept in `native`/`related`, not as reference links.
- **NCBI Datasets v2**:
  - `X-Datasets-Version` is recorded as the source version.
  - Unknown accessions or taxa answer 200 `{}`, reported as `not_found`.
  - Genome downloads are ZIP packages and are listed as `download_required`, never as FASTA.

## Readiness rules

- `ready` is only set after an actual check:
  - an EGA htsget header ticket;
  - an HTTPS 206 range probe, plus bigWig/bigBed magic for ENCODE/GEO;
  - range probes of both file and index for ENA.
- FASTQ is `not_locus_ready`.
- BAM/CRAM/VCF without an index listed by the source is `index_required`.
- `.gz` is `unknown` compression until its content shows BGZF; ordinary gzip is reported as such.
- Index companions are paired only inside one source record (ENA run/analysis, EGA `indexFileId`):
  - The full-name companion (`a.bam.bai`) wins over the stem form (`a.bai`).
  - If a file has more than one candidate, nothing is paired. Unpaired indexes are still returned with their checksums.
- Assemblies come only from structured source fields (EGA @SQ `AS` after retrieval, ENCODE `assembly`, NCBI accessions). ENA and GEO files carry none, and free text is never parsed into one.

## Safety and limits

- **Artifact paths:**
  - Artifacts are written only directly inside the caller's workspace (`_common/workspace.py`).
  - EGA region files are named `<EGAF>.region-<sha256(interval)>.bam`, so contigs are never path components.
  - Source file names are reduced to a safe basename.
  - Symlinked targets are refused.
  - Writes go to exclusive `O_EXCL|O_NOFOLLOW` temporary files (mode 0600), then an atomic rename.
  - Partial or failed files are removed.
- **Budgets:**
  - The region budget (default 16 MiB) counts decoded genomic bytes across all blocks.
  - The ticket JSON is capped at 4/3 of that plus 64 KiB.
  - No block is requested once the budget is exhausted, and a `max_body` of 0 never becomes the default cap.
  - Whole-file downloads default to 100 MiB and are refused before transfer when the reported size exceeds the budget.
  - Region length defaults to at most 1 Mb, and records to at most 10,000. Truncation is reported.
- **Deadlines:** one deadline per client operation covers token, pages, tickets, blocks and parsing. The defaults are 30 s interactive and 600 s transfers. pysam post-filtering threads cannot be interrupted; they only parse bytes already bounded by the budget.
- **Network:**
  - Every source has a host allowlist, applied to redirects too; injected clients are forced to `follow_redirects=False`.
  - Only `https` is used.
  - Loopback, link-local, private and metadata IPs are rejected.
  - `Authorization`/`Cookie` are dropped on cross-origin redirects.
  - The EGA bearer token is sent to htsget blocks only at the ticket's own origin. Ticket headers are filtered to Range/Authorization/Accept/Accept-Encoding.
- **Secrets:**
  - Tokens, passwords, client secrets and API keys are registered for redaction on every construction path.
  - Token-endpoint error bodies are never surfaced; only the HTTP status and a plain OAuth error code are kept.
  - Signed URL parameters and JWTs are redacted.
- **Rate limits:**
  - Requests are spaced per source: EGA 0.2 s, ENA 0.1 s, ENCODE 0.1 s, GEO 0.34 s (0.11 s with an API key), NCBI Datasets 0.21 s (0.11 s with a key).
  - 429/5xx get bounded retries honouring `Retry-After` within the deadline.
- **Assembly mismatch:** a definite mismatch between the caller's assembly and the retrieved header (missing contig, or `AS` tags not containing it) is rejected by default. The artifact is removed. `assembly_policy="warn"` returns the records with a warning instead. No liftover is applied.
- **Checksums:** an MD5 supplied in an htsget ticket is verified against the reconstructed bytes. A whole-file MD5 is never applied to a slice.

## Tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run pytest              # offline, mocked sources
GENOMICS_MCP_NETWORK_TESTS=1 uv run pytest -m network tests/archives tests/catalogs  # live public sources
```

- `test_archive_integration.py` and `test_catalog_integration.py` run the real `GenomicsService`: dispatch, envelopes, paging and truncation, response-byte trimming, error-code mapping, disabled sources, call deadlines, per-source outages, token redaction in outputs and logs, and in-process MCP round-trips. They also exercise the `ega://` resolver via `OperationContext.resolve_file`, the EGA transfer backend (describe, ranged resume, MD5), and the NCBI preparer (manifest bomb, quota/ceiling, deadline cleanup).
- The `network` tests use the real providers through the service, plus one stdio MCP subprocess round-trip with dummy ambient AWS values present.
- The root `tests/conftest.py` scrubs cloud variables and points AWS config at empty files.

## Live evidence (2026-09-25, `-m network`, 10 passed)

- **EGA region:** `ctx.resolve_file(ega://EGAF00007243773, chr10:[10000,10050) GRCh38)` in public-test-account mode, from pinned config, clean settings.
  - Artifact: a 121,225-byte BAM, sha256 `3305e420…c686211`.
  - Records: 91 received, 42 overlapping.
  - Header: 3,366 references, `AS:GRCh38`.
  - The same interval labelled GRCh37 was refused with `invalid_input`.
- **EGA whole file:** through `transfer_backend:ega`, `describe` reported 194,821 plain bytes and MD5 `ed365c71…`. The file was streamed in two ranged requests (resume at 100,000); the MD5 matched and pysam counted 1,097 alignments.
- **EGA access denial:** EGAF00000077618 returned `unauthorized` (HTTP 403).
- **EGA discovery:** `describe_dataset EGAD00001003338` shows it as controlled with policy EGAP00001000598. `list_files` returned 5 private records with `source_page` truncation.
- **ENA sequence:**
  - `list_files ena DQ285577` returns a FileRef for `…/fasta/DQ285577.1` (614 bases per ENA, `download_required`).
  - `fetch_file` currently returns `unsupported`, because E3 is not in this build.
  - The provider's explicit sequence path downloaded 756 bytes; the sequence MD5 was verified against ENA's `sequence_md5`, and the `.fai` shows `DQ285577.1`, 614 bases. This is a sequence record, not an alignment region.
- **ENA SRA resolution:** SRP000001 resolved to PRJNA33627. ERR10043599's submitted BAM is listed with its `.bai`, 10,287 bytes and MD5.
- **ENCODE:**
  - ENCFF792QDS: GRCh38, 1,413,106,336 bytes, bigWig, with its `href`, in annotation ENCSR901HTN.
  - ENCSR000BZH: files are linked to the experiment; two biosamples.
- **GEO:** GSM9343150 characteristics include antibody H3K4me3, and its supplementary file is an HTTPS NCBI URL.
- **NCBI Datasets:** GCF_000001405.40 is GRCh38.p14, INSDC GCA_000001405.29. The phiX packages are `download_required`, and the preparer produced a verified NC_001422.1 (5,386 bp) with `.fai`.
- **stdio MCP:** `list_files` (ENCODE), `describe_dataset` (EGA), `list_files` (ENA DQ285577) and `list_sources` all returned ok. The dummy AWS values never appeared.

These are observations on those dates, not guarantees. EGAF00001770107 returned HTTP 500 from htsget earlier.

## Remaining integration

1. **E3 (transfers):**
   - `fetch_file` for `ega://` must use `registry.component("transfer_backend:ega")` (describe → budget/quota → `open(start, end)` with resume → MD5), never `resolve()`.
   - ENA/ENCODE/GEO HTTPS files and the versioned ENA FASTA record go through E2/E3's HTTPS path.
   - Genome preparation should call `preparer:ncbi_genome_fasta` under E3 workspace accounting.
   - Until then, `fetch_file` is `unsupported`.
2. **E4 (readers):** call `ctx.resolve_file(file, interval=…)` and post-filter the returned bounded artifact. `file.format` in the result is the served format (BAM for a CRAM source). CRAM reference checks stay in E4.
3. **E2 (storage):** resolves ENCODE `href` redirects. FileRefs already carry the unsigned public S3 `uri`.
4. **Core:**
   - `catalog.SOURCE_SCHEMES` now maps `ega` to `("ega",)`, so `list_sources` reports the scheme only while the resolver is registered. This is a narrow core change made with coordinator approval.
   - `list_sources` still cannot report the configured EGA access mode (`SourceInfo` is static); the notes describe the options. Reporting the mode would need a core status hook.
   - The architecture Wiring TODO rows for E6/E7 should be updated by the core owner.
5. **Not implemented:**
   - a generic `htsget://` resolver for arbitrary servers (needs host/auth configuration);
   - `Sample.phenotype_files`: no source client discovers phenotype files, and none are invented.
