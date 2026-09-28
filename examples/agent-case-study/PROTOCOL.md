# Protocol: BCL11A erythroid enhancer accessibility in public ENCODE DNase-seq

Status: predeclared. Written and committed before any signal value was read. Changes after measurement are listed at the end, with reasons.

## Question

Is chromatin at the HbF-associated BCL11A intron-2 enhancer (DNase I hypersensitive sites +55, +58 and +62) more accessible in erythroid cells than in non-erythroid cells? When does it open during in vitro erythroid differentiation of adult CD34+ progenitors? Is it open in GM12878, a B-lymphoblastoid line that expresses BCL11A?

This checks known biology with public data. It is not a discovery claim. Bauer et al. (2013) described these sites as an erythroid enhancer and reported, by genome engineering, that it is "required in erythroid but not B-lymphoid cells for BCL11A expression" (PMID 24115442). Canver et al. (2015) mapped its critical sequences by saturating mutagenesis (PMID 26375006). The sites are named by their distance in kb from the BCL11A transcription start site.

## Data (curated, not chosen by the agent)

`manifest.json`, produced by `discovery/select_files.py` from the ENCODE REST API. The raw responses, their URLs, UTC times and SHA-256 hashes are in `evidence/encode_metadata.json`.

- Assay: DNase-seq. Assembly: GRCh38. Output: `read-depth normalized signal` bigWig from each experiment's default analysis, which must be the ENCODE4 uniform pipeline. One file per biological replicate; no pooled files.
- Erythroid time course: every released GRCh38 DNase-seq experiment of donor ENCDO937OUY's hematopoietic multipotent progenitors, cultured with EPO, SCF (kit ligand), IL-3 and hydrocortisone for 0–20 days. 10 experiments, 20 files. All come from one adult donor, so biological replicates here are not independent donors.
- Reference cell types: K562 (erythroleukaemia line), GM12878 (B-lymphoblastoid), HepG2 (hepatoblastoma line) and CD14-positive monocytes. Rule: untreated, no ENCODE `ERROR` or `NOT_COMPLIANT` audit, ENCODE4 GRCh38 default analysis; prefer the UW lab (same lab as the time course), then exactly two biological replicates, then the earliest release, then the smallest accession. 4 experiments, 6 files.
- Excluded: fetal erythroblast DNase-seq (ENCSR059YWJ, ENCSR514POY) has only a lab-custom default analysis. Mixing pipelines would make values not comparable.
- ENCODE audits are reported, not used to drop time-course data: days 11, 13 and 20 are flagged `extremely low spot score` (low signal-to-noise); days 18 and 20 are unreplicated. K562 (Duke lab, a different DNase protocol) and the monocyte experiment have one replicate each.

## Coordinates

Everything is GRCh38, 0-based half-open. No liftover. Ensembl/NCBI name the chromosome `2`; the ENCODE bigWigs name it `chr2`. This is a naming change within one assembly, and it must be stated.

Anchors are published variants inside each site, as assigned in Sebastiani et al. 2015, Table 2 (PMID 25703683, PMC4341902): rs7606173 (+55), rs6706648 and rs6738440 (+58), rs1427407 (+62). For an anchor with VCF POS `p`, `a = p - 1` (the 0-based position; equal to the SPDI position).

| Window | Definition |
| --- | --- |
| `E55` | `[a(rs7606173) - 500, a(rs7606173) + 500)` |
| `E58` | centre `c = floor((a(rs6706648) + a(rs6738440)) / 2)`; `[c - 500, c + 500)` |
| `E62` | `[a(rs1427407) - 500, a(rs1427407) + 500)` |
| `BG_up` | `[a(rs7606173) + 10000, a(rs7606173) + 20000)`, intronic, towards the promoter |
| `BG_down` | `[a(rs1427407) - 20000, a(rs1427407) - 10000)`, intronic |
| `P` | BCL11A promoter: 1 kb centred on the 0-based TSS of the Ensembl canonical transcript (minus strand: TSS = transcript end - 1) |
| `G` | GAPDH promoter, a constitutively accessible control: 1 kb centred on the 0-based TSS of its Ensembl canonical transcript (plus strand: TSS = transcript start) |

Positions are resolved at run time from public reference tools and recorded with their source.

## Metrics (per file)

- `mean(W)`: exact mean read-depth normalized signal over window `W` (pyBigWig `stats(exact=True)`, bases without data ignored). `null` means no data; never zero.
- `B = (mean(BG_up) + mean(BG_down)) / 2`: local intronic background. It can contain other elements; it is a local reference, not a null.
- Enrichment: `mean(E*) / B` and `mean(P) / B`.
- Constitutive-normalised: `mean(E*) / mean(G)`. This is the primary cross-sample metric, because it partly corrects for library signal-to-noise (several time points have low SPOT scores).
- "Accessible" (descriptive label only): enrichment >= 3.

## Contrasts (descriptive; no p-values)

With one donor and one to three replicates per group, no significance test is valid. "Separated" means every replicate value of one group lies above every replicate value of the other.

1. Lineage: `E58 / G` and `max(E55, E58, E62) / G` in late erythroid culture (days 11–20, 11 files) versus non-erythroid references (GM12878, HepG2, CD14 monocyte; 5 files). The same with enrichment.
2. Promoter/enhancer uncoupling in GM12878: `P / B` versus `E58 / B`.
3. Timing: for each day, whether all replicates exceed the maximum day-0 replicate on `E58 / G`; the first such day.
4. K562: reported descriptively (erythroleukaemia line, different lab).
5. Sub-elements: which of +55/+58/+62 is highest in late erythroid files (descriptive).

## Replicates

Values are reported per file. Per experiment: n, mean and range across biological replicates. Technical replicates are not separate data points; each file here is one biological replicate. Time-course replicates share a donor. Cell lines are not tissues, and in vitro culture is not in vivo erythropoiesis.

## Interpretation limits

- Accessibility is not activity, and neither shows causality or which gene an element regulates.
- Read-depth normalized signal is comparable only within this assay and pipeline, and only approximately across libraries of different quality.
- No clinical interpretation.

## Changes after measurement

None yet.
