# Protocol: BCL11A erythroid enhancer accessibility in public ENCODE DNase-seq

Status: predeclared. The question, panel, windows, metrics and contrasts were committed in c30f740 before any signal in the target or control windows was read. Earlier the same day the author made four response-size calibration `compare_samples` calls on an unrelated locus (the README demo interval chr1:[1000000, 1001000), the first 16 panel files) and reference lookups for the anchors and BCL11A (no signal). Wording corrections made later are listed at the end, with reasons. None of them changes a window, a metric formula, the panel or a predeclared contrast; one adds a descriptive denominator check.

## Question

Is chromatin at the HbF-associated BCL11A intron-2 enhancer (DNase I hypersensitive sites +55, +58 and +62) more accessible in erythroid cells than in non-erythroid cells? When does it open during in vitro erythroid differentiation of adult CD34+ progenitors? Is it open in GM12878, a B-lymphoblastoid line that expresses BCL11A?

This checks known biology with public data. It is not a discovery claim. Bauer et al. (2013) described these sites as an erythroid enhancer and reported, by genome engineering, that it is "required in erythroid but not B-lymphoid cells for BCL11A expression" (PMID 24115442). That loss-of-function comparison deleted the orthologous mouse enhancer in mouse erythroid (MEL) and mouse pre-B cell lines; the human evidence in that paper is chromatin data. Canver et al. (2015) mapped its critical sequences by saturating mutagenesis (PMID 26375006). The sites are named by their distance in kb from the BCL11A transcription start site.

## Data (curated, not chosen by the agent)

`manifest.json`, produced by `discovery/select_files.py` from the ENCODE REST API. The raw responses, their URLs, UTC times and SHA-256 hashes are in `evidence/encode_metadata.json`.

- Assay: DNase-seq. Assembly: GRCh38. Output: `read-depth normalized signal` bigWig from each experiment's default analysis, which must be an ENCODE4 uniform-pipeline analysis. One file per biological replicate; no pooled files. The retained files are not identically processed: 13 experiments (UW lab) use ENCODE4 pipeline v3.0.0-alpha.2, and K562 (Duke lab, a different DNase-seq protocol) uses v3.0.0.
- Erythroid time course: every released GRCh38 DNase-seq experiment of donor ENCDO937OUY's hematopoietic multipotent progenitors, cultured with EPO, SCF (kit ligand), IL-3 and hydrocortisone for 0–20 days. 10 experiments, 20 files. All come from one adult donor, so biological replicates here are not independent donors.
- Reference cell types: K562 (erythroleukaemia line), GM12878 (B-lymphoblastoid), HepG2 (hepatoblastoma line) and CD14-positive monocytes. Rule: untreated, no ENCODE `ERROR` or `NOT_COMPLIANT` audit, ENCODE4 GRCh38 default analysis; prefer the UW lab (same lab as the time course), then exactly two biological replicates, then the earliest release, then the smallest accession. 4 experiments, 6 files.
- Excluded: fetal erythroblast DNase-seq (ENCSR059YWJ, ENCSR514POY) has only a lab-custom default analysis. Excluding them removes one known source of non-comparability. It does not make the retained panel fully comparable: lab, protocol, pipeline version, sequencing depth and library quality still differ.
- ENCODE audits are reported, not used to drop time-course data: days 11, 13 and 20 are flagged `extremely low spot score` (low signal-to-noise); days 18 and 20 are unreplicated. K562 (Duke lab, a different DNase protocol) and the monocyte experiment have one replicate each.

## Coordinates

Everything is GRCh38, 0-based half-open. No liftover. Ensembl/NCBI name the chromosome `2`; the ENCODE bigWigs name it `chr2`. This is a naming change within one assembly, and it must be stated.

Anchors are published variants inside each site, as assigned in Sebastiani et al. 2015, Table 2 (PMID 25703683, PMC4341902): rs7606173 (+55), rs6706648 and rs6738440 (+58), rs1427407 (+62). For an anchor with VCF POS `p`, `a = p - 1` (the 0-based position; equal to the SPDI position).

