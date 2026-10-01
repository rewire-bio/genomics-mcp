# Optional SRA Toolkit conversion (`convert_sra_run`)

Unreleased: development checkout only, not in v0.1.0. Tracked in #47 (workflow) and #48 (container).

`convert_sra_run` downloads one public SRA run with NCBI's `prefetch` and converts the whole run to FASTQ with `fasterq-dump`. It needs NCBI SRA Toolkit 3.x (tested: 3.4.1). Nothing else in the server needs the toolkit. Without it, the tool returns `unsupported` with installation hints.

## ENA FASTQ or toolkit conversion

For most public runs, ENA already serves FASTQ. Use that first.

| | ENA FASTQ | `convert_sra_run` |
| --- | --- | --- |
| Tools | `list_files` (source `ena`, run accession), then `fetch_file` | `convert_sra_run`, then `get_transfer_status` |
| Needs SRA Toolkit | No | Yes |
| Files | ENA's gzipped FASTQ, as ENA publishes them | Uncompressed FASTQ written by `fasterq-dump` from NCBI's run file |
| Checksums | Verified against ENA's MD5 | md5/sha256 of the written files; there is no archive checksum for converted output |
| Download | Only the FASTQ files | The whole run file, then local conversion (scratch space) |

Use the toolkit when ENA lists no FASTQ for a run, or when you want NCBI's own conversion (split-3 layout, spot numbering). Neither route is a regional read: FASTQ is `not_locus_ready`.

## What a call does

`convert_sra_run(accession, budget_bytes?, timeout_s?)` accepts only `SRR`, `ERR` or `DRR` run accessions. It starts a job and returns within a few seconds. Poll `get_transfer_status`; stop it with `cancel_transfer`. Discovery tools and region reads never start a conversion.

1. `prefetch <acc> --type sra --transport http --max-size <budget in KB> --resume yes --verify yes`. The run file and any dependencies (for example reference sequences of aligned runs) go into the job's directory.
2. `fasterq-dump --size-check only --details` reports an output estimate, the sequence table, the spot count and the biological base count. The job fails if any is missing, or if the table is not `SEQUENCE` (for example PacBio `CONSENSUS`).
3. `fasterq-dump --split-3 --skip-technical`, with the toolkit's remote access turned off, so incomplete local material fails instead of being fetched again.
4. The server reads every FASTQ record itself. It checks record structure, `<acc>.<spot>` names, mate order, strictly increasing spot numbers across the files, and that reads and bases equal `fasterq-dump`'s "reads written" and the run's biological base count. Any mismatch fails the job; nothing partial is reported complete.

Output, in `<work_dir>/artifacts/<transfer_id>/`:

- `<acc>_1.fastq` and `<acc>_2.fastq`: reads from spots with two biological reads.
- `<acc>.fastq`: reads without a mate. Kept, not discarded.
- Technical reads (barcodes, adapters) are skipped. This is `fasterq-dump`'s default.

The response lists each file's path, size, md5, sha256, read and base counts, and role (`mate_1`, `mate_2`, `unpaired`). It also gives the toolkit version, run file and dependencies, conversion settings, estimates, verification results, peak disk use and timings. FASTQ content is never returned in MCP responses.

## Budgets and limits

- `budget_bytes` limits the job's disk use: run file, dependencies, scratch and FASTQ together. Default 100 MiB (`limits.max_transfer_bytes`), up to `limits.transfer_budget_ceiling_bytes`. The job reserves its budget in the work dir quota (`limits.workspace_max_bytes`) while queued or running.
- Enforcement: the job directory is measured every 0.5 s while a toolkit process runs, and again when it exits. Over budget, the process group is killed and the job fails with `budget_exceeded`. This is a watchdog, not a filesystem quota: between samples a process can exceed the budget by what it writes in 0.5 s.
- `prefetch --max-size` is set to the whole budget. It applies to the run file only: prefetch 3.4.1 does not size-check dependencies, so the watchdog is their only bound. Network bytes are not metered separately: `prefetch` writes what it downloads and resumes rather than restarting.
- Before converting, the job needs: bytes already used + 1.25 × output estimate + 1.5 × output estimate for scratch. It also checks free disk space. NCBI's figures are estimates; in testing the output estimate was slightly below the real output.
- Disk to plan for: NCBI's guide gives about 7× the run file for FASTQ and roughly 17× in total during conversion. Their fasterq-dump page gives different factors. For a 1 GB run file, start with a 17 GB budget.
- Time: `sra_toolkit.timeout_s` (default 3600 s) includes the download. A call's `timeout_s` can only lower it. On timeout the toolkit is stopped and the download is kept.
- Concurrency: SRA jobs share the two transfer slots with `fetch_file`. `fasterq-dump` uses `sra_toolkit.threads` (default 4).

