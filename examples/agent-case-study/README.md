# Agent case study: BCL11A erythroid enhancer accessibility in ENCODE DNase-seq

A Claude Code agent, with only this repository's MCP server as tools, measures public ENCODE DNase-seq signal at the HbF-associated BCL11A intron-2 enhancer (DNase I hypersensitive sites +55, +58 and +62). It compares 26 bigWig files: a single-donor adult CD34+ erythroid culture from day 0 to day 20, and K562, GM12878, HepG2 and CD14+ monocytes.

This checks published biology with public data. It is not a discovery. Bauer et al. (2013, PMID 24115442) found erythroid-specific chromatin at these sites in human cells, and showed by deleting the orthologous mouse enhancer that it is needed for BCL11A expression in a mouse erythroid line but not in a mouse pre-B line. Canver et al. (2015, PMID 26375006) mapped its critical +58 core in the human erythroid line HUDEP-2. Neither tested GM12878; what this case study says about GM12878 is an inference from accessibility. The question, target, panel, windows, metrics and contrasts were fixed in advance by the case-study author ([PROTOCOL.md](PROTOCOL.md), commit c30f740). The agent resolved coordinates with reference tools, chose how to batch and run the measurements, and wrote the report.

## What is here

| Path | What it is |
| --- | --- |
| [PROTOCOL.md](PROTOCOL.md) | Predeclared question, windows, metrics and contrasts, with later wording corrections listed |
| [discovery/select_files.py](discovery/select_files.py), [manifest.json](manifest.json), [evidence/encode_metadata.json](evidence/encode_metadata.json) | Rule-based panel selection from the ENCODE REST API; saved responses with UTC times and SHA-256 |
| [discovery/audit_warnings.py](discovery/audit_warnings.py), [evidence/encode_audits.json](evidence/encode_audits.json) | Every ENCODE audit level for the 14 experiments, including WARNING, recorded after the run for disclosure (not used for selection) |
| [agent/prompt.md](agent/prompt.md), [agent/genomics-mcp.toml](agent/genomics-mcp.toml), [run_agent.py](run_agent.py) | Agent prompt, server configuration and credential-isolated Claude Code launcher |
| [extract_run.py](extract_run.py) | Raw stream-json log → redacted transcript (visible messages, tool requests and results only), tool-call evidence, report, run metadata |
| [casestudy.py](casestudy.py), [replay.py](replay.py) | Protocol arithmetic; model-free replay and cell-by-cell verification |
| `runs/2026-09-28-claude-opus-5/` | The complete run: `transcript.redacted.jsonl`, `tool_calls.json`, `agent_report.md`, `results.json`, `run.json`, `launch.json`, `prompt.txt`, `replay/ensembl.json` |
| `runs/2026-09-28-interrupted/` | An interrupted first attempt, kept for the record; [NOTE.md](runs/2026-09-28-interrupted/NOTE.md). No values from it are used. |
| [../../tests/examples/](../../tests/examples/) | Offline tests: coordinates, replicate handling, verifier behaviour, run evidence, redaction; launcher failure paths with a fake CLI |

## Get the example

The example is newer than the v0.1.0 release tag, so check out the commit that contains it (or `main` once it is merged). The server code is unchanged from v0.1.0 at that commit: `launch.json` records that the runtime `src` tree equals v0.1.0's. Later commits on `main` can include unreleased runtime changes; check out the example commit to reproduce with the same runtime.

```sh
git clone https://github.com/rewire-bio/genomics-mcp && cd genomics-mcp
git checkout <example commit>      # or main
```

## Three ways to reproduce

**1. Verify the saved evidence (no network, no model).** Standard-library Python only.

```sh
python3 examples/agent-case-study/replay.py examples/agent-case-study/runs/2026-09-28-claude-opus-5 --offline
```

This checks every one of the 26 files × 7 windows: the agent measured it over MCP, the agent's reported value is its own tool result for that file and window, the stored pyBigWig replay agrees, and the stored contrasts follow from the stored means. Any failure exits with status 1. With the development environment, the tests run the same checks and more: `uv run pytest tests/examples`.

