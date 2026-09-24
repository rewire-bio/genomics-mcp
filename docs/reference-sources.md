# Reference sources and variant normalization (E8)

Package: `genomics_mcp.references`. Tools: `resolve_identifier`, `normalize_variant`, `lookup_variant`, `lookup_gene`, `lookup_protein`.

## Sources

| Source | Endpoint | Used for | Local limit | Docs / terms |
| --- | --- | --- | --- | --- |
| HGNC | rest.genenames.org `fetch/{field}`, `info` | approved symbol, aliases, previous symbols, cross-references, index date | 10/s | https://www.genenames.org/help/rest/ , https://www.genenames.org/about/license/ |
| Ensembl | rest.ensembl.org (GRCh38), grch37.rest.ensembl.org (GRCh37) | versioned gene/transcript lookup, sequence, VEP consequences, variant_recoder | 10/s | https://rest.ensembl.org/documentation/ , https://www.ensembl.org/info/about/legal/disclaimer.html |
| ClinVar | E-utilities `esearch`, `esummary`, `efetch rettype=vcv is_variationid=true`, `einfo` | allele matching, VCV/SCV assertions, Entrez build | NCBI 3/s unkeyed, 10/s with explicit key | https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/ |
| NCBI nuccore | E-utilities `efetch db=nuccore rettype=fasta` | reference bases for the same RefSeq chromosome when Ensembl fails | shares NCBI limit | https://www.ncbi.nlm.nih.gov/books/NBK25499/ |
| NCBI Variation | api.ncbi.nlm.nih.gov/variation/v0 `refsnp`, `hgvs/.../contextuals`, `spdi/.../all_equivalent_contextual` | rsID and RefSeq HGVS to chromosome SPDI | shares NCBI limit | https://api.ncbi.nlm.nih.gov/variation/v0/ |
| gnomAD | gnomad.broadinstitute.org/api GraphQL `variant`, `gene.gnomad_constraint` | AC/AN/AF, homozygote/hemizygote counts, population rows, filters; constraint | 10/min | https://github.com/broadinstitute/gnomad-browser , https://gnomad.broadinstitute.org/policies |
| UniProt | rest.uniprot.org/uniprotkb `{acc}.json`, `search` | entry identity, review status, function, locations, features, xrefs, release header | 10/s | https://www.uniprot.org/help/api , https://www.uniprot.org/help/license |
| Open Targets | api.platform.opentargets.org/api/v4/graphql `target.associatedDiseases`, `meta` | target-disease associations with datatype scores, data/API version | 5/s | https://platform-docs.opentargets.org/data-access/graphql-api , https://platform-docs.opentargets.org/licence |
| AlphaGenome Atlas (optional) | gRPC `gdmscience.googleapis.com:443`, `AtlasService.GetDenseVariantScores`, `ListVariantScoresMetadata` | precomputed variant scores | 2/s | https://www.alphagenomedocs.com/api/atlas.html , https://alphagenome.google/terms |

COSMIC is not implemented or advertised.

Atlas is disabled unless `ReferenceConfig.atlas_api_key` is set explicitly, or an `AtlasTransport` is injected. The default transport uses the official `alphagenome` protos over gRPC asyncio. That package is not a project dependency; without it Atlas reports `unsupported`. Only the two named precomputed RPCs are called; there is no live-inference path. Only GRCh38 SNVs are sent. The response reports no dataset or model version, so none is claimed. Results carry the provider terms: non-commercial use, not for clinical decisions, not for training other models.

## Coordinates and normalization

- Structured `position` is the 1-based VCF POS of the first REF base. `start` is 0-based. Exactly one is required.
- VCF strings (`7-140753336-A-T`, `chr7:140753336A>T`, `7 140753336 A T`) use 1-based POS. Comma-separated ALTs are split into separate alleles; the result is `ambiguous` and nothing is looked up.
- `CanonicalVariant` is 0-based half-open with minimal alleles; insertions have `start == end`. Contigs are `1`–`22`, `X`, `Y`, `MT`.
- The assembly must be GRCh38 or GRCh37 (hg38/hg19 are accepted and reported). A versioned NC_ accession implies it. No liftover is ever applied. `NC_012920.1` needs an explicit assembly. `chrM` is rejected for GRCh37 because UCSC hg19 chrM is not rCRS.
- REF is verified against any supplied reference that covers the allele. More sequence is only needed to shift indels. Reference sources, in order: the caller-supplied window, an injected local provider, Ensembl, then NCBI nuccore for the same RefSeq chromosome. Every provider failure is reported in `errors`.
- With reference sequence, indels are trimmed and left-aligned (`vcf`, `spdi`). HGVS `hgvs_g` uses the 3′ rule. `ncbi_canonical_spdi` is the fully justified form ClinVar uses. Indels at a contig start are right-anchored in VCF, as bcftools does. Without reference sequence the status is `trimmed_only`, REF is `not_checked`, and indel SPDI/HGVS are not derived.
- Coding/noncoding HGVS needs a versioned accession. RefSeq goes through NCBI Variation, Ensembl IDs through variant_recoder, each falling back to the other. An allele whose requested transcript version is absent from Ensembl's output is returned as a candidate, not selected. Protein HGVS always returns candidates and never a canonical allele. A multi-allelic rsID returns one candidate per allele.

