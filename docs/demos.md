# Clean-install demonstrations

Run 2026-09-25 14:54–14:55 UTC on macOS arm64 (Python 3.12.14) by `scripts/demos.py`, against `genomics-mcp` installed from a wheel into an empty virtual environment. Results: [demos/results/2026-09-25/](../demos/results/2026-09-25/) (`summary.json` plus one file per demo).

## How it was run

```sh
git clone <this repository> src && cd src && git checkout 31444c3
uv build --out-dir dist
uv export --frozen --no-dev --no-emit-project --format requirements.txt -o dist/requirements.lock.txt
python3 scripts/check_dist.py dist --install /tmp/gm-clean --output dist-check.json
GENOMICS_DEMO_MINIO_KEY=… GENOMICS_DEMO_MINIO_SECRET=… \
  uv run --script scripts/demos.py --out demos/results/$(date -u +%F) \
  --install-report dist-check.json --source-commit "$(git rev-parse HEAD)" -- /tmp/gm-clean/bin/genomics-mcp
```

- Wheel SHA-256 `dbb2e3358df346508ab91507e2808ec924990ecef15606cea27cbda4e94a201f`, sdist `c63a2cb4589d72193207becde27cb433c01da47afa952798342b03d7a846dc39`. Both builds that day produced identical hashes. `pyBigWig.remote == 1`.
- The harness talks MCP over stdio to the installed command. It never imports `genomics_mcp`.
- Every demo starts a fresh server with its own work directory and dummy `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` in its environment. It checks they never appear in results or logs.
- The results keep counts, identities, checksums, timings and provenance. Signed URL values, secrets, source-native metadata blobs and local paths (`$RUN`, `~`) are removed. Live-source demos can fail when a source changes or is unavailable. A failure is recorded as that demo's error, never as a pass.

## Results

| Demo | What ran | Result |
| --- | --- | --- |
| EGA | `describe_dataset` EGAD00001003338; `get_reads` `ega://EGAF00007243773` (BAM), GRCh38 chr10:[10000,10050), EGA public test account | htsget returned 91 records; 42 overlap the interval and were returned. The header's `AS:GRCh38` is reported as a declared label, not as a precise assembly accession. |
| ENA | `list_files` DQ285577; `fetch_file` (prepare) of the DQ285577.1 FASTA; `get_sequence` [0,614) | Local artifact of 756 bytes with 614 bases; file MD5/SHA-256 recorded. ENA publishes an MD5 of the sequence, not of the FASTA file, so the transfer's `checksum_verified` is false. The harness checks the 614 returned bases against ENA's `sequence_md5` independently, and they match. This is a sequence artifact, not an alignment region. |
| ENCODE | `list_files` ENCFF792QDS; `get_signal` GRCh38 chr1:[1000000,1001000) | Exact mean 26.361254017233847, from HTTP range reads of the 1,413,106,336-byte bigWig. The work directory held 0 bytes afterwards. |
| Reference + ClinVar | `normalize_variant` GRCh38 7-140753336-A-T with `ncbi_nuccore`; `lookup_variant` with `clinvar` | REF `A` verified against NCBI nuccore NC_000007.14 (GRCh38 chr7). ClinVar VCV000013961.143 (BRAF c.1799T>A, p.Val600Glu), Entrez build 260924-0125.1. Kept separate: germline "Conflicting classifications of pathogenicity", somatic clinical impact "Tier I - Strong", oncogenicity "Oncogenic". 45 submissions: 22 germline, 21 somatic clinical impact, 2 oncogenicity. |
| MinIO | Synthetic 60-read BAM uploaded to local MinIO (127.0.0.1:39000, bucket `genomics-mcp-test`) with explicit local test keys; `get_reads` via `s3://` + `storage_profile`, and on the local file; then the same S3 request without the profile keys | 20 records from S3, identical to the local file and to an independent pysam count. Without the keys: `unauthorized`, with no fallback to the dummy ambient AWS credentials. The objects were deleted afterwards. No AWS account was used. |

## Reproducing

The EGA, ENA, ENCODE and ClinVar demos need internet access only. The MinIO demo needs a local MinIO on 127.0.0.1:39000 with a bucket `genomics-mcp-test` and a user whose keys you pass in `GENOMICS_DEMO_MINIO_KEY`/`GENOMICS_DEMO_MINIO_SECRET`. Run one demo with `--only ega` (or `ena`, `encode`, `reference`, `minio`).

The same harness can target the container or the MCPB launcher: pass that command after `--`.
