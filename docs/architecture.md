# Architecture and contracts

Package `genomics_mcp` (distribution `rewire-genomics-mcp`). This file is the contract for epics E2–E9. If a contract must change, change it here and in the core module together.

## Modules

| Module | Role | Owner |
| --- | --- | --- |
| `errors` | `ErrorCode`, `ErrorInfo`, `GenomicsError` and subclasses, `redact`, `redact_url`, `redact_obj`, `register_secret` | E1 |
| `models` | `Interval`, `VariantSpec`, `FileRef`, `Study`, `Dataset`, `Sample`, `Reference`, `EvidenceRecord`, `Provenance`, `SourceStatus`, `Page`, `TransferJob`, `LocalArtifact`, `infer_format` | E1 |
| `config` | `Settings`, `Limits`, `S3Profile`, `load_settings`, `validate_http_security` | E1 |
| `security` | `resolve_local_path`, `scrubbed_env` | E1 |
| `result` | `ToolResult`, `OperationOutput`, `Truncation`, `EffectiveLimits`, `check_region`, `take_records`, `fit_to_response_budget` | E1 |
| `public` | `PublicHttpClient`, `SourcePolicy`, `Deadline`, `EgressContext`, `PublicResponse` | E1 |
| `registry` | `Operation`, `Category`, `OPERATIONS`, `Registry`, `SourceInfo`, `PROVIDER_MODULES` | E1 |
| `contracts` | `FileResolver` protocol, `ResolvedFile` | E1 |
| `context` | `OperationContext`, `fan_out`, `FanOutResult` | E1 |
| `requests` | one Pydantic request model per operation | E1 |
| `service` | validation, limits, dispatch, envelopes, `list_sources` | E1 |
| `server` | MCP tools/resources (`MCPServer` from the official SDK 2.x) | E1 |
| `auth`, `logs`, `cli`, `catalog` | bearer middleware, redacted stderr logging, CLI, planned source list | E1 |

Core imports form a DAG: `errors` and `registry` import no other core module at runtime; `service` sits above everything except `server` and `cli`. Providers import core modules only, never `service`, `server` or another epic's provider (share through `registry.provide`/`register_resolver`).

## Plugging in an epic

Each epic owns one provider package. It is imported at start-up if present; a missing package is reported as `not installed`, and any other import error fails start-up.

| Module | Epic | Registers |
| --- | --- | --- |
| `genomics_mcp.storage` | E2 | resolvers `file`, `http`, `https`, `s3`, `ftp`; `list_files` for `local` if useful |
| `genomics_mcp.artifacts` | E3 | `fetch_file`, `get_transfer_status`, `cancel_transfer` (`default`); `get_sequence/fasta`; `get_features/bed,gff3,gtf` |
| `genomics_mcp.readers` | E4 | `get_reads`, `get_coverage`, `get_pileup` for `bam`, `cram` (`sam` optional); `get_variants` for `vcf`, `bcf` |
| `genomics_mcp.signal` | E5 | `get_signal/bigwig`; `get_features/bigbed` |
| `genomics_mcp.archives` | E6 | discovery ops for `ega`, `ena`; resolvers `ega`, `htsget` |
| `genomics_mcp.catalogs` | E7 | discovery ops for `encode`, `geo`, `ncbi_datasets` |
| `genomics_mcp.evidence` | E8 | reference ops per source (`hgnc`, `ensembl`, `clinvar`, `gnomad`, `uniprot`, `open_targets`, `alphagenome_atlas`) or a `default` planner |
| `genomics_mcp.composition` | E9 | `inspect_locus`, `compare_samples` (`default`) |

```python
# genomics_mcp/readers/__init__.py
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.requests import ReadsRequest
from genomics_mcp.context import OperationContext
from genomics_mcp.result import OperationOutput, take_records


async def get_reads_bam(req: ReadsRequest, ctx: OperationContext) -> OperationOutput:
    resolved = await ctx.resolve_file(req.file)  # E2 resolver; raises on failure
    # _fetch uses pysam and take_records(iterator, max_records) -> (records, truncation)
    records, truncation = await ctx.run_blocking(_fetch, resolved, req, ctx.limits.max_records)
    return OperationOutput(data={"records": records}, truncation=truncation, provenance=[...])


def register(registry: Registry) -> None:
    registry.register(Operation.GET_READS, "bam", get_reads_bam, provider=__name__)
```

