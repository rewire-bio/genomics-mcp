All measurements are in. Here is the report.

---

# BCL11A intron-2 enhancer accessibility in public ENCODE DNase-seq

## 1. Coordinates

All GRCh38, 0-based half-open. **Contig naming:** Ensembl/NCBI returned the chromosome as `2` (RefSeq `NC_000002.12`) and `12`; the ENCODE bigWigs use `chr2` and `chr12`. I queried the bigWigs with `chr2`/`chr12` and the server confirmed contig lengths 242,193,529 and 133,275,309, which are the GRCh38 primary-assembly lengths. This is a name change within one assembly — nothing was lifted over.

**Anchors** (`normalize_variant`, source `ncbi_variation`/dbSNP build 157; `a = VCF POS − 1`):

| rsID | VCF POS | `a` (0-based) | note |
| --- | --- | --- | --- |
| rs7606173 | 60498316 | 60498315 | multi-allelic (G>A/C/T), all at one position |
| rs6706648 | 60494905 | 60494904 | multi-allelic (C>G/T), all at one position |
| rs6738440 | 60495106 | 60495105 | single ALT; SPDI `NC_000002.12:60495105:A:G` |
| rs1427407 | 60490908 | 60490907 | multi-allelic (T>C/G), all at one position |

**TSS** (`lookup_gene`, Ensembl REST release 116):

| Gene | Canonical transcript | Strand | TSS (0-based) | Rule |
| --- | --- | --- | --- | --- |
| BCL11A | ENST00000642384.2 (= MANE Select) | −1 | 60553653 | transcript end 60553654 − 1 |
| GAPDH | ENST00000229239.10 (= MANE Select) | +1 | 6534516 | transcript start |

**Windows:**

| Window | Contig | Start | End | Derivation |
| --- | --- | --- | --- | --- |
| E55 | chr2 | 60497815 | 60498815 | `a(rs7606173) ± 500` |
| E58 | chr2 | 60494504 | 60495504 | `c = floor((60494904+60495105)/2) = 60495004`; `c ± 500` |
| E62 | chr2 | 60490407 | 60491407 | `a(rs1427407) ± 500` |
| BG_up | chr2 | 60508315 | 60518315 | `a(rs7606173) + 10000 … + 20000` |
| BG_down | chr2 | 60470907 | 60480907 | `a(rs1427407) − 20000 … − 10000` |
| P | chr2 | 60553153 | 60554153 | BCL11A TSS 60553653 ± 500 |
| G | chr12 | 6534016 | 6535016 | GAPDH TSS 6534516 ± 500 |

Arithmetic consistency check (mine, not predeclared): the anchors sit 55.3 kb, 58.6 kb and 62.7 kb downstream of the BCL11A TSS in the direction of transcription (minus strand), matching the published +55/+58/+62 names. All five chr2 windows fall inside the BCL11A gene body (60450519–60554467), so both background windows are intronic as specified.

## 2. File verification

What I checked, and why I chose these checks:

- **Every one of the 26 panel files was read successfully** at all 7 windows. `compare_samples` echoed back the accession it resolved from each URL, and all 182 file×window values returned `status: ok`, `exact: true`, `truncation: null`. No file was silently dropped.
- **Method is the predeclared one:** every value came back as `pyBigWig 0.3.26 stats(exact=True) via HTTP byte ranges` with `summary.exact = true` and the note "summary and bins use exact=True (full-resolution data, not zoom levels)". Bins were `count: 1`, size 1000 or 10000, `aligned: true` — i.e. one exact mean per window, as specified.
- **Assembly is asserted, not verified.** The server explicitly reports `assembly.status: "file_metadata_asserted"` with the note "bigWig/bigBed headers do not record an assembly". GRCh38 rests on ENCODE metadata plus the matching contig lengths; the file itself cannot confirm it. This is a real limit, not a discrepancy.
- **Biosample labels** for the two experiments carrying the most interpretive weight: `describe_dataset` confirms ENCSR000EMT = "DNase-seq on human GM12878", assay DNase-seq, `bio_replicate_count: 2`, `replication_type: "isogenic"`; and ENCSR000EKS = "Duke human K562 DNase-seq", `bio_replicate_count: 1`, `tech_replicate_count: 3`, `replication_type: "unreplicated"`.

Discrepancies and things I could **not** verify:

- **I could not confirm the file→experiment mapping for the individual panel accessions.** `list_files` paginates alphabetically, 15 accessions per page, over 122–162 files per experiment; the panel files fall beyond page 1 and chasing them would have consumed most of the remaining budget. The file→experiment/replicate assignments in my tables are taken from the panel as supplied, not independently re-derived.
- `list_files` on ENCSR000EMT did show that this experiment mixes assemblies (hg19 and GRCh38), output types ("raw signal", "signal of unique reads") and statuses (`archived`, `revoked`) — so the panel's selection rule (read-depth normalized signal / GRCh38 / ENCODE4 default analysis) is doing real work. I did not verify that the two GM12878 files used are the ones that rule selects.
- ENCODE's `replication_type: "isogenic"` for GM12878 confirms its two replicates are replicates of one cell line, not independent donors.
- No contradiction found with the panel's audit notes.

## 3. Observations (abbreviated; the JSON block is complete)

`B = (mean(BG_up) + mean(BG_down)) / 2`. All ratios below are **mine and approximate, rounded to 2 significant figures**; the exact values are recomputed by the deterministic replay. Raw values are at full precision in the JSON.

| Label | rep | file | E58 | G | B | E58/G | E58/B |
| --- | --- | --- | --- | --- | --- | --- | --- |
| day 0 | 1 | ENCFF648LHT | 0.037 | 2.96 | 0.076 | 0.012 | 0.48 |
| day 0 | 2 | ENCFF275EWP | 0.035 | 1.84 | 0.079 | 0.019 | 0.45 |
| day 4 | 1 | ENCFF644AXQ | 0.149 | 4.03 | 0.072 | 0.037 | 2.1 |
| day 4 | 2 | ENCFF284MYJ | 0.375 | 5.12 | 0.065 | 0.073 | 5.8 |
| day 6 | 1 | ENCFF633NER | 0.876 | 4.61 | 0.085 | 0.19 | 10 |
| day 6 | 2 | ENCFF434ACR | 0.916 | 3.40 | 0.101 | 0.27 | 9.1 |
| day 8 | 1 | ENCFF141GMK | 1.170 | 3.29 | 0.084 | 0.36 | 14 |
| day 8 | 2 | ENCFF814GPA | 1.456 | 3.96 | 0.093 | 0.37 | 16 |
| day 8 | 3 | ENCFF774LKB | 1.424 | 3.87 | 0.093 | 0.37 | 15 |
| day 11 | 1 | ENCFF746ODB | 0.331 | 0.683 | 0.075 | 0.48 | 4.4 |
| day 11 | 2 | ENCFF878PBI | 0.757 | 2.18 | 0.084 | 0.35 | 9.0 |
| day 11 | 3 | ENCFF384JNG | 1.081 | 1.79 | 0.082 | 0.61 | 13 |
| day 13 | 1 | ENCFF271NTU | 0.629 | 0.747 | 0.083 | 0.84 | 7.6 |
| day 13 | 2 | ENCFF462KEZ | 0.997 | 1.17 | 0.115 | 0.85 | 8.7 |
| day 15 | 1 | ENCFF322YXK | 0.829 | 1.40 | 0.094 | 0.59 | 8.8 |
| day 15 | 2 | ENCFF404BKX | 1.262 | 1.96 | 0.093 | 0.65 | 14 |
| day 17 | 1 | ENCFF841CYA | 0.522 | 1.32 | 0.066 | 0.40 | 7.9 |
| day 17 | 2 | ENCFF694WKV | 0.506 | 0.957 | 0.070 | 0.53 | 7.2 |
| day 18 | 1 | ENCFF028ZFZ | 0.394 | 1.04 | 0.063 | 0.38 | 6.2 |
| day 20 | 1 | ENCFF338HAQ | 0.215 | 0.786 | 0.064 | 0.27 | 3.3 |
| K562 | 1 | ENCFF972GVB | 0.057 | 1.11 | 0.052 | 0.051 | 1.1 |
| GM12878 | 1 | ENCFF428XFI | 0.032 | 2.76 | 0.092 | 0.012 | 0.35 |
| GM12878 | 2 | ENCFF960FMM | 0.023 | 1.58 | 0.161 | 0.015 | 0.14 |
| HepG2 | 1 | ENCFF546MZK | 0.039 | 1.64 | 0.116 | 0.024 | 0.34 |
| HepG2 | 2 | ENCFF113VII | 0.043 | 1.79 | 0.120 | 0.024 | 0.36 |
| CD14 mono | 2 | ENCFF389PZY | 0.032 | 2.61 | 0.340 | 0.012 | 0.094 |

