# Genomics MCP

<!-- mcp-name: io.github.rewire-bio/genomics-mcp -->

[![Release](https://img.shields.io/github/v/release/rewire-bio/genomics-mcp)](https://github.com/rewire-bio/genomics-mcp/releases/tag/v0.1.0)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.rewire--bio%2Fgenomics--mcp-blue)](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.rewire-bio%2Fgenomics-mcp/versions/0.1.0)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](docs/install.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Let your AI assistant retrieve real genomic data and reference evidence, with the sources it came from.**

Genomics MCP is a local [Model Context Protocol](https://modelcontextprotocol.io) server. An agent can use it to find public datasets and read a bounded region from archive, remote or local files. It can also look up variants and genes in public reference databases. Region queries use an explicit assembly and 0-based half-open coordinates. Where the format and server support range reads, a region is read without downloading the whole file. Whole-file downloads are a separate, budgeted step. Results report which sources were consulted (provenance and per-source status), with accessions, versions and retrieval times where the source provides them.

It is a research tool. It retrieves and reports data. It does not give clinical interpretation, call variants or draw biological conclusions.

## Who it is for

Computational biologists and researchers who want an agent to pull data from EGA, ENA, ENCODE, GEO, NCBI or their own indexed files without writing integration code. It also suits people building agents or evaluations who need real data with traceable sources. It does not generate benchmarks itself.

## Example requests

Illustrative prompts; results depend on your client and model. Intervals are 0-based half-open (start included, end excluded); VCF-style variant positions are 1-based.

- "Show the reads from EGA file EGAF00007243773 (dataset EGAD00001003338) overlapping GRCh38 chr10:[10000, 10050)." Needs `GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1`, EGA's documented public test account.
- "What is the mean signal of ENCODE file ENCFF792QDS over GRCh38 chr1:[1000000, 1001000)?"
- "Check the reference base of GRCh38 variant 7-140753336-A-T against NCBI, then list its ClinVar records with germline, somatic and oncogenicity classifications kept separate."
- "Download the ENA FASTA for DQ285577.1 and show its first 30 bases."
- "Compare genotypes in chr1:[100000, 200000) across the two VCFs in my data folder."

## Measured example

The ENCODE request above, run through a clean install on 2026-09-25 with live data:

- File: ENCODE ENCFF792QDS, GRCh38 bigWig, 1,413,106,336 bytes
- Interval: chr1:[1000000, 1001000)
- Result: exact mean **26.361254017233847**, from HTTP range reads. The workspace held 0 bytes afterwards (nothing written to disk; network reads still happened).

Four other live demonstrations ran with the same install: an EGA test BAM region, an ENA sequence download, a reference check with ClinVar, and local MinIO. Commands and machine-readable results: [docs/demos.md](docs/demos.md).

## Quickstart

### Container (Linux x86_64 with Docker)

The image is linux/amd64. It is tested on Linux x86_64; Docker on macOS is untested. Create the data folder first; it is mounted read-only.

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

### From source (macOS arm64, Linux x86_64)

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), a C compiler, and libcurl and zlib development files (pyBigWig is built from source for remote-file support).

```sh
git clone https://github.com/rewire-bio/genomics-mcp
cd genomics-mcp && git checkout v0.1.0
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

There is no PyPI package yet. Wheel, Linux MCPB bundle, pinned `uvx` command, Windows (WSL2) and platform notes: [docs/install.md](docs/install.md).

## Coverage

| Area | Sources and formats |
| --- | --- |
| Discovery | EGA, ENA (incl. SRA accessions), ENCODE, GEO, NCBI Datasets: studies, datasets, samples, phenotypes as supplied, files |
| Genomic data | BAM/CRAM, VCF/BCF, FASTA, BED/GFF3/GTF, bigWig/bigBed on local disk, HTTPS or S3; EGA regions via htsget |
| Transfers | Budgeted, resumable, checksummed downloads returned as local paths |
| Reference | HGNC, Ensembl, ClinVar, gnomAD, UniProt, Open Targets; optional AlphaGenome Atlas with your own key |

<details>
<summary>All 23 tools and the resources</summary>

| Group | Tools |
| --- | --- |
| Discovery | `list_sources`, `search_datasets`, `describe_dataset`, `list_files`, `list_samples`, `get_sample_metadata` |
| Transfers | `fetch_file`, `get_transfer_status`, `cancel_transfer` |
| Genomics | `get_reads`, `get_coverage`, `get_pileup`, `get_variants`, `get_sequence`, `get_features`, `get_signal` |
| Composition | `inspect_locus`, `compare_samples` |
| Reference | `resolve_identifier`, `normalize_variant`, `lookup_variant`, `lookup_gene`, `lookup_protein` |

Resources: `genomics://capabilities`, `genomics://status`, `genomics://schemas`, `genomics://schemas/{name}`.

</details>

## Defaults and safety

- **Limits:** 1 Mb regions, 10,000 records, 1 MiB responses and a 30 s deadline; calls may lower these. Transfers are capped at 100 MiB unless a call sets a larger budget. Truncation is reported. Nothing is lifted over between assemblies.
- **Local only:** stdio, or Streamable HTTP with a bearer token on 127.0.0.1. There is no hosted service. Local reads are limited to the folders you allow, and source files are never modified.
- **Credentials and egress:** ambient AWS credentials are never used; private S3 and EGA need explicit configuration. Values from files not marked `public` go to external APIs only when a call sets `allow_external_annotation`.
- **Cache:** a bounded in-memory cache reuses byte ranges of public HTTPS files marked `public` (revalidated by ETag on every call) and public API responses. It is never used for private, signed or authenticated requests. Turn it off with `[cache] enabled = false`; restarting clears it. Details and measurements: [performance](docs/performance.md).

Configuration: [config.example.toml](config.example.toml). Scope and known limits: [PRD.md](PRD.md). Directory and PyPI status: [publication ledger](docs/registry-ledger.md). Technical details: [data access](docs/data-access.md), [archives](docs/archive-sources.md), [references](docs/reference-sources.md), [composition](docs/composition.md). Security: [SECURITY.md](SECURITY.md).

## Development

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

## Licence

MIT. See [LICENSE](LICENSE). Data from each source is subject to that source's own terms.