**2. Replay the measurements live and deterministically (network, no model).** Needs the source install from the main README, with `pyBigWig.remote == 1`. The replay resolves coordinates from the Ensembl REST API and reads all 182 windows with pyBigWig over HTTPS range requests, without the MCP server. It overwrites `results.json` in the directory you give, so use a copy:

```sh
uv sync --locked
cp -r examples/agent-case-study/runs/2026-09-28-claude-opus-5 /tmp/case-replay
.venv/bin/python examples/agent-case-study/replay.py /tmp/case-replay
```

**3. Run a new agent session (network, your Claude subscription).** Needs Claude Code logged in to your own account; no API key or cloud account.

```sh
uv sync --locked
python3 examples/agent-case-study/run_agent.py            # writes runs/<UTC time>/
.venv/bin/python examples/agent-case-study/replay.py examples/agent-case-study/runs/<UTC time>
```

A new session is a new, independent run. Its calls, wording and possibly its contrast calls will differ; the replay verifies it the same way. The launcher:

- refuses an `--out` directory that already exists and is not empty, and otherwise writes to a new `runs/<UTC time>/`;
- runs Claude Code in its own process group and stops that group (including the MCP server) on timeout, interruption or error;
- exits 0 only if the model finished, extraction succeeded and every (file, window) cell has a successful MCP result equal to the agent's reported value (`replay.py --coverage-only`). A finished model process with an incomplete report exits 3. The launcher does not do the live replay; run `replay.py RUN_DIR` for that;
- keeps its internal launch record (including the Claude session ID) in the git-ignored `raw/`, and publishes `launch.json` and `run.json` without it;

- gives the model only the `genomics` MCP tools (`--tools ""`, `--allowedTools mcp__genomics`, `--strict-mcp-config`), with no shell, files or web;
- runs in an empty temporary directory with `--setting-sources project`, so user hooks, settings and `env` blocks are not loaded;
- passes an allowlisted environment (PATH, HOME, USER, locale, TMPDIR). Cloud, AWS/boto/HTSlib and Anthropic API-key variables are not passed. EC2 metadata is disabled and AWS/boto config files point at an empty file;
- pins `--model claude-opus-5`. With the `opus` alias (Opus 5.5 at the time), the first attempt was flagged by Opus 5.5's biology safeguards, and Claude Code continued on Opus 5 automatically. `extract_run.py` records every model that answered.

**Another MCP client.** Configure the server as in the main README, with this example's server configuration:

```json
{
  "mcpServers": {
    "genomics": {
      "command": "/path/to/genomics-mcp/.venv/bin/genomics-mcp",
      "env": { "GENOMICS_MCP_CONFIG": "/path/to/genomics-mcp.toml" }
    }
  }
}
```

Copy `examples/agent-case-study/agent/genomics-mcp.toml`, set `work_dir` to an empty directory, and use `python3 examples/agent-case-study/run_agent.py --dry-run --out /tmp/p` to write the full prompt to `/tmp/p/prompt.txt`. Give the model only the genomics tools. Values are in each tool result's `structuredContent`; the text content is a short summary. Claude Code passes `structuredContent` to the model for successful calls. Check that your client does too.

## Expected results

From run `runs/2026-09-28-claude-opus-5/` (`claude-opus-5` via Claude Code 2.1.283, 2026-09-28 15:05–15:20 UTC, launched from commit f1f1d90 with the v0.1.0 runtime). Values are exact from `results.json`. The replay reproduced all 182 values within a relative tolerance of 1e-9. A second, different-method check by the coordinator also agreed on all 182 cells: it length-weighted the raw bigWig intervals instead of calling `stats()`.

**The run.** 29 MCP calls: 4 `normalize_variant`, 2 `lookup_gene`, 18 `compare_samples`, 3 `list_files` and 2 `describe_dataset`. The `compare_samples` calls requested 209 file-window measurements: 182 succeeded and 27 returned per-file `timeout`s. Every one of the 182 required cells succeeded exactly once. The timeouts came from four of six 16-file calls the agent issued in one turn. The agent retried only the 27 failed pairs, with fewer calls per turn, and all succeeded. The cause of the timeouts was not measured. One `normalize_variant` call was `partial`: rs6738440's position came back, but its reference-base check timed out. The server's work directory held 0 bytes afterwards.