**Stability of the denominators.** `G` is *not* stable across this panel: it spans 0.683 (ENCFF746ODB, day 11) to 5.116 (ENCFF284MYJ, day 4), a ~7.5-fold range, and drops sharply from day 11 onward — exactly the days ENCODE flags `extremely low spot score`. `B`, by contrast, is stable across the whole time course (0.063–0.115). This matters for contrast 3 and I return to it there.

## 4. Contrasts

### Contrast 1 — Lineage (late erythroid days 11–20, 11 files vs non-erythroid GM12878/HepG2/CD14, 5 files)

| Metric | Late erythroid range | Non-erythroid range | Separated? |
| --- | --- | --- | --- |
| E58 / G | 0.27 – 0.85 | 0.012 – 0.024 | **yes** (min 0.27 > max 0.024, ~11×) |
| max(E55,E58,E62) / G | 0.40 – 1.7 | 0.032 – 0.086 | **yes** (min 0.40 > max 0.086, ~4.7×) |
| E58 / B | 3.3 – 14 | 0.094 – 0.36 | **yes** (min 3.3 > max 0.36, ~9×) |
| max(E) / B | 4.9 – 21 | 0.41 – 1.3 | **yes** (min 4.9 > max 1.3) |

Separation holds on every replicate, on both the reference ratio and the local-background enrichment. Denominator check: it is not a `G` artefact — raw `mean(E58)` is 0.215–1.081 in the late erythroid files versus 0.023–0.043 in the non-erythroid files, non-overlapping by ~5×. All 11 late erythroid files meet the descriptive `enrichment ≥ 3` label at E58; none of the 5 non-erythroid files does.

### Contrast 2 — Promoter/enhancer uncoupling in GM12878

| file | P / B | E58 / B | ratio |
| --- | --- | --- | --- |
| ENCFF428XFI (rep 1) | 6.6 | 0.35 | ~19× |
| ENCFF960FMM (rep 2) | 1.8 | 0.14 | ~13× |

The **direction is consistent in both replicates**: the BCL11A promoter is 13–19× more enriched than E58. But the descriptive `≥ 3` label is met by rep 1 (6.6) and not rep 2 (1.8), so I report `gm12878_promoter_accessible` as **null** rather than force a call. `gm12878_E58_accessible` is unambiguously **false** — both replicates are below 0.4, an order of magnitude under the threshold.

A note on why the replicates disagree, flagged as a post-hoc check and **not** a substitute for the predeclared metric: GM12878's `B` is inflated by `BG_up` specifically (rep 1 0.141, rep 2 0.271) against `BG_down` (0.043, 0.051). The protocol warns that `B` "can contain other elements"; that appears to be happening here. Against `BG_down` alone both replicates would clear the threshold (14.1 and 5.6). I report the predeclared value and flag this; I am not substituting the convenient denominator.

### Contrast 3 — Timing (first day on which all replicates exceed the maximum day-0 replicate, E58/G)

Day-0 maximum = **0.019** (ENCFF275EWP). Day 4 gives 0.037 and 0.073 — both above it.

**First such day = day 4.**

| Day | E58/G per replicate | all > 0.019? |
| --- | --- | --- |
| 4 | 0.037, 0.073 | **yes** |
| 6 | 0.19, 0.27 | yes |
| 8 | 0.36, 0.37, 0.37 | yes |
| 11 | 0.48, 0.35, 0.61 | yes |
| 13 | 0.84, 0.85 | yes |
| 15 | 0.59, 0.65 | yes |
| 17 | 0.40, 0.53 | yes |
| 18 | 0.38 | yes |
| 20 | 0.27 | yes |

**Denominator check (this is where it bites).** The day-4 call is robust: it reproduces on raw `mean(E58)` (day 0 max 0.037; day 4 = 0.149, 0.375) and on `E58/B` (day 0 max 0.48; day 4 = 2.1, 5.8). And at day 4 `G` is *higher* than at day 0, so the early rise cannot be a shrinking denominator.

