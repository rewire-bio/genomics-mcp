# Review of the original PRD

Reviewed 2026-09-24. Subject: `docs/prd-original.md` ("Genomics Reference MCP", Draft v1), kept unchanged for the record. The approved scope in `PRD.md` replaces it.

## Summary

The original PRD gets several principles right: provenance on every claim, no invented consensus, explicit assembly handling, partial results when one source fails, and research-only framing. Its main weakness is what it would actually deliver. As written, V1 is a federation of public REST APIs behind about ten MCP tools. That is useful but not new (BioMCP already does MIT-licensed API federation for agents), and it cannot answer the question that usually comes first: *what is in this sample or dataset at this locus?* It also leaves coordinate conventions unspecified, uses outdated source models (ClinVar, gnomAD), and puts licence-restricted sources (COSMIC) into V1.

## 1. Scope

| Issue | Detail | Consequence |
| --- | --- | --- |
| Reference-only | BAM/CRAM analysis and user data are non-goals; data access is pushed to a separate, unspecified "Genome Data MCP" (section 32). | Agents cannot move from an archive dataset or local file to evidence in one tool model. The two products would have to agree on coordinates, assemblies and variant representation anyway, so the split duplicates the hardest part. |
| Broad V1 source list | Eight sources in V1, including COSMIC (licensed) and AlphaGenome (keyed, terms-bound). | V1 acceptance depends on entitlements the project may not hold. |
| `query_source` escape hatch | Generic passthrough to any adapter (10.10). | Arbitrary upstream queries bypass limits, licence field filtering and egress rules. Not needed if typed tools are complete. |
| Overlapping tools | `get_population_evidence`, `get_clinical_evidence`, `get_cancer_evidence`, `get_functional_predictions` repeat `lookup_variant` with a filter. | More tools for the model to choose between with no new capability. `lookup_variant(sources=...)` covers them. |
| Remote deployment | "HTTP transport for team/server deployments" (25) with no authentication, binding or TLS requirements. | An unauthenticated network MCP server that can reach licensed sources. |
| Performance targets | p50 < 5 s uncached for a seven-source lookup (21). | gnomAD's public API allows about 10 requests/minute; the target cannot hold under any repeated use and must be stated per source. |
| No bounds | No limits on region size, records, response bytes or downloads. | One call can return megabytes of JSON or trigger unbounded transfers. |

## 2. Scientific errors and gaps

