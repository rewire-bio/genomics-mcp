# Release and registry ledger — 0.1.0

Updated 2026-09-28. Machine-readable copy: [registry/ledger.json](../registry/ledger.json).

**[v0.1.0 is published on GitHub](https://github.com/rewire-bio/genomics-mcp/releases/tag/v0.1.0).** Native Python, the public GHCR container and Linux MCPB bundle are available. The official MCP Registry lists version 0.1.0 with OCI and MCPB packages. Submission does not imply directory approval.

| Route | Confirmed state | Evidence / remaining step |
| --- | --- | --- |
| GitHub release | Published 2026-09-25 | [Release](https://github.com/rewire-bio/genomics-mcp/releases/tag/v0.1.0); all seven assets downloaded anonymously and checksums verified. |
| GHCR container | Published 2026-09-25 | Anonymous image pull and MCP smoke test passed. Organisation package restriction restored after publication. |
| Official MCP Registry | Published 2026-09-25 | [Active v0.1.0 record](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.rewire-bio%2Fgenomics-mcp/versions/0.1.0), rechecked 2026-09-28. OCI and MCPB; no PyPI claim. |
| GitHub MCP Registry | Not listed when checked 2026-09-28 | [Search](https://github.com/mcp?search=genomics) returned no MCPs. Official registry publication does not establish inclusion here. |
| BioContextAI | Submitted 2026-09-25 | [PR #73](https://github.com/biocontext-ai/registry/pull/73), awaiting review. |
| Docker MCP Catalog | Submitted 2026-09-25 | [PR #5242](https://github.com/docker/mcp-registry/pull/5242), awaiting review. |
| Glama | Updated package published 2026-09-28 | [Listing](https://glama.ai/mcp/servers/rewire-bio/genomics-mcp) claimed and README synced. Glama build passed after adding native build prerequisites; package `0.1.1` is explicitly a Glama packaging revision of project `0.1.0`. Quality evaluation may still lag. |
| LobeHub | Existing public listing observed 2026-09-28 | [Listing](https://lobehub.com/mcp/rewire-bio-genomics-mcp) still shows old development documentation and Unvalidated status. One metadata refresh attempted; updated content not confirmed. |
| Awesome MCP Servers | Submitted 2026-09-25 | [PR #15126](https://github.com/punkpeye/awesome-mcp-servers/pull/15126), awaiting review. |
| mcpservers.org | Free form prepared | Needs a maintainer-selected personal contact email. Not submitted. |
| MCP Market | Free queue prepared | [Form](https://mcpmarket.com/submit); contact email confirmation pending. Not submitted. |
| Cline Marketplace | Deferred pending client test | [Submission requirements](https://github.com/cline/mcp-marketplace) include successful Cline installation and a 400x400 logo. Cline client test not performed. |
| Smithery | Tested release bundle ready | Web sign-in complete. Needs separate CLI authorization and an owned namespace to upload the released bundle. |
| PyPI | Workflow ready; not published | Needs a personal account and trusted publisher configuration; see [release instructions](release.md). |
| PulseMCP | Deferred | New submissions paused when checked 2026-09-24. |
| MCP.so | Excluded from unpaid launch | Requires a $39 listing fee. |

## Release evidence

Release source: `0487de1b7867c652bcab12cf6a4cd110115a45d4`.

- [Release workflow](https://github.com/rewire-bio/genomics-mcp/actions/runs/36153523194) passed distribution, real container, MCPB and GitHub release jobs. Container and unpacked bundle initialized over MCP, discovered all 23 tools and returned an exact synthetic sequence.
- [Package checks](https://github.com/rewire-bio/genomics-mcp/actions/runs/36152920786) and [core tests](https://github.com/rewire-bio/genomics-mcp/actions/runs/36152920824) passed on Linux and macOS before merge. The final implementation suite reported 593 passed and 27 optional skips.
- Five independently repeated [clean-install demonstrations](demos.md) passed: EGA public-test BAM region, ENA sequence download, bounded ENCODE bigWig, NCBI reference with ClinVar evidence, and synthetic local MinIO. The final published wheel's 97 application modules match the demonstrated wheel; final installation and MCP smoke checks also passed.
- Published wheel SHA-256: `e283b0e82d1cc18fc13d4a372c5c67f3d7a8f0a9518983f954ebf2abecf57298`. The release includes `SHA256SUMS` for all six payload assets.
- Tested image digest: `sha256:d80c94467a8b9a39a4e4b7906f553d04018fce3c78e4124ba470283576ab4d19`. Container and MCPB acceptance is Linux amd64 only. Host application install flows remain untested.

## Submission validation

- BioContextAI: upstream pre-commit checks passed at `ed5eb26`, including both schemas, identifier matching and formatting. Its exact Git installation command passed MCP initialization, tool discovery and an exact sequence query without a PyPI package.
- Docker: [actual upstream Task validation and build](https://github.com/rewire-bio/genomics-mcp/actions/runs/36154432957) passed at registry commit `49b643c`, using the exact release source. The build discovered 23 tools without a static tools file or credentials.
- Official registry: metadata passed the 2025-12-11 schema and checksum-verified `mcp-publisher` 1.8.1 validation. [Publication workflow](https://github.com/rewire-bio/genomics-mcp/actions/runs/36155986877) subsequently verified public assets and image anonymously, published via GitHub OIDC, and fetched the exact active version record.
- Glama metadata passed its live schema. Its fresh [build test](https://glama.ai/mcp/servers/rewire-bio/genomics-mcp/admin/dockerfile/tests/01a0e71b-f6e5-70cd-b8eb-7fc79f69ae67) passed after adding compiler/libcurl/zlib prerequisites, locked installation and a remote-bigWig assertion. The new package release was verified in the admin page. A refreshed quality evaluation is not claimed.

Each route's dates and gates are recorded separately in the JSON ledger. Account permissions, external review and paid or paused routes remain explicit.

## Promotion

A disclosed [r/mcp showcase](https://www.reddit.com/r/mcp/comments/1ws8t60/genomics_mcp_fetch_genomic_reads_variants_and/) was published and its title, full body and showcase flair verified on 2026-09-28. It links the released project and real-data demonstrations, states the tested platforms and discloses Claude Code implementation. No removal notice was observed. No post was made in r/bioinformatics, which prohibits tool promotion.