But the **apparent continued rise of E58/G to a peak at day 13 (0.85) is substantially a falling-`G` effect and I do not describe it as the enhancer opening further.** Raw `mean(E58)` peaks at **day 8** (1.17–1.46) and then declines monotonically to day 20 (0.215). `E58/B` also peaks at day 8 (14–16). Over the same span `G` falls from ~3.3–4.0 (day 8) to 0.68–2.18 (day 11) and 0.75–1.17 (day 13). So E58/G keeps climbing past day 8 only because its denominator collapses faster than its numerator. The days where this happens are precisely those ENCODE flags `extremely low spot score` (11, 13, 20). On the better-behaved local denominator, the picture is: opening begins by day 4, completes by roughly day 8, and the element stays above the enrichment threshold (day 20 E58/B = 3.3) through the end of culture.

This is one donor, with 1–3 replicates per day, at the sequencing quality ENCODE reports. It is not a population-level timing.

### Contrast 4 — K562 (descriptive)

| E55/B | E58/B | E62/B | P/B | E58/G | max(E)/G |
| --- | --- | --- | --- | --- | --- |
| 1.3 | 1.1 | 0.68 | 1.8 | 0.051 | 0.061 |

K562 does **not** meet the `≥ 3` accessible label at any of the three elements. Its E58/G (0.051) sits above the non-erythroid references (0.012–0.024) but roughly 5× below the lowest late-erythroid file (0.27). I report this descriptively and do not reconcile it. K562 is the one panel member from a different lab (Duke), a different DNase-seq protocol, and a different pipeline version (v3.0.0 vs v3.0.0-alpha.2), and ENCODE records it as `unreplicated` (1 biological, 3 technical replicates) — so I cannot separate a biological statement from a processing one here. An erythroleukaemia line is also not the same thing as differentiating primary progenitors.

### Contrast 5 — Sub-elements (late erythroid, descriptive)

**E55 is the highest of the three in 11 of 11 late erythroid files** — unanimous, with no replicate dissenting. Typical late-erythroid values: E55 0.31–3.39, E58 0.22–1.26, E62 0.23–1.64. E62 exceeds E58 in most of these files, so the descending order is generally E55 > E62 > E58.

`highest_element_late_erythroid = "E55"`.

## 5. Interpretation

Kept separate from the observations above.

The predeclared contrasts came out in the direction the published biology predicts, which is what this exercise was designed to check — this is a positive control on the panel and the pipeline, **not a discovery**. Bauer et al. (2013) described these sites as an erythroid enhancer required in erythroid but not B-lymphoid cells; Canver et al. (2015) mapped the critical sequences. What the data here add is only that this signature is recoverable from public ENCODE DNase-seq with a predeclared window definition.

Three readings I think the numbers support:

1. **The lineage contrast is strong and denominator-independent.** Separation holds on four different metrics, on every replicate, with no overlap. That it survives both `E/G` and `E/B` is the main reason I weight it.
2. **Opening is early in this culture, and complete well before the culture ends.** On the stable denominator, E58 rises by day 4, peaks around day 8, and stays above threshold through day 20. The day-13 peak in `E58/G` is a measurement artefact of declining library signal-to-noise, not a biological late peak.
3. **The promoter and the enhancer are decoupled in GM12878, in direction.** The promoter is 13–19× more enriched than E58 in both replicates, while E58 itself sits at background. This is consistent with a cell that expresses BCL11A without using this enhancer. It does not demonstrate that, because accessibility is not activity and neither shows which gene an element regulates.

What I will not claim: nothing here is causal, nothing identifies the target gene of these elements, and nothing is clinical. Accessibility differences between files of different library quality are only approximately comparable. The time course is a single adult donor — its replicates are not independent donors, and in vitro culture is not in vivo erythropoiesis. K562, GM12878 and HepG2 are cell lines, not tissues.

## 6. One testable follow-up hypothesis

**Hypothesis.** The decline in raw `mean(E58)` after day 8 is an artefact of falling library signal-to-noise in the late time points, not a real closing of the enhancer. If so, a SPOT-matched or peak-normalised comparison should show E58 accessibility flat or still rising from day 8 to day 20, rather than falling ~6-fold.