1. **Coordinates are undefined.** `position` appears without saying whether it is 0- or 1-based, and there is no interval type. VCF POS is 1-based, BED/pysam intervals are 0-based half-open, SPDI is 0-based. Silent off-by-one errors are the most common failure in this domain. The approved scope fixes intervals as 0-based half-open with an explicit assembly, and keeps VCF-style `pos` only in VCF-style variant input.
2. **"Resolve GRCh37 vs GRCh38" (12.5) cannot be done reliably from input alone.** Many positions are valid on both assemblies. The assembly must be supplied; liftover is lossy and must never be implicit. Goal 4.1.4 ("handle common identifier conversion automatically") conflicts with the PRD's own rule that nothing is silently transformed.
3. **"Normalize indels" is ambiguous.** VCF normalization left-aligns; HGVS applies the 3' rule. Both depend on the exact reference sequence. The output must say which convention was used and which reference checked the REF allele.
4. **rsIDs are not variants.** One rsID can cover several alleles, and rsIDs are merged over time. Resolution has to return all alleles and report ambiguity.
5. **HGVS protein is not reversible.** Codon degeneracy means `p.` notation usually maps to several genomic changes. "HGVS protein where resolvable" should be "returns candidate set, never a single guess".
6. **Transcript model is Ensembl-only.** No RefSeq accession, no version, no MANE Select/Plus Clinical pairing. The worked example resolves a RefSeq `NM_` transcript through source "ensembl" with no version handling, and RefSeq transcripts can differ from the genome at alignment gaps. The example coordinates were not independently re-checked in this review; they must be verified against a primary source in tests before use.
7. **ClinVar model is out of date.** Since January 2024 ClinVar reports three classification types: germline, somatic clinical impact, and oncogenicity, each with its own aggregate ([ClinVar docs](https://www.ncbi.nlm.nih.gov/clinvar/docs/clinsig/)). A single "clinical significance" field loses this. Assertion-level provenance means SCV records; the PRD lists only VCV/RCV.
8. **gnomAD fields lack context.** No dataset version (v2 on GRCh37, v4 on GRCh38), no exome/genome split, and allele frequency without allele number hides coverage. Hemizygous counts on X/Y are missing. Absence from gnomAD is not AF = 0 when the site is poorly covered. Constraint metrics differ between releases and must carry the release.
9. **Open Targets scores are not confidence.** Association scores are ranking heuristics and must be labelled that way.
10. **AlphaGenome Atlas fields are unverified.** The PRD names an "AlphaGenome Variant Impact (AVI) score"; the current Atlas API reference ([alphagenomedocs.com](https://www.alphagenomedocs.com/api/atlas.html)) does not show that field name in its index. Field names, assembly coverage and terms must come from current documentation when the adapter is built.

## 3. Licensing and terms

- **COSMIC** requires registration and, for commercial use, a paid licence, and restricts redistribution. It cannot be a V1 acceptance criterion for an open-source project without an entitlement. Deferred.
- **AlphaGenome** access is keyed and subject to provider terms. Optional only, with a user-supplied key.
- **UniProt** is CC BY 4.0: attribution must travel with the data.
- Other sources (ClinVar, gnomAD, Ensembl, HGNC, Open Targets, ENA, EGA, ENCODE, GEO, NCBI Datasets) each have their own terms. Each source entry carries a `terms_url` where confirmed; unconfirmed ones are left empty rather than guessed.
- **Controlled-access data** (EGA) needs DAC approval per dataset. Only EGA's documented public test data may be used in development.
- Open question 8 (hosted service querying non-commercial sources) is avoided by not operating any service: users run the server locally with their own credentials.

## 4. API wrappers vs actual data

The product thesis says value is "normalization + orchestration + provenance + policy-aware access", yet the acceptance criteria only require that adapters "are implemented". Nothing requires retrieving real records, and "recorded fixtures" can pass with mocks alone. Problems:

- An adapter that returns metadata or a link where data was requested looks like success to an agent.
- No criterion checks biological correctness (for example, that a region query matches `bcftools view -r` on the same file).
- There is no retrieval of the data researchers actually hold: archive files, indexed alignments and variant calls, signal tracks.

The approved scope requires actual records or local artifacts, golden fixtures compared with samtools/bcftools, and dated live demonstrations against real public sources. Metadata-only placeholders do not satisfy retrieval acceptance.

## 5. Security and privacy gaps

- No rule on sending values derived from private files (for example, variants in a patient VCF) to external APIs. The approved scope requires explicit per-call consent.
- No statement on ambient cloud credentials. A server with boto3/HTSlib installed will otherwise pick up whatever the shell provides.
- Log redaction is mentioned but signed URLs, tokens and private query content are not called out.
- Caching of licensed or private responses is left to adapters.

## 6. What changed in the approved scope

| Original | Approved (`PRD.md`) |
| --- | --- |
| Reference-only MCP; data MCP later | One MCP: archive discovery, bounded genomic retrieval, and reference evidence |
| ~10 tools incl. passthrough | 23 typed tools in five groups; no passthrough, no shell |
| Position convention unspecified | 0-based half-open intervals, explicit assembly, VCF `pos` only for VCF-style alleles |
| COSMIC in V1 | Deferred (licence) |
| AlphaGenome via client, possibly live | Optional precomputed Atlas lookups with a user key; no live inference |
| HTTP for team deployments | stdio and bearer-authenticated Streamable HTTP, loopback by default; no operated service |
| No limits | 1 Mb region, 10,000 records, 1 MiB response, 30 s, 100 MiB transfer default |
| "Adapters implemented" | Real records/artifacts, golden fixtures, live dated demos |
