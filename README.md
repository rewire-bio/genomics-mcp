# Genomics MCP

<!-- mcp-name: io.github.rewire-bio/genomics-mcp -->

An MCP server for finding genomic datasets, retrieving bounded data at a locus, and looking up versioned, source-attributed reference evidence. It runs on your machine. Research use only; no clinical verdicts.

**Status: 0.1.0, release prepared, not yet published.** All 23 tools are implemented and tested. The first release will be a GitHub release and a GHCR container image. Current publication state: [docs/registry-ledger.md](docs/registry-ledger.md).

## What it does

- **Discovery:** EGA, ENA, ENCODE, GEO and NCBI Datasets studies, datasets, samples, phenotypes (as the archive supplies them) and files.
- **Genomics:** reads, coverage, pileup, variants, sequence, features and signal from indexed BAM/CRAM, VCF/BCF, FASTA, BED/GFF3/GTF and bigWig/bigBed files on local disk, HTTPS or S3. EGA regions come through EGA's htsget.
- **Transfers:** bounded, resumable, checksummed downloads to a local workspace. Files are returned as paths, never as bytes in MCP text.
- **Composition:** `inspect_locus` and `compare_samples` across several files.
- **Reference:** HGNC, Ensembl, ClinVar (germline, somatic clinical impact and oncogenicity kept separate), gnomAD, UniProt, Open Targets, and optional AlphaGenome Atlas precomputed predictions with your own key.

Intervals are 0-based half-open with an explicit assembly. Nothing is lifted over silently. CRAM needs a reference whose MD5 matches the header. Default limits: 1 Mb region, 10,000 records, 1 MiB response, 30 s deadline, 100 MiB transfer.

## Install

Full options, platform notes and Windows (WSL2/container): [docs/install.md](docs/install.md).

### Container (linux/amd64)

After the release is published:

```sh
docker run --rm -i \
  --mount type=bind,source="$HOME/genomics-data",target=/data,readonly \
  --mount type=volume,source=genomics-mcp-work,target=/work \
  ghcr.io/rewire-bio/genomics-mcp:0.1.0
```

Only `/data` is readable as local input; `/work` holds downloads and indexes (bounded to 10 GiB).

### Python (macOS, Linux)

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), a C compiler, and libcurl and zlib development files. pyBigWig is built from source because the published Linux wheel has no remote-file support.

```sh
git clone https://github.com/rewire-bio/genomics-mcp
cd genomics-mcp
uv sync --locked --no-dev
uv run genomics-mcp --check-config
```

## Use

### stdio (default)

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

For the container, use `"command": "docker"` with the `run` arguments above.

### Streamable HTTP

HTTP needs a bearer token of at least 32 characters and binds to 127.0.0.1 unless configured otherwise. There is no hosted service.

```sh
export GENOMICS_MCP_HTTP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run genomics-mcp --transport http --port 8765
# endpoint: http://127.0.0.1:8765/mcp  header: Authorization: Bearer $GENOMICS_MCP_HTTP_TOKEN
```

### Configuration

Copy [config.example.toml](config.example.toml) and pass it with `--config` or `GENOMICS_MCP_CONFIG`. `genomics-mcp --check-config` prints a summary without secrets.

- Local files are readable only under `paths.allowed_roots`.
- Ambient cloud credentials (`AWS_*`, `~/.aws`, instance metadata) are never used. For private S3 or MinIO, add a `[storage.profiles.<name>]` entry that names the environment variables holding your keys.
- EGA controlled files need your own EGA account. `GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1` uses EGA's documented public test account.
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

## Evidence

Five clean-install demonstrations with real data ran on 2026-09-25: an EGA public-test BAM region, an ENA sequence artifact, an ENCODE bigWig signal, a reference base plus ClinVar evidence, and a synthetic BAM on local MinIO. Commands and machine-readable results: [docs/demos.md](docs/demos.md).

## Documentation

[PRD.md](PRD.md) (scope and limits) · [docs/architecture.md](docs/architecture.md) · [docs/data-access.md](docs/data-access.md) · [docs/archive-sources.md](docs/archive-sources.md) · [docs/reference-sources.md](docs/reference-sources.md) · [docs/composition.md](docs/composition.md) · [docs/install.md](docs/install.md) · [docs/release.md](docs/release.md) · [SECURITY.md](SECURITY.md)

## Development

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Default tests use synthetic data and local subprocesses only. Live-source and MinIO tests are opt-in (see the test modules). samtools/bcftools oracle tests run when those tools are installed.

## Licence

MIT. See [LICENSE](LICENSE). Data from each source is subject to that source's own terms.