## Evidence semantics

- Every `Evidence` has source, native ID/version, URL, `retrieved_at`, `terms_url`, transformations and limitations. `source_release` is only set when the source reports one: HGNC index date, Ensembl REST release, ClinVar Entrez build, UniProt release header, Open Targets data/API version, or the gnomAD dataset ID.
- ClinVar: allele matching uses canonical SPDI on GRCh38, and VCV XML VCF fields on GRCh37 after an ESummary location prefilter. One `clinical_variant_record` holds each aggregate classification separately: germline, somatic clinical impact and oncogenicity. Each keeps ClinVar's review status, description, explanation, conflict flag and conditions. Every SCV is a `clinical_assertion` with submitter, review status, classification type/value, somatic impact type and significance, drug, origin, conditions and citations. Local counts are split by ClinVar's contribution flag and do not resolve anything.
- gnomAD: the dataset must match the build (r4/r3 GRCh38, r2_1 GRCh37). The variant ID is the left-aligned VCF form, and the returned allele and build are checked. AC, AN and AF are kept for the exome, genome and joint blocks, along with population rows. `af_derived` is only added, labelled, when AF is absent and AN > 0. A missing or zero AN is reported. "Variant not found" is `not_found`, not a frequency of zero.

## Failure isolation, limits and consent

- Error kinds: `invalid_input`, `not_found`, `unauthorized`, `forbidden`, `rate_limited`, `timeout`, `upstream`, `invalid_response`, `unsupported`, `not_configured`. URLs in errors have no query string, and key-like values are redacted.
- The whole call has a 30 s deadline. Each fan-out source is also cut off at the deadline, so its failure never discards other sources' completed evidence. Limiter waits that would pass the deadline fail as `rate_limited`. Retries cover 429 (honouring Retry-After up to 10 s), 500/502/503/504, timeouts and transport errors.
- Bodies are streamed and bounded: 8 MiB by default and 25 MiB for ClinVar VCV XML, with a Content-Length precheck. Error bodies are read to at most 64 KiB, and redirects are never followed. Results over 1 MiB drop trailing evidence and say so in `truncation`.
- `query_origin="private_file"` with `allow_external_queries` false makes zero external requests. Local normalization with a supplied reference still works. Remote resolution (rsID, c./n./p. HGVS, NG_/LRG_ HGVS) and every `lookup_variant` source return `forbidden`.

## Entry points

```python
from genomics_mcp.references import ReferenceService, ReferenceConfig, call_tool, TOOLS, input_schemas, output_schemas, source_status

service = ReferenceService(httpx.AsyncClient(), ReferenceConfig(ncbi_api_key=None, atlas_api_key=None),
                           atlas_transport=None, reference_provider=None)
result: dict = await call_tool(service, "lookup_variant", {"variant": "7-140753336-A-T", "assembly": "GRCh38"})
```

`TOOLS[name]` gives the request/result Pydantic models and the method name. `call_tool` returns `status="error"` with `invalid_input` for bad arguments instead of raising. `ReferenceProvider` is the protocol for a local FASTA-backed reference. Nothing is read from the environment.

## Test evidence

- Offline: `PYTHONPATH=src .work/venv/bin/python -m pytest tests/reference -q -p no:cacheprovider`. Fixtures are real responses captured on 2026-09-24, trimmed, plus the full gzipped VCV000013961.143, plus synthetic XML for somatic conflicts and IncludedRecord.
- The review's bcftools 1.24 oracle (5,720 synthetic variants) was rerun on 2026-09-25 after the fixes: 0 mismatches. It had also exposed a contig-start rotation bug, now fixed and covered by tests.
- Live (opt in with `GENOMICS_MCP_LIVE=1`, `tests/reference/test_live.py`), 2026-09-24 ~23:40 UTC, scrubbed environment: 4 passed.
  - HGNC FANCD1 → HGNC:1101 (previous symbol)
  - UniProt P51587 reviewed (release 2026_03)
  - ClinVar 7-140753336-A-T → VCV000013961 with at least 40 SCVs
  - gnomAD/Open Targets/Ensembl outcomes explicit
- Observed live source states on 2026-09-24:
  - rest.ensembl.org (GRCh38) mostly returned HTML 500s for sequence, VEP and lookup; one lookup/symbol returned 200 and one lookup/id timed out. Reference checks fell back to NCBI nuccore NC_000007.14 and the errors were reported.
  - grch37.rest.ensembl.org returned 200 (REST release 116).
  - gnomAD r4 and r2_1, Open Targets (data 26.09, API 26.9.0), UniProt, HGNC and NCBI Variation all returned 200.
  - One GRCh38 ClinVar lookup exceeded the 30 s deadline; gnomAD's result was kept. Two immediate repeats completed in about 6 s.
- The other cited docs/terms URLs returned 200 on 2026-09-25. The Ensembl disclaimer URL returned 403 (the Ensembl web servers were also failing) and is unverified.
- Not verified live: Atlas (no key; tested only with an injected fake transport), and NCBI keyed rate.
