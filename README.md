# Genomics MCP

<!-- mcp-name: io.github.rewire-bio/genomics-mcp -->

[![Release](https://img.shields.io/github/v/release/rewire-bio/genomics-mcp)](https://github.com/rewire-bio/genomics-mcp/releases/tag/v0.1.0)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.rewire--bio%2Fgenomics--mcp-blue)](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.rewire-bio%2Fgenomics-mcp/versions/0.1.0)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](docs/install.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Let your AI assistant fetch real genomic data and cite where it came from.**

Genomics MCP is a local [Model Context Protocol](https://modelcontextprotocol.io) server. It lets an agent go from a public dataset to a bounded slice of real data at a locus, then to versioned reference evidence. It does not write glue code or download whole files, and it does not guess coordinates. Every result carries accessions, versions and retrieval dates.

It is a research tool. It retrieves and reports data. It does not diagnose, classify variants clinically, call variants or draw biological conclusions.

## Who it helps

- **Computational biologists** who want an agent to pull a region from an archive, a URL or their own indexed files, without writing integration code.
- **Researchers** asking questions about a locus, gene or variant who need the answer tied to named sources and versions.
- **Agent and evaluation builders** who need cited, reproducible access to real data. Each record has an accession, version, retrieval date and 0-based half-open coordinates on an explicit assembly, so questions built from it can be traced back to the source. The server does not generate benchmarks itself.

## What you can ask

These prompts are illustrative. What happens depends on your MCP client and model. The EGA example uses EGA's documented public test account, which you enable with `GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1`.

- "Find the files in EGA dataset EGAD00001003338 and show me the reads in GRCh38 chr10:10,000–10,050 from the BAM file."
- "What is the mean ENCODE signal of ENCFF792QDS over chr1:1,000,000–1,001,000?"
- "Normalize BRAF c.1799T>A on GRCh38 and show the ClinVar records, keeping germline, somatic and oncogenicity classifications separate."
- "Compare genotypes at this locus across these two VCFs in my data folder."
- "Download the ENA FASTA for DQ285577.1 and give me its first 30 bases."

## A measured example

From the [clean-install demonstrations](docs/demos.md) (2026-09-25, live data). The file record came from `list_files`; it is abridged here.

```json
{
  "tool": "get_signal",
  "arguments": {
    "file": {"uri": "https://encode-public.s3.amazonaws.com/…/ENCFF792QDS.bigWig", "format": "bigwig", "assembly": "GRCh38"},
    "interval": {"contig": "chr1", "start": 1000000, "end": 1001000, "assembly": "GRCh38"}
  }
}
```

Result: `"summary": {"type": "mean", "value": 26.361254017233847, "exact": true}`. The value came from HTTP range reads of a 1,413,106,336-byte bigWig; the workspace held 0 bytes afterwards. That is bytes written to disk, not total network traffic. The other demonstrations cover an EGA public-test BAM region (91 records received, 42 overlapping), an ENA sequence artifact, a NC_000007.14 reference check with ClinVar VCV000013961, and a synthetic BAM on local MinIO.

## Quickstart

Choose one route. Details, checksums, Windows (WSL2) and troubleshooting: [docs/install.md](docs/install.md).

### Container (Linux x86_64, Docker)

```sh
docker pull ghcr.io/rewire-bio/genomics-mcp:0.1.0
```

Add to your MCP client configuration, replacing the data path:

```json
{
  "mcpServers": {
    "genomics": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--mount", "type=bind,source=/path/to/your/data,target=/data,readonly",
        "--mount", "type=volume,source=genomics-mcp-work,target=/work",
        "ghcr.io/rewire-bio/genomics-mcp:0.1.0"
      ]
    }
  }
}
```

The image is linux/amd64 only. It has been tested on Linux x86_64; running it on Apple silicon under emulation is untested.

### From source (macOS arm64, Linux x86_64)

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), a C compiler, and libcurl and zlib development files. pyBigWig is built from source because its published Linux wheel cannot read remote files.

```sh
git clone https://github.com/rewire-bio/genomics-mcp
cd genomics-mcp
git checkout v0.1.0
uv sync --locked --no-dev
uv run --no-dev genomics-mcp --check-config
```

```json
{
  "mcpServers": {
    "genomics": {
      "command": "uv",
      "args": ["--directory", "/path/to/genomics-mcp", "run", "--no-dev", "genomics-mcp"],
      "env": { "GENOMICS_MCP_ALLOWED_ROOTS": "/path/to/your/data" }
    }
  }
}
```

There is no PyPI package yet. The release also has a wheel, an MCPB bundle for Linux, and a pinned `uvx` Git command; see [docs/install.md](docs/install.md).

## What it covers

| Area | Sources and formats |
| --- | --- |
| Discovery | EGA, ENA (incl. SRA accessions), ENCODE, GEO, NCBI Datasets: studies, datasets, samples, phenotypes as the archive supplies them, files |
| Genomic data | BAM/CRAM, VCF/BCF, FASTA, BED/GFF3/GTF, bigWig/bigBed on local disk, HTTPS or S3; EGA regions via htsget |
| Transfers | Bounded, resumable, checksummed downloads returned as local paths |
| Reference evidence | HGNC, Ensembl, ClinVar, gnomAD, UniProt, Open Targets; optional AlphaGenome Atlas with your own key |

<details>
<summary>All 23 tools and the resources</summary>

| Group | Tools |
| --- | --- |
| Discovery | `list_sources`, `search_datasets`, `describe_dataset`, `list_files`, `list_samples`, `get_sample_metadata` |
| Transfers | `fetch_file`, `get_transfer_status`, `cancel_transfer` |
| Genomics | `get_reads`, `get_coverage`, `get_pileup`, `get_variants`, `get_sequence`, `get_features`, `get_signal` |
| Composition | `inspect_locus`, `compare_samples` |
| Reference | `resolve_identifier`, `normalize_variant`, `lookup_variant`, `lookup_gene`, `lookup_protein` |

Resources: `genomics://capabilities`, `genomics://status`, `genomics://schemas`, `genomics://schemas/{name}`. Every tool returns the same envelope: status, data, per-source errors, provenance, truncation and applied limits.

</details>

## How it behaves

- **Explicit coordinates:** 0-based half-open intervals on a named assembly. No silent liftover. CRAM needs a reference whose MD5 matches the header.
- **Bounded by default:** 1 Mb region, 10,000 records, 1 MiB response, 30 s deadline, 100 MiB transfer. Truncation is always reported. Larger transfers need an explicit budget.
- **Local and read-only:** stdio by default, or Streamable HTTP with a bearer token on 127.0.0.1. There is no hosted service. Sources are never modified, and local reads are limited to the folders you allow.
- **Your credentials only when named:** ambient AWS credentials are never used. Private S3/MinIO and EGA access need explicit configuration.
- **Consent for egress:** values from files not marked `public` go to external APIs only when a call sets `allow_external_annotation`.

Configuration: copy [config.example.toml](config.example.toml) and pass `--config`. `genomics-mcp --check-config` prints a summary without secrets.

## Status

Version 0.1.0 is [released on GitHub](https://github.com/rewire-bio/genomics-mcp/releases/tag/v0.1.0). It is published as a public GHCR image and listed in the [official MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.rewire-bio%2Fgenomics-mcp/versions/0.1.0). Directory listings and PyPI: [publication ledger](docs/registry-ledger.md). Scope and known limits: [PRD.md](PRD.md).

## Documentation

- [Installation and platforms](docs/install.md)
- [Clean-install demonstrations](docs/demos.md)
- [Data access: storage, transfers, readers](docs/data-access.md)
- [Archive sources](docs/archive-sources.md)
- [Reference sources](docs/reference-sources.md)
- [Composition tools](docs/composition.md)
- [Architecture and contracts](docs/architecture.md)
- [Release procedure](docs/release.md)
- [Security](SECURITY.md)

## Development

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Default tests use synthetic data and local subprocesses. Live-source and MinIO tests are opt-in.

## Licence

MIT. See [LICENSE](LICENSE). Data from each source is subject to that source's own terms.
