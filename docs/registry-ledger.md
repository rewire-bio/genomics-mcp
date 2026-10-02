# Release and registry ledger — 0.1.0

Updated 2026-10-02 (PyPI published 2026-10-02; verification evidence refreshed). Machine-readable copy: [registry/ledger.json](../registry/ledger.json).

**[v0.1.0 is published on GitHub](https://github.com/rewire-bio/genomics-mcp/releases/tag/v0.1.0) and [PyPI](https://pypi.org/project/rewire-genomics-mcp/0.1.0/).** Native Python, the public GHCR container and Linux MCPB bundle are available. The official MCP Registry lists version 0.1.0 with OCI and MCPB packages. Submission does not imply directory approval.

| Route | Confirmed state | Evidence / remaining step |
| --- | --- | --- |
| GitHub release | Published 2026-09-25 | [Release](https://github.com/rewire-bio/genomics-mcp/releases/tag/v0.1.0); all 6 payload checksums and `SHA256SUMS` re-verified anonymously 2026-09-30. Clean wheel and sdist installs both passed MCP smoke. |
| GHCR container | Published 2026-09-25 | Anonymous image pull and MCP smoke test passed in CI (workflow 36155986877, 2026-09-25). Local host pull not re-run (no local Docker daemon). Organisation package restriction restored after publication. |
| Official MCP Registry | Published 2026-09-25 | [Active v0.1.0 record](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.rewire-bio%2Fgenomics-mcp/versions/0.1.0), rechecked live 2026-10-02. OCI and MCPB; no PyPI entry in v0.1.0. Attempt to add PyPI via [workflow 36987556269](https://github.com/rewire-bio/genomics-mcp/actions/runs/36987556269) passed package checks and live uvx launch with remote pyBigWig, but mcp-publisher publish returned HTTP 400 (duplicate version). Next fresh release version may include PyPI. |
| GitHub MCP Registry | Not listed when checked 2026-09-30 | [Search](https://github.com/mcp?search=genomics) returns "No MCPs found matching genomics". Official registry publication does not establish inclusion here. |
| BioContextAI | Submitted 2026-09-25 | [PR #73](https://github.com/biocontext-ai/registry/pull/73), rechecked 2026-09-30: open, mergeable, awaiting review. |
| Docker MCP Catalog | Submitted 2026-09-25 | [PR #5242](https://github.com/docker/mcp-registry/pull/5242), rechecked 2026-09-30: open, mergeable, awaiting review. |
| Glama | Updated package published 2026-09-28 | [Listing](https://glama.ai/mcp/servers/rewire-bio/genomics-mcp) claimed and README synced. Glama package `0.1.1` passed native build; 23 tools discovered; TDQS B (3.4/5), rechecked live 2026-09-30. |
| LobeHub | Existing public listing observed 2026-09-28 | [Listing](https://lobehub.com/mcp/rewire-bio-genomics-mcp) rechecked live 2026-09-30: shows old development docs and Unvalidated status. Refresh requested 2026-09-28; updated content not confirmed. |
| Awesome MCP Servers | Submitted 2026-09-25 | [PR #15126](https://github.com/punkpeye/awesome-mcp-servers/pull/15126), rechecked 2026-09-30: open, mergeable, Glama check passing, awaiting review. |
| mcpservers.org | Submitted 2026-09-28 | Free submission receipt verified; awaiting review (quoted 2 weeks). |
| MCP Market | Submitted 2026-09-28 | Free queue receipt verified; awaiting review (quoted 4–6 weeks). |
| Cline Marketplace | Deferred pending client test | [Submission requirements](https://github.com/cline/mcp-marketplace) include Cline installation test and 400x400 logo. Cline test not performed. |
| Smithery | Published 2026-09-28 | [Local bundle listing](https://smithery.ai/servers/tim-80ew/genomics-mcp); CLI deployment succeeded, metadata verified. Capability discovery not yet available. |
| PyPI | Published 2026-10-02 | [Package 0.1.0](https://pypi.org/project/rewire-genomics-mcp/0.1.0/); published via [workflow 36987431002](https://github.com/rewire-bio/genomics-mcp/actions/runs/36987431002) with release asset verification, clean install, twine strict and GitHub OIDC trusted publishing. [PyPI JSON](https://pypi.org/pypi/rewire-genomics-mcp/0.1.0/json) confirms wheel SHA-256 `e283b0e82d1cc18fc13d4a372c5c67f3d7a8f0a9518983f954ebf2abecf57298` and sdist SHA-256 `7eb575762b0734caebe76a2caf2e0d885502ba333597f0cd5bd9299a9750c332` equal GitHub release assets. GitHub environment `pypi` exists; trusted publisher was configured and successful OIDC upload verified. |
| PulseMCP | Deferred | New submissions paused (checked 2026-09-24). |
| MCP.so | Excluded from unpaid launch | Requires a $39 listing fee. |

## Release evidence

Release source: `0487de1b7867c652bcab12cf6a4cd110115a45d4`.

- [Release workflow](https://github.com/rewire-bio/genomics-mcp/actions/runs/36153523194) passed distribution, real container, MCPB and GitHub release jobs. Container and unpacked bundle initialized over MCP, discovered all 23 tools and returned an exact synthetic sequence.
- [Package checks](https://github.com/rewire-bio/genomics-mcp/actions/runs/36152920786) and [core tests](https://github.com/rewire-bio/genomics-mcp/actions/runs/36152920824) passed on Linux and macOS before merge. The final implementation suite reported 593 passed and 27 optional skips.
- Five independently repeated [clean-install demonstrations](demos.md) passed: EGA public-test BAM region, ENA sequence download, bounded ENCODE bigWig, NCBI reference with ClinVar evidence, and synthetic local MinIO. The final published wheel's 97 application modules match the demonstrated wheel; final installation and MCP smoke checks also passed.
- Published wheel SHA-256: `e283b0e82d1cc18fc13d4a372c5c67f3d7a8f0a9518983f954ebf2abecf57298`. The release includes `SHA256SUMS` for all six payload assets.
- Tested image digest: `sha256:d80c94467a8b9a39a4e4b7906f553d04018fce3c78e4124ba470283576ab4d19`. Container and MCPB acceptance is Linux amd64 only. Host application install flows remain untested.
- Verification evidence refreshed 2026-09-30 across independent runs:
  - **Payload integrity:** Coordinator independently downloaded all 6 release assets and verified hashes against `SHA256SUMS` anonymously in `build/independent-publication`.
  - **Wheel clean install and smoke:** Sonnet session `8db0c76c-b871-4e9a-86bc-f6195281e99f` executed `check_dist.py` and `mcp_smoke.py` in `build/task-isolation`. Pinned dependencies installed into clean Python 3.12 venv with forced pyBigWig source build (`pyBigWig.remote == 1`). Full MCP smoke passed: 23 tools, 17 sources, sequence `TTGCAAGGCT`, dummy ambient AWS credentials confirmed unused (`check_dist_output.json`, `mcp_smoke_output.log`).
  - **Source distribution clean install and smoke:** Coordinator independently installed `rewire_genomics_mcp-0.1.0.tar.gz` into clean Python 3.12 venv in `build/independent-publication`. MCP smoke passed: 23 tools, 17 sources, sequence `TTGCAAGGCT`, dummy ambient AWS credentials confirmed unused (`sdist-install.json`, `sdist-smoke.json`).
  - **PyPI status & audit (historical):** Package unlisted (`https://pypi.org/pypi/rewire-genomics-mcp/json` returned 404 on 2026-09-30). Trusted publishing required personal login; audited and recorded 2026-10-01 in session `baa4f216-8e13-4ef8-a11d-061fa8ff7ae4` without modifying 2026-09-30 test timestamps.
- Verification and publication evidence (2026-10-02):
  - **PyPI publication:** Published 2026-10-02 via [workflow 36987431002](https://github.com/rewire-bio/genomics-mcp/actions/runs/36987431002) with release asset verification, clean install, `twine check --strict` and GitHub OIDC trusted publishing. [PyPI JSON](https://pypi.org/pypi/rewire-genomics-mcp/0.1.0/json) confirms wheel SHA-256 `e283b0e82d1cc18fc13d4a372c5c67f3d7a8f0a9518983f954ebf2abecf57298` and sdist SHA-256 `7eb575762b0734caebe76a2caf2e0d885502ba333597f0cd5bd9299a9750c332` equal original GitHub v0.1.0 assets. GitHub environment `pypi` exists; trusted publisher was configured with exact repository, workflow, and environment, and successful OIDC upload verified.
  - **Official MCP Registry PyPI addition attempt:** Attempted to add PyPI to v0.1.0 via [workflow 36987556269](https://github.com/rewire-bio/genomics-mcp/actions/runs/36987556269). Verified PyPI package existence and passed live `uvx` launch with `pyBigWig.remote == 1`, but `mcp-publisher publish` failed with HTTP 400 (`invalid version: cannot publish duplicate version 0.1.0`). Version 0.1.0 remains live with OCI and MCPB packages; PyPI inclusion deferred to next fresh release version without workflow changes.

## Submission validation

- BioContextAI: upstream pre-commit checks passed at `ed5eb26`, including both schemas, identifier matching and formatting. Its exact Git installation command passed MCP initialization, tool discovery and an exact sequence query without a PyPI package. Rechecked 2026-09-30: PR #73 remains open and mergeable.
- Docker: [actual upstream Task validation and build](https://github.com/rewire-bio/genomics-mcp/actions/runs/36154432957) passed at registry commit `49b643c`, using the exact release source. The build discovered 23 tools without a static tools file or credentials. Rechecked 2026-09-30: PR #5242 remains open and mergeable.
- Official registry: metadata passed the 2025-12-11 schema and checksum-verified `mcp-publisher` 1.8.1 validation. [Publication workflow](https://github.com/rewire-bio/genomics-mcp/actions/runs/36155986877) verified public assets anonymously, published via GitHub OIDC, and fetched the active version record. Rechecked live 2026-10-02 (OCI and MCPB active). Attempt to add PyPI via [workflow 36987556269](https://github.com/rewire-bio/genomics-mcp/actions/runs/36987556269) verified PyPI package existence and passed live uvx launch with remote pyBigWig, but failed at publication with HTTP 400 (duplicate version 0.1.0 cannot be republished). Version 0.1.0 remains live with OCI and MCPB packages; PyPI inclusion deferred to next release version.
- Glama: metadata passed live schema. Fresh [build test](https://glama.ai/mcp/servers/rewire-bio/genomics-mcp/admin/dockerfile/tests/01a0e71b-f6e5-70cd-b8eb-7fc79f69ae67) passed with compiler/libcurl/zlib prerequisites, locked install and remote-bigWig assertion. Package `0.1.1` maps to project release `0.1.0`. Public listing reports 23 tools and TDQS B (3.4/5), evaluated 2026-09-28; rechecked live 2026-09-30.
- Awesome MCP Servers: PR #15126 rechecked 2026-09-30: open, mergeable, Glama check passing.

Issue #36 was closed because eligible unpaid submissions and the evidence-backed ledger met acceptance criteria. External directory approvals remain pending upstream. No new features, no duplicate submissions.

## Promotion

A disclosed [r/mcp showcase](https://www.reddit.com/r/mcp/comments/1ws8t60/genomics_mcp_fetch_genomic_reads_variants_and/) was published and its title, full body and showcase flair verified on 2026-09-28. It links the released project and real-data demonstrations, states the tested platforms and discloses Claude Code implementation. No removal notice was observed. No post was made in r/bioinformatics, which prohibits tool promotion.