**Coordinates.** The anchors are at VCF POS 60498316, 60494905, 60495106 and 60490908 (rs7606173, rs6706648, rs6738440, rs1427407). The BCL11A TSS (ENST00000642384.2, minus strand) is 60553653 and the GAPDH TSS (ENST00000229239.10) is 6534516, both 0-based. The windows are E55 chr2:[60497815, 60498815), E58 [60494504, 60495504), E62 [60490407, 60491407), BG_up [60508315, 60518315), BG_down [60470907, 60480907), P [60553153, 60554153) and G chr12:[6534016, 6535016). They match an independent Ensembl REST lookup exactly. BG_up and BG_down lie within the BCL11A gene span, but their intron/exon status was not checked; they are local background windows.

**Observations (exact replay; per-day means of replicates).**

| Sample | n | E58 | G | B | E58/G | E58/B | ENCODE SPOT flags |
| --- | --- | --- | --- | --- | --- | --- | --- |
| day 0 | 2 | 0.036 | 2.40 | 0.078 | 0.016 | 0.46 | warning |
| day 4 | 2 | 0.26 | 4.57 | 0.068 | 0.055 | 3.9 | none |
| day 6 | 2 | 0.90 | 4.00 | 0.093 | 0.23 | 9.7 | none |
| day 8 | 3 | 1.35 | 3.71 | 0.090 | 0.36 | 14.9 | none |
| day 11 | 3 | 0.72 | 1.55 | 0.080 | 0.48 | 8.9 | error + warning |
| day 13 | 2 | 0.81 | 0.96 | 0.099 | 0.85 | 8.1 | error |
| day 15 | 2 | 1.05 | 1.68 | 0.094 | 0.62 | 11.2 | warning |
| day 17 | 2 | 0.51 | 1.14 | 0.068 | 0.46 | 7.5 | warning |
| day 18 | 1 | 0.39 | 1.04 | 0.063 | 0.38 | 6.2 | none (unreplicated) |
| day 20 | 1 | 0.22 | 0.79 | 0.064 | 0.27 | 3.3 | error (unreplicated) |
| GM12878 | 2 | 0.028 | 2.17 | 0.127 | 0.013 | 0.25 | warning (also low read depth) |
| HepG2 | 2 | 0.041 | 1.71 | 0.118 | 0.024 | 0.35 | warning |
| CD14+ monocyte | 1 | 0.032 | 2.61 | 0.340 | 0.012 | 0.094 | none |
| K562 (Duke) | 1 | 0.057 | 1.11 | 0.052 | 0.051 | 1.1 | warning |

SPOT flags are ENCODE's experiment audits on 2026-09-28 (`evidence/encode_audits.json`): "extremely low spot score" is an ERROR and "low spot score" a WARNING. Every raw window mean for all 26 files is in `results.json` (`files`).

**Contrasts.**

1. Lineage: separated on all four predeclared metrics. `E58/G` is 0.274–0.852 in the 11 late erythroid files (days 11–20) and 0.012–0.024 in the 5 non-erythroid files. `E58/B` is 3.35–13.5 against 0.094–0.360, and `max(E)/B` is 4.88–36.2 against 0.41–1.26. Raw E58 is also separated: 0.215–1.262 against 0.023–0.043. This is the robust result.
2. GM12878: the BCL11A promoter is more enriched than E58 in both replicates (`P/B` 6.6 and 1.8; `E58/B` 0.35 and 0.14). Only replicate 1 reaches the predeclared, arbitrary "accessible" label (≥ 3) at the promoter. E58 is below it in both. BG_up is 3–5× BG_down in GM12878, which inflates B. ENCODE flags this reference experiment for low read depth and low SPOT score.
3. Timing: every day-4 replicate exceeds the day-0 maximum on `E58/G`, on raw E58 and on `E58/B`. The highest sampled day mean of raw E58 and `E58/B` is at day 8. Later days are lower than day 8 but not steadily so: raw E58 is 0.72 at day 11, 0.81 at day 13 and 1.05 at day 15, then 0.51, 0.39 and 0.22 at days 17, 18 and 20. `E58/G` peaks at day 13 instead, because the day mean of G is lower on every sampled day from 11 to 20 (0.79–1.68) than on days 0–8 (2.40–4.57). That is arithmetic, not evidence of further opening. Whether the lower G is technical or biological is not established: low-SPOT flags occur on some of those days but not all (day 18 has none). A sampled maximum at day 8 does not show that opening is complete by then.
4. K562: `E58/B` is 1.1, with no element at or above 3 (one replicate, a different lab and protocol).
5. Sub-elements: E55 has the highest signal in all 11 late erythroid files; mean `E/G` is 1.09 for E55, 0.54 for E58 and 0.66 for E62. Accessibility ranking is not functional importance: Canver et al. found +58 critical by deletion.

