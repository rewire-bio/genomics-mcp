# Agent case study: BCL11A erythroid enhancer accessibility in ENCODE DNase-seq

A Claude Code agent, with only this repository's MCP server as tools, measures public ENCODE DNase-seq signal at the HbF-associated BCL11A intron-2 enhancer (DNase I hypersensitive sites +55, +58 and +62). It compares 26 bigWig files: a single-donor adult CD34+ erythroid culture from day 0 to day 20, and K562, GM12878, HepG2 and CD14+ monocytes.

This checks published biology with public data (Bauer et al. 2013, PMID 24115442; Canver et al. 2015, PMID 26375006). It is not a discovery. The question, target, panel, windows, metrics and contrasts were fixed in advance by the case-study author ([PROTOCOL.md](PROTOCOL.md), commit c30f740). The agent resolved coordinates with reference tools, chose how to batch and run the measurements, and wrote the report.

## What is here

| Path | What it is |
| --- | --- |
| [PROTOCOL.md](PROTOCOL.md) | Predeclared question, windows, metrics and contrasts, with later wording corrections listed |
| [discovery/select_files.py](discovery/select_files.py), [manifest.json](manifest.json), [evidence/encode_metadata.json](evidence/encode_metadata.json) | Rule-based panel selection from the ENCODE REST API; saved responses with UTC times and SHA-256 |
| [agent/prompt.md](agent/prompt.md), [agent/genomics-mcp.toml](agent/genomics-mcp.toml), [run_agent.py](run_agent.py) | Agent prompt, server configuration and credential-isolated Claude Code launcher |
| [extract_run.py](extract_run.py) | Raw stream-json log → redacted transcript (visible messages, tool requests and results only), tool-call evidence, report, run metadata |
| [casestudy.py](casestudy.py), [replay.py](replay.py) | Protocol arithmetic; model-free replay and cell-by-cell verification |
| `runs/2026-09-28-claude-opus-5/` | The complete run: `transcript.redacted.jsonl`, `tool_calls.json`, `agent_report.md`, `results.json`, `run.json`, `launch.json`, `prompt.txt`, `replay/ensembl.json` |
| `runs/2026-09-28-interrupted/` | An interrupted first attempt, kept for the record; [NOTE.md](runs/2026-09-28-interrupted/NOTE.md). No values from it are used. |
| [../../tests/examples/test_agent_case_study.py](../../tests/examples/test_agent_case_study.py) | Offline tests: coordinates, replicate handling, verifier behaviour, run evidence, redaction |

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

From run `runs/2026-09-28-claude-opus-5/` (Claude Code session `783458be-0e17-4e42-b201-5b2583f6acb7`, `claude-opus-5`, 2026-09-28 15:05–15:20 UTC, launched from commit f1f1d90 with the v0.1.0 runtime). Values are exact from `results.json`; the replay reproduced all 182 values within a relative tolerance of 1e-9.

**The run.** 29 MCP calls: 4 `normalize_variant`, 2 `lookup_gene`, 18 `compare_samples`, 3 `list_files` and 2 `describe_dataset`. The agent first sent six 16-file `compare_samples` calls at once (96 concurrent remote bigWig reads). Four came back `partial`, with 27 per-file timeouts at the server's 30 s deadline. The agent reported this, retried only the 27 failed file-window pairs, with fewer concurrent calls, and got all of them. One `normalize_variant` call was `partial`: rs6738440's position came back, but its reference-base check timed out. The server's work directory held 0 bytes afterwards.

**Coordinates.** The anchors are at VCF POS 60498316, 60494905, 60495106 and 60490908 (rs7606173, rs6706648, rs6738440, rs1427407). The BCL11A TSS (ENST00000642384.2, minus strand) is 60553653 and the GAPDH TSS (ENST00000229239.10) is 6534516, both 0-based. The windows are E55 chr2:[60497815, 60498815), E58 [60494504, 60495504), E62 [60490407, 60491407), BG_up [60508315, 60518315), BG_down [60470907, 60480907), P [60553153, 60554153) and G chr12:[6534016, 6535016). They match an independent Ensembl REST lookup exactly.

**Observations (exact replay; per-day means of replicates).**

