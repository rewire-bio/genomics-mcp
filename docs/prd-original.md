# Product Requirements Document: Genomics Reference MCP

**Project:** Genomics Reference MCP  
**Organization:** rewire-bio  
**Proposed repository:** `rewire-bio/genomics-reference-mcp`  
**Status:** Draft v1  
**Date:** 2026-09-24

## 1. Executive summary

Genomics Reference MCP is a Model Context Protocol server that gives AI agents a single, normalized interface to authoritative genomics, variant, protein, cancer, functional-genomics, and clinical reference databases.

Instead of requiring an agent to understand and integrate dozens of heterogeneous APIs independently, the MCP accepts canonical biological entities such as genomic variants, genes, transcripts, and proteins; resolves identifiers and genome assemblies; queries multiple underlying sources; and returns normalized, provenance-rich evidence.

The initial product is focused on variant and gene interpretation workflows and should support sources including:

- AlphaGenome Atlas
- UniProt
- COSMIC
- ClinVar
- gnomAD
- Ensembl
- HGNC
- Open Targets

The product is not intended to make clinical diagnoses or collapse conflicting evidence into a single unsupported verdict. It is an evidence retrieval and normalization layer for research and agent workflows.

## 2. Problem

Biological reference data is fragmented across many databases with inconsistent:

- identifiers;
- genome assemblies;
- variant representations;
- transcript models;
- response schemas;
- authentication models;
- licensing terms;
- update cadences;
- rate limits;
- nomenclature;
- evidence models.

A researcher or AI agent investigating one variant may need to query several independent systems:

1. Ensembl to resolve gene/transcript context.
2. ClinVar for submitted clinical interpretations.
3. gnomAD for population allele frequency.
4. COSMIC for somatic cancer observations.
5. AlphaGenome Atlas for predicted molecular impact.
6. UniProt for protein function and domain context.
7. Open Targets for gene-disease associations.

Today, each API must be understood separately. This creates unnecessary integration work and makes agent behavior brittle.

## 3. Product vision

> Ask one genomics interface about a biological entity and receive a structured, source-attributed view across the reference ecosystem.

The long-term goal is to become the reference-data layer for genomics agents.

Example:

```text
Agent
  |
  | lookup_variant("chr7:140753336 A>T", assembly="GRCh38")
  v
Genomics Reference MCP
  |
  +--> Ensembl
  +--> ClinVar
  +--> gnomAD
  +--> COSMIC
  +--> AlphaGenome Atlas
  +--> UniProt
  +--> Open Targets
  |
  v
Normalized evidence bundle
```

## 4. Goals

### 4.1 Primary goals

1. Provide one MCP interface for high-value genomics reference data.
2. Normalize common genomic entities across sources.
3. Preserve source provenance for every material claim.
4. Handle common identifier conversion automatically.
5. Handle GRCh37/GRCh38 assembly differences explicitly.
6. Make source-specific authentication manageable.
7. Return compact, agent-friendly structured responses.
8. Avoid hiding disagreements between databases.
9. Make it easy to add new source adapters.
10. Support both high-level cross-source tools and source-specific escape hatches.

### 4.2 Non-goals for V1

- Clinical diagnosis.
- ACMG/AMP classification automation.
- Treatment recommendation.
- Primary sequence-read analysis from BAM/CRAM.
- Variant calling.
- Whole-genome batch annotation at population scale.
- Replacing source databases.
- Mirroring restricted datasets without permission.
- Training models on third-party data where terms prohibit it.
- A graphical genome browser.

## 5. Target users

### 5.1 Computational biologist

Wants an AI agent to retrieve and combine reference evidence without writing bespoke API integration code.

### 5.2 Research scientist

Wants natural-language access to variant, gene, protein, and disease context with links back to original sources.

### 5.3 Bioinformatics platform developer

Wants a stable abstraction across external databases so downstream applications are not tightly coupled to source-specific APIs.

### 5.4 AI-agent developer

Wants a small set of reliable, typed tools that an LLM can call correctly.

## 6. Core user stories

### Variant investigation

> As a researcher, I want to provide a genomic variant and retrieve population, clinical, cancer, predicted functional, gene, transcript, and protein evidence in one response.

### Gene investigation

