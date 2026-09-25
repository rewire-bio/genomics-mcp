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
from genomics_mcp.references import (
    ReferenceService,
    ReferenceConfig,
    call_tool,
    TOOLS,
    input_schemas,
    output_schemas,
    source_status,
)

service = ReferenceService(
    httpx.AsyncClient(),
    ReferenceConfig(ncbi_api_key=None, atlas_api_key=None),
    atlas_transport=None,
    reference_provider=None,
)
result: dict = await call_tool(
    service, "lookup_variant", {"variant": "7-140753336-A-T", "assembly": "GRCh38"}
)
```

`TOOLS[name]` gives the request/result Pydantic models and the method name. `call_tool` returns `status="error"` with `invalid_input` for bad arguments instead of raising. `ReferenceProvider` is the protocol for a local FASTA-backed reference. Nothing is read from the environment.

## MCP integration (`genomics_mcp.evidence`)

- `register(registry)` (loaded through core `PROVIDER_MODULES`) registers:
  - one `default` planner for each of `resolve_identifier`, `normalize_variant`, `lookup_variant`, `lookup_gene` and `lookup_protein`;
  - `SourceInfo` for hgnc, ensembl, clinvar, ncbi_variation, ncbi_nuccore, gnomad, uniprot, open_targets and alphagenome_atlas, with their operations;
  - the `reference_runtime` and `reference_evidence` components, and a shutdown hook.

  `list_sources` reports planner-served sources as available, via `SourceInfo.operations`. Atlas shows `not_configured` without a key.
- Settings drive everything; nothing is discovered from the environment:
  - `sources.<name>.enabled`: a disabled source is never contacted and reports `disabled`.
  - The NCBI key and contact email come from `sources.clinvar.api_key_env` / `contact_email`.
  - The Atlas key comes from `sources.alphagenome_atlas.api_key_env`.
  - `timeout_s` is both the per-attempt timeout and the whole-call budget for that source, capped by the call deadline.
  - `requests_per_minute` can only slow a source down.

  One `ReferenceService` per effective configuration keeps rate limiters across calls.
- Output mapping:
  - Each evidence item becomes a validated core `EvidenceRecord` under `data.records`. Source-native `data` is kept intact (ClinVar SCVs, gnomAD AC/AN/populations), plus `category`, `source_record_version`, `transformation_trace` and `source_truncation`.
  - For budget trimming, records are ordered with summaries first and individual SCVs last. `max_records` and `max_response_bytes` truncation is reported in the envelope.
  - `ErrorInfo` preserves the native error kind, operation, HTTP status and a query-less URL. A missing consent becomes `consent_required`.
  - `SourceStatus` lists every consulted source: ok/partial/unavailable/timeout/not_found/unauthorized/disabled/not_configured/not_implemented/skipped.
  - Top-level provenance is one compact entry per source and release; per-record provenance is in each record.
  - `data.result_status` keeps `ambiguous` and `unresolved`.
- `sources`: names outside an operation's list are reported as `not_implemented` (never dropped); if none remain, the call is `invalid_input`. For `lookup_variant`, `sources`/`include` select annotation sources (`include`: consequence, clinical, population, prediction). Normalization support (Ensembl/NCBI reference, rsID/HGVS resolution) is still used.
- Local FASTA (`reference`): the reference must be a local file. Any other scheme is refused with `preparation_required` before any resolver, network or source call; fetch it and its indexes first, or omit `reference` to use the Ensembl/NCBI reference sources. The FileRef must carry an explicit assembly equal to the variant's. The FASTA and every file the native reader opens (`.fai`, and `.gzi` for BGZF, whether given explicitly or found next to the FASTA) are checked against the allowed roots after resolving symlinks, before any read. pysam always gets explicit index paths, so a missing index is reported and never created. Contig names `7`, `chr7` and, for GRCh38 only, `chrM` are accepted and the choice is reported. Reading it sends nothing externally.
- Composition facade: `ctx.component("reference_evidence").normalize_variant(ctx, variant, egress=ctx.egress_for(files), reference=...)` and `.lookup_variant(ctx, variant, egress=..., sources=..., include=...)` return `OperationOutput`. Private-derived input without consent makes zero external requests; local FASTA validation still runs. The first external step returns `consent_required` (state `skipped`).
- HTTP (`references/http.py`):
  - Bodies are streamed and cut off at 8 MiB (25 MiB for ClinVar VCV XML), with a Content-Length precheck. Error bodies are read to at most 64 KiB.
  - Redirects are never followed, and every URL must be https on the source's documented hosts (core `check_network_destination`). There is no URL passthrough.
  - Errors and messages are redacted with the core redactor; each attempt has a hard timeout.
  - The provider sets the `httpx` logger to WARNING, because httpx logs full request URLs at INFO.
- Atlas extra: `uv sync --extra atlas` installs `alphagenome==0.9.0` (pinned, in `uv.lock`); its protos are imported and a request is built offline in `tests/reference/test_atlas_sdk.py`. Without the extra, or without a key, the result is an explicit `unsupported`/`not_configured`. Atlas has not been verified live (no key).

Core fixes made in this integration (narrow, with regression tests in `tests/test_fanout_budget.py`):

- `service.call`: the outer timeout adds a bounded grace (`min(1 s, max(0.05 s, 5% of timeout))`) past the call deadline. Fan-out children and planners time out at the deadline and still return completed sources' results. Before the fix, a fast source plus a stalled one at 0.05 s returned `timeout` with no data. Handlers that ignore the deadline are still cut off, and cancelling the call cancels the workers.
- `result.fit_to_response_budget`: every envelope, including `budget_exceeded` and error envelopes, fits `max_response_bytes`. Complete data is kept first: trailing provenance, warnings, source_status and errors are dropped only as needed. Records are trimmed (reported in `truncation` with `available`) only if that is not enough. Error details, hint and text are shortened by serialized bytes, keeping the original error code. Anything dropped is counted in the new optional `metadata_omitted` field; a randomized check of 400 multibyte cases found no envelope over the cap.
- `service.describe_source`: planner-served sources are listed with their operations.

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
- Integration, 2026-09-24 ~23:57 UTC (scrubbed environment):
  - Full suite (core and reference): passed, with the 5 live tests skipped by default. The live tests passed (5/5, including the MCP-server path) when enabled.
  - Live MCP calls through `build_server` → `GenomicsService` → provider:
    - `lookup_variant` GRCh38 V600E: partial, 48 records (45 SCVs). ClinVar, gnomAD and NCBI nuccore were ok; Ensembl GRCh38 was unavailable (HTML 500 on sequence and VEP) and reported. GRCh37 was not substituted. One ClinVar EFetch 429 was retried successfully.
    - `lookup_variant` GRCh37: ok (grch37 Ensembl VEP, gnomAD r2_1, ClinVar matched by VCV VCF fields).
    - `normalize_variant rs113488022`: ambiguous (3 alleles).
    - `lookup_gene FANCD1`: partial. HGNC, UniProt, Open Targets and gnomAD were ok; Ensembl lookup returned 500.
    - `lookup_protein P51587` and `resolve_identifier NM_000059.4`: ok.
    - Atlas without a key: `not_configured`, with no request sent.