### Registry API

- `register(operation, key, handler, *, provider, description="")`. Keys: source name for discovery; file format for genomics (must be one of the operation's accepted formats); `"default"` for transfers and composition; source name or `"default"` (planner) for reference ops. Duplicates raise.
- `register_resolver(scheme, resolver, *, provider)`: one resolver per URI scheme.
- `register_source(SourceInfo(...))`: replaces the planned entry in `catalog.py` with current details (terms, auth, notes).
- `provide(name, component)` / `component(name)`: shared long-lived objects (e.g. a transfer manager).
- `on_shutdown(async_callback)`: closed by the CLI after the transport stops.

### Dispatch rules (service)

| Dispatch | Operations | Key |
| --- | --- | --- |
| builtin | `list_sources` | — |
| source | `search_datasets`, `describe_dataset`, `list_files`, `list_samples`, `get_sample_metadata` | `request.source` (lower-cased); disabled sources return `unsupported` |
| format | genomics ops | `file.format` or inferred from the name. Wrong format → `invalid_input`; interval over `max_region_bp` → `budget_exceeded` (checked before the handler runs) |
| default | transfers, composition | `"default"` |
| fanout | reference ops | `"default"` planner if registered; else each requested source concurrently via `fan_out`, `data = {"by_source": {source: data}}` |

Missing handler → `unsupported` with `hint` naming the planned epic. The whole dispatch runs under the interactive deadline.

### Handler contract

`async def handler(request: <RequestModel>, ctx: OperationContext) -> OperationOutput`

- Return actual records under `data["records"]` (list) so response-size trimming works, or a dict/model for single entities. Put pydantic models in `data` directly or as dicts; the envelope serializes with `mode="json"`.
- Raise a `GenomicsError` subclass for failures: `NotFoundError`, `UnauthorizedError`, `UnsupportedError`, `InvalidInputError`, `PreparationRequiredError`, `UpstreamError`, `DeadlineExceededError`, `BudgetExceededError`, `ConsentRequiredError`. Do not return empty data for a failure.
- For partial results (e.g. one of several files failed), return data plus `errors=[ErrorInfo...]`; status becomes `partial`.
- Enforce `ctx.limits.max_records` with `take_records`, and set `truncation`. The service then enforces `max_response_bytes`.
- Blocking libraries (pysam, pyBigWig, boto3) run through `ctx.run_blocking`. It returns `timeout` at the deadline, but the thread keeps running, so read bounded amounts.
- Every record set carries `Provenance` (source, redacted URL, method, version, transformations).

### OperationContext

Fields: `operation`, `settings`, `limits` (`EffectiveLimits`: `max_region_bp`, `max_records`, `max_response_bytes`, `timeout_s`), `deadline` (`Deadline`), `registry`, `http` (`PublicHttpClient`), `request_id`, `allow_external_annotation`.
Methods: `resolve_file(FileRef) -> ResolvedFile`, `resolve_local_path(path)`, `check_region(interval)`, `run_blocking(fn, *args)`, `egress_for(files) -> EgressContext`, `child(timeout_s)`, `component(name)`, `require_component(name)`. Module function `fan_out(ctx, {name: async fn(ctx) -> OperationOutput}, timeout_s=None) -> FanOutResult(outputs, statuses, errors)` isolates each task's failure or timeout.

### FileResolver (E2, E6)

```python
class FileResolver(Protocol):
    async def resolve(self, file: FileRef, ctx: OperationContext) -> ResolvedFile: ...
    async def stat(self, file: FileRef, ctx: OperationContext) -> FileRef: ...
```

`ResolvedFile`: `file`, `open_uri`, `index_open_uri`, `reference_open_uri`, `local_path`, `range_capable` (True only when verified), `readiness`, `expires_at`. `open_uri` may be a signed URL: never log or return it; report `file.display_uri()`.
Resolvers must: call `ctx.resolve_local_path` for local files (allowed roots, symlink-safe); use `settings.s3_profile(name)` / `settings.s3_credentials(name)` for private S3 and unsigned requests for public S3; never create a default boto3 session or let HTSlib read `AWS_*`/`~/.aws`; refuse requester-pays unless the profile enables it; never guess an index or CRAM reference that is not explicitly given or source-asserted.

### Public HTTP (E6–E8)

```python
POLICY = SourcePolicy(
    name="ena",
    base_url="https://www.ebi.ac.uk/ena/portal/api",
    requests_per_minute=...,
    terms_url=...,
)
data, prov = await ctx.http.get_json(
    POLICY, "search", params=..., deadline=ctx.deadline, egress=EgressContext.public()
)
```

`request(policy, method, path_or_url, *, deadline, egress, params, headers, json_body, idempotent, max_response_bytes, record_id)`. Hosts are restricted to the policy base URL host plus `allowed_hosts`. GET/HEAD retry on 429/502/503/504 and connection errors; POST retries only with `idempotent=True`. 401/403 → `unauthorized`, 404 → `not_found`, other → `upstream_error`. Responses over `max_response_bytes` (default 16 MiB) → `budget_exceeded`. `egress` is required: use `ctx.egress_for(files)` whenever query values come from files, so private-derived queries fail with `consent_required` unless the caller set `allow_external_annotation`.
Config (`[sources.<name>]`) can disable a source, override `base_url`, timeout and concurrency, and lower (never raise) the rate.

### Result envelope

`ToolResult{schema_version, operation, status, data, error, errors, source_status, provenance, warnings, truncation, limits}`. MCP responses carry it as structured content and as JSON text, with `isError` true when `status == "error"`. Argument-schema violations detected by the SDK before the service runs (e.g. `end <= start`) return an MCP tool error with the validation text instead of an envelope.

## Transports and auth

- stdio: stdout is protocol only; logs go to stderr.
- Streamable HTTP: `genomics-mcp --transport http`. Requires `GENOMICS_MCP_HTTP_TOKEN` (or `http.token_file`), 32+ characters. `BearerAuthMiddleware` wraps the whole ASGI app (constant-time digest compare; 401 with `WWW-Authenticate: Bearer`). DNS-rebinding protection limits `Host`/`Origin`. Non-loopback hosts need `http.allow_non_loopback = true`; put TLS in front.
- The service outlives HTTP sessions; the CLI closes it (and runs `on_shutdown` callbacks) after the transport stops.

## Configuration

TOML (`--config` or `GENOMICS_MCP_CONFIG`) plus environment: `GENOMICS_MCP_HOST`, `GENOMICS_MCP_PORT`, `GENOMICS_MCP_HTTP_TOKEN`, `GENOMICS_MCP_HTTP_TOKEN_FILE`, `GENOMICS_MCP_WORK_DIR`, `GENOMICS_MCP_ALLOWED_ROOTS` (path-separated), `GENOMICS_MCP_LOG_LEVEL`. Precedence: CLI > environment > TOML > defaults. Unknown keys are errors. Secrets are referenced by environment variable name; `AWS_*` names are refused. See `config.example.toml`.

## Wiring TODO

Tracked until each epic lands; update the capability test when an item is done.

| Item | Epic | State |
| --- | --- | --- |
| Storage resolvers file/http(s)/s3/ftp; readiness; dummy ambient AWS proof | E2 | not started |
| Transfers, budgets, resume, checksum; reference prep; FASTA; BED/GFF/GTF | E3 | not started |
| BAM/CRAM/VCF/BCF readers and CRAM reference checks | E4 | not started |
| bigWig/bigBed | E5 | not started |
| EGA/ENA discovery, EGA htsget/ega resolvers | E6 | not started |
| ENCODE/GEO/NCBI Datasets | E7 | not started |
| Reference adapters and normalization; source `terms_url` confirmation | E8 | not started |
| `inspect_locus`, `compare_samples` | E9 | not started |
| Server `server.json`, container, release | E10–E11 | not started |