> As a researcher, I want to provide a gene symbol and retrieve identifiers, canonical transcripts, protein products, expression context, disease associations, and functional annotations.

### Cancer variant investigation

> As a cancer researcher, I want to know whether a variant has been observed in COSMIC, what tissues/tumour types it appears in, and what other evidence exists.

### Functional prediction

> As an agent, I want to retrieve AlphaGenome Atlas scores for eligible variants and clearly distinguish prediction from observed evidence.

### Source reconciliation

> As a researcher, I want conflicting claims from ClinVar, COSMIC, and other sources to remain separately attributed instead of being flattened into one answer.

### Identifier resolution

> As an agent, I want to submit HGVS, rsID, genomic coordinate, Ensembl ID, HGNC symbol, or UniProt accession and have the MCP resolve it to canonical entities.

## 7. Product principles

### 7.1 Provenance over synthesis

The MCP should retrieve and normalize evidence, not invent consensus.

### 7.2 Explicit uncertainty

Missing data, conflicting records, ambiguous transcripts, and assembly uncertainty must be represented explicitly.

### 7.3 Canonical inputs, source-native evidence

Inputs should be normalized. Returned evidence should retain both normalized fields and source-native identifiers.

### 7.4 Research use first

The server must clearly separate research evidence from clinical interpretation.

### 7.5 Small tool surface

Prefer a small number of biologically meaningful tools over one MCP tool per upstream API endpoint.

## 8. V1 source integrations

## 8.1 AlphaGenome Atlas

**Purpose:** Predicted functional effect of genomic variants.

V1 fields:

- AlphaGenome Variant Impact (AVI) score;
- available feature-level impact information;
- variant identity;
- assembly/context requirements;
- Atlas record URL or source identifier;
- source timestamp/version where available.

Requirements:

- Support Atlas programmatic access through the official AlphaGenome client/API.
- Distinguish precomputed Atlas results from live AlphaGenome model predictions.
- Do not automatically invoke live model inference in V1 unless explicitly enabled.
- Preserve provider usage restrictions and research-use notices.

## 8.2 ClinVar

**Purpose:** Submitted clinical significance and supporting records.

V1 fields:

- Variation ID / VCV / RCV where available;
- clinical significance;
- review status;
- condition;
- submitter;
- assertion date;
- citations;
- last evaluated date;
- source URL.

Implementation candidates:

- NCBI E-utilities;
- ClinVar downloadable records for optional local/cache modes.

Important behavior:

- Never convert conflicting interpretations into a single boolean `pathogenic=true`.
- Preserve assertion-level provenance.

## 8.3 gnomAD

**Purpose:** Population allele frequency and constraint context.

V1 variant fields:

- allele count;
- allele number;
- allele frequency;
- homozygote count where available;
- population-stratified frequencies;
- filtering status.

V1 gene fields:

- relevant gene constraint metrics where available.

## 8.4 COSMIC

**Purpose:** Somatic cancer mutation observations.

V1 fields should include only data permitted by the configured COSMIC access method and licence:

- mutation identifier;
- genomic/protein representation;
- gene;
- tumour/site context;
- sample/count summaries where permitted;
- source identifiers.

Requirements:

- Adapter must support authenticated access.
- Restricted fields must never be exposed unless allowed by the user's configured entitlement.
- No redistribution assumptions should be built into the server.

## 8.5 UniProt

**Purpose:** Protein identity, function, domains, annotations, and cross-references.

V1 fields:

- accession;
- recommended protein name;
- gene name;
- sequence length;
- function;
- subcellular location;
- domains/features;
- reviewed/unreviewed status;
- selected cross references.

## 8.6 Ensembl

**Purpose:** Canonical genomic entity resolution and transcript/consequence context.

V1 fields:

- gene ID;
- transcript ID;
- protein ID;
- gene symbol;
- biotype;
- coordinates;
- assembly;
- transcript mapping;
- variant consequence where available;
- cross-references.

Ensembl should be the default resolver for many genomic-coordinate workflows, but the internal architecture must not make the entire product dependent on Ensembl availability.

## 8.7 HGNC

**Purpose:** Canonical human gene naming.

V1 fields:

- approved symbol;
- approved name;
- HGNC ID;
- aliases;
- previous symbols;
- cross-references.