**Denominators.** G varies 7.5-fold across files (0.68–5.12, CV 0.56). Within the time course, B is 0.063–0.115. Across all files it is 0.052–0.340 (CV 0.55), mainly because of the monocyte and GM12878 BG_up windows.

**Audit of the agent's report.** Its report (`agent_report.md`) is kept exactly as the model wrote it. Every value in its JSON block equals its own tool result for that file and window. Its rounded table (26 rows × 5 values) matches the replay, and all six of its contrast calls agree with the replay (it gave `null` for the GM12878 promoter label because the replicates disagree). Its prose has errors, corrected here, not in the transcript:

- the call breakdown "6 + 21 + 3 + 2" does not add up to its stated total of 29 (18 `compare_samples` calls);
- the late-erythroid `max(E)/B` range is given as "4.9–21" (exact: 4.88–36.22, ENCFF404BKX);
- the late raw E58 range is given as "0.215–1.081" (exact: 0.215–1.262, ENCFF404BKX);
- it says raw E58 "declines monotonically" after day 8 (it does not; see contrast 3) and that opening "completes by roughly day 8" (a sampled maximum does not show that);
- it calls the later `E58/G` peak "a measurement artefact of declining library signal-to-noise" (the denominator effect is arithmetic; its cause is not established) and says G falls on "exactly the days" with low-SPOT flags (the day mean of G is lower on all sampled days from 11 to 20);
- it describes "96 concurrent bigWig byte-range fetches" (not measured: the server applies its own concurrency limits);
- it says the background windows are intronic because they lie within the gene span (not checked against transcript structure);
- it says `compare_samples` "echoed back the accession it resolved from each URL" (the server echoes the accession it was given).

It could not confirm the file-to-experiment mapping of individual files through `list_files` pagination within its budget, and it says so.

**The agent's follow-up hypothesis** (its own, untested): the lower raw E58 after day 8 reflects falling library signal-to-noise rather than closing chromatin. Its proposed test (relate each file's E58 to its SPOT score; express E58 as a fraction of in-peak signal) would be a sensitivity analysis, not a separation of the two explanations: time, cell state, global accessibility and library quality vary together in this single culture. Independent donors, quality-matched libraries or orthogonal data (for example matched expression or chromatin marks) would be needed for a biological conclusion.

## Limitations

- The target and panel were preselected, and the biology is published. The agent did not choose them.
- The erythroid time course is one adult donor's in vitro culture. Its replicates are not independent donors, and no inferential test is done. Cell lines are not tissues.
- The panel is not identically processed. Thirteen experiments are from the UW lab with ENCODE4 pipeline v3.0.0-alpha.2. K562 is from the Duke lab (a different DNase-seq protocol) with v3.0.0, and has one replicate. The monocyte experiment also has one replicate.
- ENCODE flags days 11, 13 and 20 with an "extremely low spot score" ERROR, and days 0, 11, 15 and 17, K562, GM12878 and HepG2 with a "low spot score" WARNING. GM12878 also has a low-read-depth WARNING. Days 18 and 20 have one replicate. The panel rules screened ERROR/NOT_COMPLIANT audits only for the reference cell types; nothing was dropped after measurement.
- The GM12878 comparison with Bauer et al. is an inference. Their loss-of-function result was in mouse cell lines, and GM12878 was not tested there.
- The "accessible" label (enrichment ≥ 3) is an arbitrary predeclared threshold.
- `E / G` (enhancer over the GAPDH promoter) is a descriptive reference ratio, not a correction for library quality. Raw `E`, `G` and background values are reported with it.
- Accessibility is not enhancer activity, and neither shows causality. No clinical interpretation.
- pyBigWig range reads do not check the files' MD5s, which cover whole files. The replay and the MCP server read the same public objects independently.
