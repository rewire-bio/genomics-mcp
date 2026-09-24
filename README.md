# Genomics MCP

<!-- mcp-name: io.github.rewire-bio/genomics-mcp -->

An MCP server for finding genomic datasets, retrieving bounded data at a locus, and looking up versioned, source-attributed reference evidence.

**Status: in development. Not released.** Version 0.1.0 is not on PyPI or any MCP registry yet. The core server, configuration, limits and authentication work. Data, archive and reference tools are listed but return `unsupported` until their adapters are added. `list_sources` and `genomics://capabilities` show what the running build supports.

Scope: [PRD.md](PRD.md). Design and adapter contracts: [docs/architecture.md](docs/architecture.md). Review of the original draft: [docs/prd-review.md](docs/prd-review.md).

## What it will do

- Discovery: EGA, ENA, ENCODE, GEO, NCBI Datasets studies, datasets, samples, phenotypes (as supplied) and files.
- Genomics: reads, coverage, pileup, variants, sequence, features and signal from indexed BAM/CRAM/VCF/BCF/FASTA/BED/GFF/GTF/bigWig/bigBed, on local disk, HTTPS, or S3.
- Transfers: bounded downloads to a local work directory with checksums.
- Reference: HGNC, Ensembl, ClinVar, gnomAD, UniProt, Open Targets, and optional AlphaGenome Atlas precomputed predictions.

Intervals are 0-based half-open with an explicit assembly. Nothing is lifted over silently. Defaults: 1 Mb region, 10,000 records, 1 MiB response, 30 s deadline, 100 MiB transfer. Research use only; no clinical verdicts.

## Install (from source)

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). macOS and Linux; Windows via WSL2.

```sh
git clone https://github.com/rewire-bio/genomics-mcp
cd genomics-mcp
uv sync --locked
uv run genomics-mcp --help
```

## Use

### stdio (default)

Example client entry (Claude Desktop, Claude Code and similar):

```json
{
  "mcpServers": {
    "genomics": {
      "command": "uv",
      "args": ["--directory", "/path/to/genomics-mcp", "run", "genomics-mcp"],
      "env": { "GENOMICS_MCP_ALLOWED_ROOTS": "/path/to/your/data" }
    }
  }
}
```

### Streamable HTTP

HTTP needs a bearer token of at least 32 characters and binds to 127.0.0.1 unless configured otherwise.

```sh
export GENOMICS_MCP_HTTP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run genomics-mcp --transport http --port 8765
# endpoint: http://127.0.0.1:8765/mcp  header: Authorization: Bearer $GENOMICS_MCP_HTTP_TOKEN
```

### Configuration

Copy [config.example.toml](config.example.toml) and pass it with `--config` or `GENOMICS_MCP_CONFIG`. `genomics-mcp --check-config` prints a summary without secrets.

- Local files are readable only under `paths.allowed_roots`.
- Ambient cloud credentials (`AWS_*`, `~/.aws`, instance metadata) are never used. For private S3 or MinIO, add a `[storage.profiles.<name>]` entry that names the environment variables holding your keys.
- Values derived from files you have not marked `public` are sent to external APIs only when a call sets `allow_external_annotation`.

## Tools

| Group | Tools |
| --- | --- |
| Discovery | `list_sources`, `search_datasets`, `describe_dataset`, `list_files`, `list_samples`, `get_sample_metadata` |
| Transfers | `fetch_file`, `get_transfer_status`, `cancel_transfer` |
| Genomics | `get_reads`, `get_coverage`, `get_pileup`, `get_variants`, `get_sequence`, `get_features`, `get_signal` |
| Composition | `inspect_locus`, `compare_samples` |
| Reference | `resolve_identifier`, `normalize_variant`, `lookup_variant`, `lookup_gene`, `lookup_protein` |

Resources: `genomics://capabilities`, `genomics://status`, `genomics://schemas`, `genomics://schemas/{name}`.

## Development

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Tests use synthetic data and local subprocesses only. They do not need network access or cloud accounts.

## Licence

MIT. See [LICENSE](LICENSE). Data from each source is subject to that source's own terms.