## 8.8 Open Targets

**Purpose:** Gene-disease and target-disease evidence.

V1 fields:

- target;
- disease;
- association score/evidence summaries;
- evidence datatype;
- source identifiers.

## 9. Candidate V2 sources

- dbSNP
- CIViC
- OncoKB
- GTEx
- Human Protein Atlas
- STRING
- AlphaFold DB
- PDBe
- InterPro
- GWAS Catalog
- PharmGKB
- Monarch Initiative
- ClinGen
- GenCC
- DECIPHER, subject to access/terms
- dbNSFP-derived predictors, subject to licensing
- MaveDB

V2 prioritization should be based on user demand, source licensing, API reliability, and marginal information value.

## 10. MCP tool surface

V1 should expose approximately 8-10 tools.

### 10.1 `lookup_variant`

Primary cross-source variant tool.

Input:

```json
{
  "variant": "chr7:140753336A>T",
  "assembly": "GRCh38",
  "sources": ["clinvar", "gnomad", "cosmic", "alphagenome", "ensembl"],
  "include": ["gene_context", "population", "clinical", "cancer", "prediction"]
}
```

Also accept:

- HGVS genomic;
- HGVS coding;
- HGVS protein where resolvable;
- rsID;
- SPDI;
- VCF-style coordinate.

Output:

```json
{
  "query": {},
  "canonical_variant": {},
  "gene_context": [],
  "evidence": {
    "population": [],
    "clinical": [],
    "cancer": [],
    "functional_prediction": []
  },
  "warnings": [],
  "provenance": []
}
```

### 10.2 `lookup_gene`

Input:

```json
{
  "gene": "TP53",
  "include": ["identifiers", "transcripts", "protein", "disease", "constraint"]
}
```

### 10.3 `lookup_protein`

Accept UniProt accession, Ensembl protein ID, or resolvable gene/transcript identifier.

### 10.4 `resolve_identifier`

Converts between identifiers and reports ambiguity.

Example:

```json
{
  "input": "P04637",
  "target_types": ["gene", "protein", "ensembl_gene", "hgnc"]
}
```

### 10.5 `normalize_variant`

Normalizes variant representation without querying all downstream sources.

Input should support:

- assembly;
- contig;
- position;
- ref;
- alt;
- HGVS;
- rsID.

Output should include canonical genomic representation plus derived HGVS where possible.

### 10.6 `get_population_evidence`

Focused population-frequency query.

### 10.7 `get_clinical_evidence`

Focused ClinVar and future clinical-knowledge-source query.

### 10.8 `get_cancer_evidence`

Focused COSMIC plus future CIViC/OncoKB query.

### 10.9 `get_functional_predictions`

Focused AlphaGenome Atlas and future prediction-source query.

### 10.10 `query_source`

Expert escape hatch.

```json
{
  "source": "uniprot",
  "operation": "search",
  "parameters": {}
}
```

This should be disabled or constrained for adapters where arbitrary passthrough creates security, licensing, or stability problems.

## 11. Canonical data model

The server should use internal canonical entity types.

### 11.1 Variant

```text
Variant
- assembly
- chromosome
- position
- reference
- alternate
- normalized_representation
- hgvs_g
- hgvs_c[]
- hgvs_p[]
- rs_ids[]
- genes[]
- transcripts[]
```

### 11.2 Gene

```text
Gene
- symbol
- hgnc_id
- ensembl_gene_id
- entrez_id
- aliases[]
- coordinates[]
```

### 11.3 Transcript

```text
Transcript
- ensembl_transcript_id
- gene_id
- assembly
- coordinates
- biotype
- canonical_status
- mane_status
```

### 11.4 Protein

```text
Protein
- uniprot_accession
- ensembl_protein_id
- gene
- name
- reviewed
```

### 11.5 Evidence record

Every source adapter should emit a common evidence envelope:

```json
{
  "source": "clinvar",
  "source_record_id": "VCV000012345",
  "evidence_type": "clinical_assertion",
  "entity": {},
  "data": {},
  "source_url": "...",
  "retrieved_at": "...",
  "source_updated_at": "...",
  "source_version": "...",
  "confidence": null,
  "limitations": []
}
```

`confidence` is source-native only. The MCP must not invent cross-source confidence scores in V1.