## Isolation

- No shell and no caller-supplied flags. The toolkit runs with the same small environment as the native readers: no AWS or cloud variables, no proxies, EC2 metadata disabled.
- Each job has its own `HOME`, `TMPDIR` and `NCBI_SETTINGS` file inside the job directory. Your `~/.ncbi` is not read or written. The settings use a random GUID, no cloud instance identity, no AWS or GCP charges, and no toolkit cache.
- Only public runs. Controlled-access (dbGaP) runs fail. No `.ngc` or cart files are accepted.
- Toolkit output is captured and logged at debug level to stderr. It never reaches MCP stdout.
- Limitation: if the server is killed with SIGKILL, automatic cleanup cannot run and running toolkit processes may continue. Stopped-child cleanup removes newly created locks and preserves pre-existing locks. SIGKILL cannot clean up, so an operator must ensure no orphan process owns a lock before manually removing it and retrying. Retrying does not automatically clear old locks.

## Native installation

NCBI's binaries for 3.4.1, from https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/3.4.1/. NCBI publishes MD5s there (`md5sum.txt`). The SHA-256 values were computed from our downloads, whose MD5s matched NCBI's.

| Platform | File | MD5 (NCBI) | SHA-256 |
| --- | --- | --- | --- |
| macOS arm64 | `sratoolkit.3.4.1-mac-arm64.tar.gz` | `787da33c43343f2034c063ea1cd376f7` | `b38ec90f9c99805533c90684310b0495288890559c5af194f1074b72f4e95255` |
| Linux x86_64 | `sratoolkit.3.4.1-ubuntu64.tar.gz` | `ec6e9056a2bfebcf23c6cd6e02951ef2` | `b950362c054765a4184af41947f022f040e94e964862017c0ecb0b0273db3596` |

```sh
curl -fLO https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/3.4.1/sratoolkit.3.4.1-mac-arm64.tar.gz
shasum -a 256 sratoolkit.3.4.1-mac-arm64.tar.gz   # compare with the table
tar -xzf sratoolkit.3.4.1-mac-arm64.tar.gz -C ~/opt
```

Point the server at the `bin` directory. `vdb-config` is not needed.

```json
{
  "mcpServers": {
    "genomics": {
      "command": "uv",
      "args": ["--directory", "/path/to/genomics-mcp", "run", "--no-dev", "genomics-mcp"],
      "env": {
        "GENOMICS_MCP_SRA_TOOLKIT_DIR": "/Users/you/opt/sratoolkit.3.4.1-mac-arm64/bin"
      }
    }
  }
}
```

Or set `[sra_toolkit] bin_dir` in the config file, or put `prefetch` and `fasterq-dump` on `PATH`. `genomics://status` reports whether the toolkit was found and its version. If macOS blocks a binary downloaded with a browser, remove the quarantine attribute from the extracted directory (`xattr -dr com.apple.quarantine <dir>`); `curl` downloads are not quarantined.

## Container

The standard image is unchanged and has no toolkit. An optional variant adds only `prefetch`, `fasterq-dump`, their `sratools` dispatcher and NCBI's default configuration from the Linux x86_64 build above. The build checks the archive's SHA-256. The variant is not published; build it locally (linux/amd64):

```sh
docker build --platform linux/amd64 --target sra -t genomics-mcp-sra .
```

It runs as the image's non-root user (uid 10001). Toolkit settings, scratch, downloads and FASTQ all stay under `/work`. No home directory, privileges or Docker socket are needed. A bind-mounted work directory must be writable by uid 10001. The files it creates are owned by that uid.

```json
{
  "mcpServers": {
    "genomics": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i", "--read-only", "--tmpfs", "/tmp",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--mount", "type=volume,source=genomics-mcp-work,target=/work",
        "genomics-mcp-sra"
      ]
    }
  }
}
```

## Cleanup