| Sample | E58 | G | B | E58/G | E58/B |
| --- | --- | --- | --- | --- | --- |
| day 0 (n=2) | 0.036 | 2.40 | 0.078 | 0.016 | 0.46 |
| day 4 (n=2) | 0.26 | 4.57 | 0.068 | 0.055 | 3.9 |
| day 8 (n=3) | 1.35 | 3.71 | 0.090 | 0.36 | 14.9 |
| day 13 (n=2, low SPOT) | 0.81 | 0.96 | 0.099 | 0.85 | 8.1 |
| day 20 (n=1, low SPOT) | 0.22 | 0.79 | 0.064 | 0.27 | 3.3 |
| GM12878 (n=2) | 0.028 | 2.17 | 0.127 | 0.013 | 0.25 |
| HepG2 (n=2) | 0.041 | 1.71 | 0.118 | 0.024 | 0.35 |
| CD14+ monocyte (n=1) | 0.032 | 2.61 | 0.340 | 0.012 | 0.094 |
| K562 (n=1, Duke) | 0.057 | 1.11 | 0.052 | 0.051 | 1.1 |

All days and all 26 files, with every raw window mean, are in `results.json` (`files`, `contrasts.denominator_check.by_label`).

**Contrasts.**

1. Lineage: separated on all four predeclared metrics. `E58/G` is 0.27–0.85 in the 11 late erythroid files and 0.012–0.024 in the 5 non-erythroid files. Raw E58 (0.215–1.08 against 0.023–0.043) and `E58/B` (3.3–13.5 against 0.09–0.36) are also separated.
2. GM12878: the BCL11A promoter is more enriched than E58 in both replicates (`P/B` 6.6 and 1.8; `E58/B` 0.35 and 0.14). Only replicate 1 reaches the predeclared "accessible" label (≥ 3) at the promoter. E58 is below it in both. In GM12878, BG_up is 3–5× BG_down, which inflates B.
3. Timing: every day-4 replicate exceeds the day-0 maximum on `E58/G`, and also on raw E58 and on `E58/B`. After day 8, `E58/G` keeps rising to day 13, but raw E58 and `E58/B` peak at day 8 and then fall, while G falls about fourfold. The late rise of the ratio comes from its denominator. It is not further opening. This is one donor's culture, and days 11, 13 and 20 have low SPOT scores.
4. K562: `E58/B` is 1.1, with no element at or above 3 (one replicate, a different lab and protocol).
5. Sub-elements: E55 has the highest signal in all 11 late erythroid files; mean `E/G` is 1.09 for E55, 0.54 for E58 and 0.66 for E62.

**Denominators.** G varies 7.5-fold across files (0.68–5.12, CV 0.56) and is lowest in the low-SPOT late time points. B is 0.052–0.340 across all files (CV 0.55), mainly because of the monocyte and GM12878 BG_up windows. Within the time course it is 0.063–0.115.

**Audit of the agent's report.** Every value in its JSON block equals its own tool result for that file and window. Its rounded table (26 rows × 5 values) matches the replay, and all six of its contrast calls agree with the replay (it gave `null` for the GM12878 promoter label because the replicates disagree). Errors in its prose: the call log says "21 `compare_samples`" (the transcript has 18); it gives the late-erythroid `max(E)/B` range as "4.9–21" (exact: 4.9–36.2); and it says `compare_samples` "echoed back the accession it resolved from each URL" (the server echoes the accession it was given). It could not confirm the file-to-experiment mapping of individual files through `list_files` pagination within its budget, and says so.

**The agent's follow-up hypothesis** (its own, untested): the fall in raw E58 after day 8 reflects falling library signal-to-noise rather than closing chromatin. Its proposed test uses public data only: relate each file's E58 to its ENCODE SPOT score, and express E58 as a fraction of in-peak signal. A second donor's time course would be a stronger test.

## Limitations

- The target and panel were preselected, and the biology is published. The agent did not choose them.
- The erythroid time course is one adult donor's in vitro culture. Its replicates are not independent donors, and no inferential test is done. Cell lines are not tissues.
- The panel is not identically processed. Thirteen experiments are from the UW lab with ENCODE4 pipeline v3.0.0-alpha.2. K562 is from the Duke lab (a different DNase-seq protocol) with v3.0.0, and has one replicate. The monocyte experiment also has one replicate.
- ENCODE flags days 11, 13 and 20 as "extremely low spot score" (low signal-to-noise); days 18 and 20 have one replicate. None were dropped.
- `E / G` (enhancer over the GAPDH promoter) is a descriptive reference ratio, not a correction for library quality. Raw `E`, `G` and background values are reported with it.
- Accessibility is not enhancer activity, and neither shows causality. No clinical interpretation.
- pyBigWig range reads do not check the files' MD5s, which cover whole files. The replay and the MCP server read the same public objects independently.