## 12. Identifier and normalization layer

This is a core differentiator.

Responsibilities:

1. Detect identifier type.
2. Normalize chromosome naming.
3. Validate reference allele where possible.
4. Normalize indels.
5. Resolve GRCh37 vs GRCh38.
6. Resolve gene aliases to approved HGNC symbols.
7. Map genes to transcripts and proteins.
8. Map genomic variants to transcript/protein consequences.
9. Track every conversion step.

The server should never silently liftover or change transcript representation. Transformations must be returned in a `normalization` or `resolution` trace.

Example:

```json
{
  "input": "NM_000546.6:c.215C>G",
  "resolved": {
    "assembly": "GRCh38",
    "chromosome": "17",
    "position": 7676154,
    "ref": "G",
    "alt": "C"
  },
  "resolution_trace": [
    {
      "operation": "transcript_to_genome",
      "source": "ensembl",
      "transcript": "NM_000546.6"
    }
  ]
}
```

## 13. Source adapter architecture

Each source must implement a common adapter contract.

```text
SourceAdapter
- name
- capabilities()
- health()
- authenticate()
- lookup_variant()
- lookup_gene()
- lookup_protein()
- normalize_response()
- provenance()
```

Adapters should be independently testable.

Suggested package structure:

```text
src/
  mcp/
  models/
  normalization/
  resolver/
  adapters/
    alphagenome/
    clinvar/
    cosmic/
    ensembl/
    gnomad/
    hgnc/
    opentargets/
    uniprot/
  cache/
  auth/
  provenance/
  config/
```

## 14. Query planner

High-level tools should use a deterministic query planner rather than delegating source selection entirely to the LLM.

Example:

```text
lookup_variant
    |
    +-- normalize variant
    |
    +-- Ensembl consequence
    +-- gnomAD population
    +-- ClinVar clinical
    +-- COSMIC cancer
    +-- AlphaGenome functional
    |
    +-- protein resolution
           |
           +-- UniProt
```

The planner should:

- run independent queries concurrently;
- respect per-source timeouts;
- allow partial success;
- cache reusable results;
- return source failures explicitly;
- avoid cascading failure when one provider is unavailable.

## 15. Authentication and configuration

Example:

```yaml
sources:
  alphagenome:
    enabled: true
    api_key_env: ALPHAGENOME_API_KEY

  clinvar:
    enabled: true
    ncbi_api_key_env: NCBI_API_KEY

  gnomad:
    enabled: true

  ensembl:
    enabled: true

  uniprot:
    enabled: true

  cosmic:
    enabled: true
    credentials_env: COSMIC_TOKEN

  opentargets:
    enabled: true
```

The MCP must expose a non-sensitive capability/health view:

```text
AlphaGenome    configured
ClinVar        available
gnomAD         available
COSMIC         authentication required
UniProt        available
```

Secrets must never be returned through MCP tools.

## 16. Licensing and usage-policy layer

Different sources impose different terms.

The system must maintain source metadata describing:

- licence/terms URL;
- authentication requirement;
- commercial-use constraints;
- redistribution constraints;
- attribution requirement;
- cache restrictions;
- maximum permitted retention if applicable;
- permitted response fields.

The system must not assume that because data can be queried it can be redistributed or cached indefinitely.

AlphaGenome results must preserve research/clinical-use limitations from the provider.

COSMIC support must be entitlement-aware.

## 17. Provenance

Provenance is mandatory, not optional.

Every evidence record should include:

- source;
- source record identifier;
- source URL when available;
- retrieval timestamp;
- source version/release where available;
- source's own last-update timestamp where available;
- transformation steps performed by this MCP.

High-level responses should include a compact provenance list that an agent can cite.

## 18. Conflict handling

The MCP must not collapse conflicting evidence.

Example:

```json
{
  "clinical_evidence": [
    {
      "source": "clinvar",
      "significance": "Pathogenic",
      "submitter": "Lab A"
    },
    {
      "source": "clinvar",
      "significance": "Uncertain significance",
      "submitter": "Lab B"
    }
  ],
  "conflicts": [
    {
      "type": "clinical_significance",
      "sources": ["clinvar"],
      "message": "Submitted interpretations disagree."
    }
  ]
}
```