**What would test it.** This is answerable with public data and no new experiments: the same ENCODE experiments publish per-replicate SPOT scores and called hotspot/peak files alongside the signal bigWigs. Regressing each file's raw `mean(E58)` on its SPOT score across all 20 time-course files, and comparing E58 signal expressed as a fraction of that file's total in-peak signal, separates the two explanations — a genuine closing would survive the normalisation, an artefact would not. A stronger design would add an independent second donor: a published in vitro erythroid differentiation time course from a different adult CD34+ donor processed through the same ENCODE4 uniform pipeline, which would also address the single-donor limitation that makes the current timing claim non-generalisable. A matched RNA-seq time course on the same cultures would separately test whether BCL11A expression tracks E58 accessibility, though it still would not establish that this element drives that expression.

## 7. Limitations

- **Single donor.** All 20 time-course files come from ENCDO937OUY. Replicates within a day are not independent donors, so no population-level or timing claim generalises. No inferential test is reported, because the design lacks the independent sampling one would require.
- **Panel is not internally comparable.** 13 experiments use ENCODE4 pipeline v3.0.0-alpha.2 (UW); K562 uses v3.0.0 from a different lab with a different DNase-seq protocol. Excluding the lab-custom fetal erythroblast analyses removed one source of non-comparability but did not make the rest comparable. Lab, protocol, pipeline version, depth and library quality all still differ.
- **Known quality problems, reported not corrected.** Days 11, 13 and 20 are flagged `extremely low spot score`; days 18 and 20 are unreplicated; K562 and the monocyte experiment have one biological replicate each. The monocyte file is replicate 2, with no replicate 1 in the panel.
- **`E/G` is descriptive only.** It is not a correction for library quality, and `G` is demonstrably unstable here (7.5× range, collapsing in the low-SPOT files). Where a ratio moved because `G` moved, I said so (contrast 3).
- **`B` is a local reference, not a null.** It can contain other elements, and in GM12878 it evidently does — `BG_up` is 3–5× `BG_down` there, which is what makes contrast 2's replicates disagree.
- **Assembly is asserted, not verified.** bigWig headers carry no assembly; GRCh38 rests on ENCODE metadata plus matching contig lengths.
- **File→experiment mapping was not independently verified** for the individual panel accessions (see §2). Labels and replicate numbers are as supplied by the panel.
- **rs6738440's reference base was not checked** — both Ensembl and NCBI nuccore timed out, so the tool returned `reference_check: not_checked`. The position comes from dbSNP and is unaffected; three of the four anchors are multi-allelic, but all alleles of each sit at a single position, so no window is ambiguous.
- Accessibility is not activity; neither shows causality or the regulated gene. Cell lines are not tissues. No clinical interpretation.

## 8. Call log

**29 MCP calls total** (budget 45). 6 reference lookups + 21 `compare_samples` + 3 `list_files` (metadata) + 2 `describe_dataset`. No `fetch_file`; no whole-file downloads; every region ≤ 100 kb (the 10 kb background windows were the largest); `max_records` set to the file count on every call.

**Failures and retries:**

- **1 partial reference lookup:** `normalize_variant(rs6738440)` returned `status: partial` — `ncbi_variation` succeeded (giving the position) but Ensembl `sequence/region` timed out at 13.6 s and `ncbi_nuccore` had its deadline exhausted, so REF was not verified. Not retried: the position I needed was already resolved, and a retry would only have re-checked a base that does not affect any window.
- **4 partial `compare_samples` calls, 27 per-file timeouts.** I issued 6 measurement calls in one round (96 concurrent bigWig byte-range fetches) and overloaded the server's 30 s per-file budget: BG_up lost 5 files, BG_down 6, P 8, G 8. All were flagged `retryable`, and the affected files were reported under `unavailable_files` rather than silently zeroed.
- **4 retry calls, all successful**, recovering all 27 values. I diagnosed the cause as my own concurrency, not bad files — the same accessions had already succeeded at other windows — so I dropped to two calls per round, and one call per round for the 10 kb windows. No file was retried more than once, and no call was retried verbatim.

Final coverage: **182/182 file×window values obtained**, no nulls, no truncation.