| Window | Definition |
| --- | --- |
| `E55` | `[a(rs7606173) - 500, a(rs7606173) + 500)` |
| `E58` | centre `c = floor((a(rs6706648) + a(rs6738440)) / 2)`; `[c - 500, c + 500)` |
| `E62` | `[a(rs1427407) - 500, a(rs1427407) + 500)` |
| `BG_up` | `[a(rs7606173) + 10000, a(rs7606173) + 20000)`, local background towards the promoter |
| `BG_down` | `[a(rs1427407) - 20000, a(rs1427407) - 10000)`, local background |
| `P` | BCL11A promoter: 1 kb centred on the 0-based TSS of the Ensembl canonical transcript (minus strand: TSS = transcript end - 1) |
| `G` | GAPDH promoter, a constitutively accessible control: 1 kb centred on the 0-based TSS of its Ensembl canonical transcript (plus strand: TSS = transcript start) |

Positions are resolved at run time from public reference tools and recorded with their source.

## Metrics (per file)

- `mean(W)`: exact mean read-depth normalized signal over window `W` (pyBigWig `stats(exact=True)`, bases without data ignored). `null` means no data; never zero.
- `B = (mean(BG_up) + mean(BG_down)) / 2`: local background. It can contain other elements; it is a local reference, not a null.
- Enrichment: `mean(E*) / B` and `mean(P) / B`.
- Reference ratio: `mean(E*) / mean(G)`. This is the predeclared primary cross-sample ratio. It is descriptive only. Using a housekeeping promoter does not establish a correction for library signal-to-noise, and the ratio can change because the denominator changes. Raw `mean(E*)`, `mean(G)` and `B` are always reported with it, together with the stability of `G` across files.
- "Accessible" (descriptive label only): enrichment >= 3.

## Contrasts (descriptive; no p-values)

This descriptive case study performs no inferential tests. It lacks the independent donor sampling that a population-level claim would need: the time course is one donor, with one to three replicates per group. "Separated" means every replicate value of one group lies above every replicate value of the other.

1. Lineage: `E58 / G` and `max(E55, E58, E62) / G` in late erythroid culture (days 11–20, 11 files) versus non-erythroid references (GM12878, HepG2, CD14 monocyte; 5 files). The same with enrichment.
2. Promoter/enhancer uncoupling in GM12878: `P / B` versus `E58 / B`.
3. Timing: for each day, whether all replicates exceed the maximum day-0 replicate on `E58 / G`; the first such day. This describes this single-donor culture at the sequencing quality ENCODE reports (low SPOT at days 11, 13 and 20; days 18 and 20 unreplicated). It is not a population-level timing.
4. K562: reported descriptively (erythroleukaemia line, different lab).
5. Sub-elements: which of +55/+58/+62 is highest in late erythroid files (descriptive).

Denominator check (added after review; see below): for contrasts 1 and 3, the same comparison is also reported on raw `mean(E58)` and on `E58 / B`, with raw `G` and `B` per file and per day. A rising `E58 / G` with a falling `G` is not described as the enhancer opening.

## Replicates

Values are reported per file. Per experiment: n, mean and range across biological replicates. Technical replicates are not separate data points; each file here is one biological replicate. Time-course replicates share a donor. Cell lines are not tissues, and in vitro culture is not in vivo erythropoiesis.

## Interpretation limits

- Accessibility is not activity, and neither shows causality or which gene an element regulates.
- Read-depth normalized signal is comparable only within this assay and pipeline, and only approximately across libraries of different quality.
- No clinical interpretation.

## Changes after the first run

On 2026-09-28, after an interrupted first agent run (`runs/2026-09-28-interrupted/`, 9 MCP calls, no report) and an independent review, and before the complete run. The author had not inspected any target-window value. The interrupted run had returned `G` for the 6 reference-cell-type files to the agent only.

1. Status line: the chronology is stated exactly, including the unrelated calibration calls.
2. `E / G`: reworded from "partly corrects for library signal-to-noise" to a descriptive reference ratio, with raw values and `G` stability reported.
3. Data: the pipeline versions and lab/protocol difference of K562 are stated. Excluding lab-custom analyses is not claimed to make the panel comparable.
4. Contrasts: "no significance test is valid" replaced by the reason no inferential test is done. Timing is stated as conditional on this culture and its quality.
5. Added the denominator check (raw `E58` and `E58 / B` alongside `E58 / G`). It is reported in addition to the predeclared contrasts, not instead of them.

## Changes after the complete run (wording only)

On 2026-09-28, after the complete run and a second independent review. No window, formula, panel member, contrast or measured value changed.

6. The background windows were described as "intronic". They lie within the BCL11A gene span, but their intron/exon status was not checked against transcript structure, so they are now called local background windows.
7. The Bauer et al. quotation now states its model system: the erythroid versus B-lymphoid loss-of-function comparison was done in mouse cell lines. GM12878 was not tested there, so the GM12878 contrast is an inference from accessibility.