An optional later product may summarize evidence, but raw attributed evidence must remain available.

## 19. Caching

V1 should support pluggable caching:

- in-memory default;
- SQLite local cache;
- Redis optional.

Cache keys must include:

- source;
- normalized query;
- source API version where relevant;
- assembly;
- response version.

Each adapter should define conservative TTLs based on source update cadence and terms.

Users must be able to disable caching per source.

## 20. Rate limiting and resilience

Requirements:

- per-source concurrency limit;
- exponential backoff;
- retry only on safe/retryable failures;
- circuit breaker for repeatedly failing sources;
- total request deadline;
- partial-result response;
- explicit `source_status` block.

Example:

```json
{
  "source_status": {
    "clinvar": "ok",
    "gnomad": "ok",
    "cosmic": "authentication_required",
    "alphagenome": "timeout"
  }
}
```

## 21. Performance targets

For single-variant cross-source lookup:

- p50 under 2 seconds when cached;
- p50 under 5 seconds for normal uncached public-source lookup;
- partial response rather than total failure when one source times out;
- streamed/progressive MCP response may be considered later.

For V1, correctness, provenance, and stable schemas are more important than extreme throughput.

## 22. Safety and scientific-use requirements

The MCP is a research information tool.

It should:

- label predictions as predictions;
- distinguish observed evidence from computational inference;
- preserve clinical-source review status;
- expose known limitations;
- avoid generating treatment recommendations;
- avoid claiming clinical validation where a provider does not;
- make assembly/transcript ambiguity visible;
- avoid interpreting absence of a record as evidence of benignity.

## 23. Observability

Structured logs should capture:

- tool called;
- source adapters invoked;
- latency per source;
- cache hit/miss;
- error class;
- response size;
- normalization path.

Logs must not include credentials.

Potentially sensitive biological query content should be configurable/redactable.

## 24. MCP resources

Useful read-only resources:

```text
reference://sources
reference://sources/clinvar
reference://sources/cosmic
reference://capabilities
reference://schema/variant
reference://schema/gene
reference://status
```

These let an agent inspect capabilities without executing expensive queries.

## 25. Deployment

V1 should support:

### Local MCP

```text
uvx genomics-reference-mcp
```

or:

```text
docker run rewirebio/genomics-reference-mcp
```

### Remote MCP

HTTP transport for team/server deployments.

Configuration must work through environment variables and YAML/TOML.

No database should be required for the minimal local installation.

## 26. Proposed implementation stack

Recommended:

- Python 3.12+
- FastMCP or official MCP Python SDK
- Pydantic for schemas
- httpx for async HTTP
- tenacity or equivalent retry layer
- SQLite optional local cache
- Redis optional remote cache
- pytest
- mypy/pyright
- Ruff
- uv packaging

The adapter architecture should keep source-specific SDK dependencies optional where practical.

## 27. Repository structure

```text
genomics-reference-mcp/
  README.md
  PRD.md
  pyproject.toml
  src/
    genomics_reference_mcp/
      server.py
      tools/
      models/
      resolver/
      normalization/
      adapters/
      cache/
      config/
      provenance/
  tests/
    unit/
    integration/
    fixtures/
  docs/
    sources/
    schemas/
    deployment/
```

## 28. V1 acceptance criteria

V1 is complete when:

1. The server can start locally as an MCP server.
2. `lookup_variant` accepts at least genomic-coordinate and HGVS variants.
3. GRCh37/GRCh38 is explicit in every genomic result.
4. Ensembl, ClinVar, gnomAD, UniProt, AlphaGenome Atlas, HGNC, and Open Targets adapters are implemented.
5. COSMIC adapter architecture exists and authenticated integration is implemented where permitted by available credentials/licensing.
6. `lookup_gene` returns normalized gene/protein/disease context.
7. Every returned evidence record contains provenance.
8. Conflicting ClinVar interpretations remain separately represented.
9. AlphaGenome predictions are clearly labelled as computational predictions.
10. Failure of one upstream source does not fail the complete request.
11. Secrets never appear in MCP output or logs.
12. Unit tests cover normalization and adapter response mapping.
13. Integration tests run against recorded or permitted test fixtures.
14. The README contains a five-minute local setup path.

## 29. Success metrics

Early product success should be measured by:

### Reliability

- percentage of valid queries successfully normalized;
- per-source successful query rate;
- percentage of multi-source queries returning partial/full results;
- schema stability.

### Agent usability

- tool-call success rate across major MCP clients;
- percentage of agent attempts using the correct high-level tool;
- average calls required to answer a benchmark variant question;
- malformed-input recovery rate.

### Scientific utility

Create a benchmark set of known variants and genes and measure:

- identifier resolution accuracy;
- source-record retrieval recall;
- correct assembly handling;
- correct provenance;
- preservation of conflicting evidence.

## 30. Initial benchmark

Create a public test fixture set spanning:

- SNV;
- insertion;
- deletion;
- multiallelic site;
- rsID;
- coding HGVS;
- protein HGVS;
- non-coding regulatory variant;
- ClinVar conflict;
- common gnomAD variant;
- somatic cancer variant;
- gene alias;
- transcript ambiguity;
- GRCh37/GRCh38 coordinate pair.

Each test should specify expected canonical entities and source records without asserting a clinical diagnosis.

## 31. Roadmap

### Phase 0 — Skeleton

- MCP server
- canonical models
- adapter interface
- configuration
- provenance envelope

### Phase 1 — Open core sources

- Ensembl
- HGNC
- ClinVar
- UniProt
- gnomAD

### Phase 2 — Functional and disease context

- AlphaGenome Atlas
- Open Targets

### Phase 3 — Cancer

- COSMIC
- CIViC
- optional OncoKB integration

### Phase 4 — Structural and expression context

- AlphaFold/PDBe
- GTEx
- Human Protein Atlas
- STRING
- InterPro

### Phase 5 — Batch/agent workflows

- lists of variants;
- query plans;
- background batch execution outside interactive MCP request paths;
- downloadable evidence bundles.

## 32. Future relationship with Genome Data MCP

This project should remain distinct from a future Genome Data MCP.

```text
                     AI Agent
                        |
          +-------------+-------------+
          |                           |
     Genome Data MCP           Reference MCP
          |                           |
   user's BAM/CRAM/VCF        public/licensed
          |                   reference sources
          |
 "What is in my sample?"      "What is known about it?"
```

The two products can later compose into end-to-end workflows:

1. identify a candidate variant from user data;
2. normalize it;
3. retrieve reference evidence;
4. inspect predicted functional impact;
5. return a provenance-rich research summary.

## 33. Key risks

### Source instability

APIs change and rate limits vary.

**Mitigation:** strict adapter boundaries, contract tests, source health checks.

### Licensing

Some high-value databases have restricted use.

**Mitigation:** entitlement-aware adapters, no implicit redistribution, source-level policy metadata.

### Variant normalization errors

Incorrect transcript/assembly mapping can invalidate downstream queries.

**Mitigation:** normalization trace, explicit assembly, reference validation, benchmark suite.

### LLM misuse

Agents may over-interpret evidence.

**Mitigation:** structured evidence, clear evidence-type labels, limitations, no hidden classification layer.

### Schema bloat

A universal biological schema can become unmanageable.

**Mitigation:** small canonical core plus source-native extension fields.

## 34. Open questions

1. Should COSMIC be V1 or V1.1 given licensing/onboarding complexity?
2. Should live AlphaGenome inference be a separate MCP tool from Atlas lookup?
3. Which service should be authoritative for variant normalization?
4. Should GA4GH VRS become the canonical internal variant representation?
5. Should the MCP support batch variant lists in V1?
6. Should source-native raw payloads be optionally exposed?
7. How much transcript consequence calculation should be performed locally versus delegated to Ensembl?
8. What is the commercial licensing strategy for hosted deployments that query non-commercial sources?
9. Should a future hosted product proxy requests or let users supply credentials directly to a local MCP?
10. Which benchmark dataset should be used to measure multi-source retrieval completeness?

## 35. Product thesis

The defensible value is not merely wrapping APIs in MCP.

The core product is:

**normalization + source orchestration + provenance + policy-aware access + stable agent-facing schemas.**

If done well, Genomics Reference MCP becomes the layer an AI agent calls whenever it needs to answer:

> What is known about this variant, gene, transcript, or protein?

without the agent needing to understand the fragmented genomics database ecosystem itself.