```json
{
  "anchors": {
    "rs7606173": {"vcf_pos": 60498316, "source": "ncbi_variation (dbSNP build 157) via normalize_variant; multi-allelic G>A/C/T, all candidates at one position"},
    "rs6706648": {"vcf_pos": 60494905, "source": "ncbi_variation (dbSNP build 157) via normalize_variant; multi-allelic C>G/T, all candidates at one position"},
    "rs6738440": {"vcf_pos": 60495106, "source": "ncbi_variation (dbSNP build 157) via normalize_variant; SPDI NC_000002.12:60495105:A:G; REF not checked (ensembl and ncbi_nuccore timed out)"},
    "rs1427407": {"vcf_pos": 60490908, "source": "ncbi_variation (dbSNP build 157) via normalize_variant; multi-allelic T>C/G, all candidates at one position"}
  },
  "tss": {
    "BCL11A": {"transcript": "ENST00000642384.2", "strand": -1, "tss_0based": 60553653, "source": "ensembl REST release 116 via lookup_gene; canonical and MANE Select; transcript end 60553654 minus 1"},
    "GAPDH": {"transcript": "ENST00000229239.10", "strand": 1, "tss_0based": 6534516, "source": "ensembl REST release 116 via lookup_gene; canonical and MANE Select; transcript start"}
  },
  "windows": {
    "E55": {"contig": "chr2", "start": 60497815, "end": 60498815},
    "E58": {"contig": "chr2", "start": 60494504, "end": 60495504},
    "E62": {"contig": "chr2", "start": 60490407, "end": 60491407},
    "BG_up": {"contig": "chr2", "start": 60508315, "end": 60518315},
    "BG_down": {"contig": "chr2", "start": 60470907, "end": 60480907},
    "P": {"contig": "chr2", "start": 60553153, "end": 60554153},
    "G": {"contig": "chr12", "start": 6534016, "end": 6535016}
  },
  "files": [
    {"file": "ENCFF648LHT", "experiment": "ENCSR098PTC", "label": "EPO culture day 0", "biological_replicate": 1,
     "mean": {"E55": 0.1017276043817401, "E58": 0.03655854641646147, "E62": 0.081342021971941, "BG_up": 0.09981782517395914, "BG_down": 0.05308612158112228, "P": 2.667924266695976, "G": 2.9588830634355543}},
    {"file": "ENCFF275EWP", "experiment": "ENCSR098PTC", "label": "EPO culture day 0", "biological_replicate": 2,
     "mean": {"E55": 0.08018202155828476, "E58": 0.03539374418929219, "E62": 0.108969637747854, "BG_up": 0.09451537254918367, "BG_down": 0.0635024249668233, "P": 1.7983270496428012, "G": 1.8449502627253533}},
    {"file": "ENCFF644AXQ", "experiment": "ENCSR148VUP", "label": "EPO culture day 4", "biological_replicate": 1,
     "mean": {"E55": 0.4965295668691397, "E58": 0.14934232990443708, "E62": 0.32188170838728547, "BG_up": 0.08882443613372742, "BG_down": 0.05543756420947611, "P": 1.8184404938519, "G": 4.02629337143898}},
    {"file": "ENCFF284MYJ", "experiment": "ENCSR148VUP", "label": "EPO culture day 4", "biological_replicate": 2,
     "mean": {"E55": 0.6922963909804821, "E58": 0.3746801892369986, "E62": 0.5430965255405754, "BG_up": 0.07836478740070016, "BG_down": 0.050829209138639274, "P": 2.2307459523528816, "G": 5.116195534467697}},
    {"file": "ENCFF633NER", "experiment": "ENCSR564JUY", "label": "EPO culture day 6", "biological_replicate": 1,
     "mean": {"E55": 1.3700117963552474, "E58": 0.8758673620820046, "E62": 0.9254607259277254, "BG_up": 0.09029432783275843, "BG_down": 0.07920823944229632, "P": 1.789829987153411, "G": 4.608875833749771}},
    {"file": "ENCFF434ACR", "experiment": "ENCSR564JUY", "label": "EPO culture day 6", "biological_replicate": 2,
     "mean": {"E55": 1.4478924903273582, "E58": 0.9157111086249351, "E62": 0.9139497090131045, "BG_up": 0.09445062422705815, "BG_down": 0.1076064663629979, "P": 1.4095907240509986, "G": 3.395281455874443}},
    {"file": "ENCFF141GMK", "experiment": "ENCSR845CFB", "label": "EPO culture day 8", "biological_replicate": 1,
     "mean": {"E55": 2.6765867501497267, "E58": 1.169574056327343, "E62": 1.4832679538950324, "BG_up": 0.06898491541855037, "BG_down": 0.09854556237086654, "P": 1.3089501006156206, "G": 3.2926248766183854}},
    {"file": "ENCFF814GPA", "experiment": "ENCSR845CFB", "label": "EPO culture day 8", "biological_replicate": 2,
     "mean": {"E55": 3.131937365680933, "E58": 1.456482793301344, "E62": 1.5115924578905107, "BG_up": 0.07880212963931263, "BG_down": 0.10784220246672631, "P": 1.5957549624741076, "G": 3.962686476349831}},
    {"file": "ENCFF774LKB", "experiment": "ENCSR845CFB", "label": "EPO culture day 8", "biological_replicate": 3,
     "mean": {"E55": 4.046009455621243, "E58": 1.4236896945238113, "E62": 1.5526187573224306, "BG_up": 0.07943924652785063, "BG_down": 0.10738770549651236, "P": 1.6056040392518043, "G": 3.873287312269211}},
    {"file": "ENCFF746ODB", "experiment": "ENCSR420NOA", "label": "EPO culture day 11", "biological_replicate": 1,
     "mean": {"E55": 0.8016643208265305, "E58": 0.3307315929532051, "E62": 0.5709749580398202, "BG_up": 0.0690760539183393, "BG_down": 0.08115052357949316, "P": 0.19928080209344626, "G": 0.6829240776896477}},
    {"file": "ENCFF878PBI", "experiment": "ENCSR420NOA", "label": "EPO culture day 11", "biological_replicate": 2,
     "mean": {"E55": 1.9875196918845177, "E58": 0.7565685600936413, "E62": 0.9582594863176346, "BG_up": 0.06894894395023585, "BG_down": 0.09904985515326262, "P": 0.665530960187316, "G": 2.1779332369565965}},
    {"file": "ENCFF384JNG", "experiment": "ENCSR420NOA", "label": "EPO culture day 11", "biological_replicate": 3,
     "mean": {"E55": 1.94415823135525, "E58": 1.0806607269644737, "E62": 1.0314801679700614, "BG_up": 0.06109150585345924, "BG_down": 0.10279130662232637, "P": 0.4939727784246206, "G": 1.7857274955511093}},
    {"file": "ENCFF271NTU", "experiment": "ENCSR855FOP", "label": "EPO culture day 13", "biological_replicate": 1,
     "mean": {"E55": 1.1915444418787957, "E58": 0.628984040260315, "E62": 0.5894711396172643, "BG_up": 0.07558945635519922, "BG_down": 0.09100760285444558, "P": 0.15192133285850287, "G": 0.7467400895655155}},
    {"file": "ENCFF462KEZ", "experiment": "ENCSR855FOP", "label": "EPO culture day 13", "biological_replicate": 2,
     "mean": {"E55": 1.6744590348005295, "E58": 0.9972835114002228, "E62": 1.0753217138648032, "BG_up": 0.08868805735185742, "BG_down": 0.14173604196086526, "P": 0.38344067902863027, "G": 1.171160287618637}},
    {"file": "ENCFF322YXK", "experiment": "ENCSR937UWI", "label": "EPO culture day 15", "biological_replicate": 1,
     "mean": {"E55": 1.723445815294981, "E58": 0.8292400559186935, "E62": 1.2059808525294065, "BG_up": 0.08309921318991109, "BG_down": 0.10515767973251641, "P": 0.30689160434901713, "G": 1.4009909586906433}},
    {"file": "ENCFF404BKX", "experiment": "ENCSR937UWI", "label": "EPO culture day 15", "biological_replicate": 2,
     "mean": {"E55": 3.3854275166988375, "E58": 1.2624417514801025, "E62": 1.6442744879722595, "BG_up": 0.0760261573754251, "BG_down": 0.11089331129714847, "P": 0.46081756229698656, "G": 1.9558925958871842}},
    {"file": "ENCFF841CYA", "experiment": "ENCSR362JSZ", "label": "EPO culture day 17", "biological_replicate": 1,
     "mean": {"E55": 1.074771709740162, "E58": 0.5217754639983178, "E62": 0.670537039488554, "BG_up": 0.06119366676360369, "BG_down": 0.07161941607389599, "P": 0.3434522749632597, "G": 1.32018359208107}},
    {"file": "ENCFF694WKV", "experiment": "ENCSR362JSZ", "label": "EPO culture day 17", "biological_replicate": 2,
     "mean": {"E55": 1.0382754804193974, "E58": 0.5057291307449341, "E62": 0.7094575281441212, "BG_up": 0.06722030922025442, "BG_down": 0.07315040332563222, "P": 0.29002794210612776, "G": 0.9571813473701477}},
    {"file": "ENCFF028ZFZ", "experiment": "ENCSR493IAY", "label": "EPO culture day 18", "biological_replicate": 1,
     "mean": {"E55": 0.5713056157529354, "E58": 0.3936477569937706, "E62": 0.4712599732130766, "BG_up": 0.06061525994166732, "BG_down": 0.06550949727930129, "P": 0.3630372677147388, "G": 1.037249329328537}},
    {"file": "ENCFF338HAQ", "experiment": "ENCSR115YPI", "label": "EPO culture day 20", "biological_replicate": 1,
     "mean": {"E55": 0.314160166233778, "E58": 0.21542412745952605, "E62": 0.23345030594617128, "BG_up": 0.06008111631125212, "BG_down": 0.06858936129622162, "P": 0.34841985842585566, "G": 0.786393728852272}},
    {"file": "ENCFF972GVB", "experiment": "ENCSR000EKS", "label": "K562", "biological_replicate": 1,
     "mean": {"E55": 0.0676031993702054, "E58": 0.056983551394194365, "E62": 0.035076868396485224, "BG_up": 0.050517277079168706, "BG_down": 0.052641821988206355, "P": 0.09068915827572346, "G": 1.1125012863874435}},
    {"file": "ENCFF428XFI", "experiment": "ENCSR000EMT", "label": "GM12878", "biological_replicate": 1,
     "mean": {"E55": 0.11642304934561253, "E58": 0.032302348017692564, "E62": 0.07209711630642414, "BG_up": 0.1413452812358737, "BG_down": 0.04328068261295557, "P": 0.6088548810333013, "G": 2.7611357617378234}},
    {"file": "ENCFF960FMM", "experiment": "ENCSR000EMT", "label": "GM12878", "biological_replicate": 2,
     "mean": {"E55": 0.13628065135329961, "E58": 0.02323642033338547, "E62": 0.06449670592695475, "BG_up": 0.2714461602382362, "BG_down": 0.051226483592018486, "P": 0.2876806506216526, "G": 1.5820484157204628}},
    {"file": "ENCFF546MZK", "experiment": "ENCSR149XIL", "label": "HepG2", "biological_replicate": 1,
     "mean": {"E55": 0.0566667890176177, "E58": 0.03922940889000893, "E62": 0.05469407745823264, "BG_up": 0.06679047015495598, "BG_down": 0.16570700353421272, "P": 0.1341206355765462, "G": 1.63624607026577}},
    {"file": "ENCFF113VII", "experiment": "ENCSR149XIL", "label": "HepG2", "biological_replicate": 2,
     "mean": {"E55": 0.056335533391684296, "E58": 0.043204703144729135, "E62": 0.041748663473874334, "BG_up": 0.0633827706053853, "BG_down": 0.17686174692586065, "P": 0.13168958877399564, "G": 1.7852220332622528}},
    {"file": "ENCFF389PZY", "experiment": "ENCSR000EPK", "label": "CD14-positive monocyte", "biological_replicate": 2,
     "mean": {"E55": 0.11660463105887174, "E58": 0.032125224858522414, "E62": 0.1392312427125871, "BG_up": 0.6001600616499781, "BG_down": 0.0804349249534309, "P": 0.9234955516755581, "G": 2.6107386062145235}}
  ],
  "contrast_calls": {
    "lineage_separated_E58_over_G": true,
    "lineage_separated_max_over_G": true,
    "gm12878_promoter_accessible": null,
    "gm12878_E58_accessible": false,
    "first_day_all_reps_above_day0_E58_over_G": 4,
    "highest_element_late_erythroid": "E55"
  },
  "calls": {"total": 29, "failed": 4, "retried": 4}
}
```

`gm12878_promoter_accessible` is `null` because the two replicates fall on opposite sides of the predeclared `enrichment ≥ 3` threshold (6.6 and 1.8); no single call is supported. `calls.failed` counts the 4 `compare_samples` calls that returned `status: partial` (27 per-file timeouts in total, caused by my own over-parallelisation); `calls.retried` counts the 4 recovery calls, all of which succeeded.