- Completed: only the FASTQ files remain, in `<work_dir>/artifacts/<transfer_id>/`. The run file and scratch are deleted. Delete that directory when you no longer need the files.
- Failed but resumable (for example a timeout or network error): the download stays in `<work_dir>/transfers/<transfer_id>/sra.part/` so the next `convert_sra_run` resumes it. When a process group is stopped, newly created `.sra.lock` files are cleaned up while preserving partial `.tmp`/`.prf` data; pre-existing locks are preserved. `cancel_transfer` removes it.
- Failed permanently (budget exceeded while running, invalid output) or cancelled: the job's files are removed.

## Troubleshooting

| Result | Meaning |
| --- | --- |
| `unsupported`: SRA Toolkit is not available | `prefetch`/`fasterq-dump` not found, or version below 3.0. Set `GENOMICS_MCP_SRA_TOOLKIT_DIR`. |
| `not_found` with `no data ( 404 )` | NCBI's locator did not return the run. Check `https://www.ncbi.nlm.nih.gov/sra/<acc>`; ENA may still serve FASTQ. |
| `budget_exceeded` with `required_bytes` | Call again with at least that `budget_bytes`; the download is kept. |
| `budget_exceeded`, "toolkit was stopped" | Disk use passed the budget during a step; the job's files were removed. |
| `timeout` | Call again; the download is kept. |
| `unsupported`, table `CONSENSUS` | The run's FASTQ comes from a table whose completeness cannot be verified here. |

## Licence and dependencies

NCBI's own sra-tools and ncbi-vdb code is a United States Government work in the public domain. The toolkit binaries are not only NCBI code: they statically include bzip2 1.0.8, zlib 1.3.1, Zstandard 1.5.7 (BSD-3-Clause or GPL-2.0), Mbed TLS 3.2.1 (Apache-2.0) and an LGPL-2.1-or-later sort routine from the GNU C Library (ncbi-vdb `libs/klib/qsort.c`). All three shipped binaries contain all of these; ncbi-vdb's vendored `regex` and `szip` were not found in them. They link dynamically only to glibc from the Debian base image.

The image carries the licence texts, the pinned sra-tools and ncbi-vdb commits and the LGPL component's source in `/opt/sratoolkit/licenses`, from [packaging/sra-toolkit/licenses](../packaging/sra-toolkit/licenses/README.md). The genomics-mcp code itself is MIT.

Sources: [prefetch and fasterq-dump](https://github.com/ncbi/sra-tools/wiki/08.-prefetch-and-fasterq-dump), [fasterq-dump](https://github.com/ncbi/sra-tools/wiki/HowTo:-fasterq-dump), [SRA Toolkit Docker](https://github.com/ncbi/sra-tools/wiki/SRA-tools-docker).

## Verification status (1 October 2026)

- Offline tests (`tests/artifacts/test_sra.py`, scripted toolkit): 52 passed. Missing or old toolkit, invalid and unknown accessions, controlled access, interrupted download and resume, pre-existing lock preservation, cancellation (process group and descendants killed, files removed), timeout, disk and budget limits, missing estimates, unsupported tables, incomplete, inconsistent or duplicated output, environment and settings isolation, workspace reservation on resume.
- Real toolkit 3.4.1, macOS arm64:
  - `tests/artifacts/test_sra_live.py`: 2 passed. Real `prefetch` and `fasterq-dump` conversion and verification through `convert_sra_run` passed for SRR13450355 (paired, 744 spots) and SRR24157174 (single-end, 1,080 spots) with counts verified against ENA.
  - Native demonstration harness (`scripts/sra_demo.py`): all 8 checks passed (`status`, `invalid`, `budget`, `paired`, `single`, `cancel`, `interrupt`, `interrupt_resume`). Cleanly handles prefetch process group cleanup, mid-download interruption, safe resume with retained `.tmp` partial data, and cancellation.
- Container: Linux CI ([run 36837416155](https://github.com/rewire-bio/genomics-mcp/actions/runs/36837416155), 1 October 2026, commit `968b7a1`) passed all jobs including all 8 real Linux amd64 `sra-container` checks under Docker as uid 10001 (harness uses concurrent MCP tool calls and workspace observation to interrupt active downloads). CI also checks insufficient disk with an 8 MiB tmpfs.
- Platforms: macOS arm64 native fully verified; Linux amd64 container verified by that run. Other platforms are not claimed.
