# Archive and catalog sources (E6/E7)

Status: standalone source clients, tests and live checks are done. MCP provider registration and transfer-manager wiring are **not done yet**; see [Remaining integration](#remaining-integration). Do not merge this branch into a registry that loads `genomics_mcp.archives`/`genomics_mcp.catalogs` until `register(registry)` exists. The core loader raises `TypeError` for a provider package without it.

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

Offline (mocked HTTP and synthetic BAM/ZIP fixtures): `tests/archives`, `tests/catalogs`. Run:

```sh
env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
  AWS_EC2_METADATA_DISABLED=true AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
  PYTHONPATH=src .work/venv/bin/python -m pytest -q tests/archives tests/catalogs
```

The conftests also remove cloud variables and disable instance metadata for every test. Live tests are opt-in: `GENOMICS_MCP_LIVE=1`. EGA tests also need `GENOMICS_MCP_EGA_PYEGA3_CONFIG`, a directory holding pyega3's public `default_server_file.json` and `default_credential_file.json`.

## Live evidence (run 2026-09-24T23:47Z, `-m live`, 13 passed)

- **EGA `get_region`:**
  - Input: EGAF00007243773, chr10:[10000,10050), GRCh38, via the public test account.
  - Output: a 121,225-byte BAM, sha256 `3305e420…c686211`.
  - Header: 3,366 references, `AS:GRCh38`.
  - Records: 91 decoded and 42 overlapping.
- **EGA `fetch_file`:** the same file, 194,821 bytes, MD5 `ed365c71461eac21a64d2c29e7216e50` verified, 1,097 alignments.
- **EGA `check_file`:** ready, with index `ega://EGAF00007243782`.
- **EGA denial:** an htsget request for EGAF00000077618 returned `unauthorized` (HTTP 403) and wrote no artifact.
- **ENA `fetch_file`:**
  - Input: ERR10043599 submitted `I17622.MT.bam` (10,287 bytes) plus `.bai`.
  - Both MD5s were verified, the range check reported ready, and pysam counted 198 reads on MT.
  - The FASTQ is `not_locus_ready`.
- **ENA `fetch_sequence_fasta`:** DQ285577 resolved to DQ285577.1, 614 bases, with a `.fai`.
- **ENA SRA resolution:** SRP000001 resolved to PRJNA33627.
- **ENCODE ENCFF001JBR:** mm9, 16,438,476 bytes, in ENCSR000BZH; range + bigBed magic gave ready.
- **ENCODE ENCFF792QDS:** GRCh38, 1,413,106,336 bytes, in dataset (annotation) ENCSR901HTN; ready without downloading.
- **ENCODE ENCSR000BZH:** two biosamples.
- **GEO GSM9343150:** the characteristics include antibody H3K4me3; its supplementary bigWig is ready over HTTPS range.
- **NCBI Datasets:**
  - GCF_000001405.40 is GRCh38.p14, with paired accession GCA_000001405.29.
  - phiX GCF_000819615.1 was prepared: the ZIP was MD5-verified against md5sum.txt, NC_001422.1 (5,386 bp) extracted, and a `.fai` built.

These are live observations, not guarantees. EGAF00001770107 (the BAM paired with the suggested index EGAF00001775036) returned HTTP 500 from htsget. Not every EGA test file is operational.

## Remaining integration

1. Add `register(registry)` to `genomics_mcp.archives` and `genomics_mcp.catalogs`: source handlers for the six discovery operations, `SourceInfo` entries (terms and auth mode), and an `ega://` resolver whose region path calls `EgaClient.get_region` with the caller's explicit interval.
2. Map `SourceError` to core `GenomicsError` by code. Convert models to core models. Put records under `OperationOutput.data.records`, and call core `register_secret` for EGA and API-key secrets.
3. Wire `fetch_file` / `prepare_genome_fasta` / `fetch_sequence_fasta` into the E3 transfer manager (budgets, progress, resume, workspace quotas) and core response-size limits.
4. Load EGA credentials from explicit core configuration only.
5. Verify real MCP round-trips (stdio and Streamable HTTP) for EGA region, ENA download and ENCODE metadata before calling E6/E7 complete.
6. Not yet populated: `Sample.phenotype_files`. No source client discovers phenotype files, and none are invented.
