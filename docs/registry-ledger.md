# Release and registry ledger — 0.1.0

Machine-readable copy: [registry/ledger.json](../registry/ledger.json). Updated 2026-09-25.

**Nothing has been published or submitted yet.** "Prepared" means the metadata or workflow exists and was validated as described. Submitted, published and approved are recorded separately, with dates, when they happen.

| Route | Prepared | Submitted | Published | Approved | Next step / gate |
| --- | --- | --- | --- | --- | --- |
| GitHub release v0.1.0 | 2026-09-25 | — | — | n/a | Run `release.yml` after `package.yml` passes. |
| GHCR `ghcr.io/rewire-bio/genomics-mcp:0.1.0` | 2026-09-25 | — | — | n/a | Pushed by `release.yml` as private; an org owner sets it Public in package settings; then check anonymous pull. |
| PyPI `rewire-genomics-mcp` | 2026-09-25 | — | — | n/a | **Needs account setup:** personal PyPI pending publisher (see [release.md](release.md)). Not needed for the GitHub/OCI release. |
| Official MCP Registry | 2026-09-25 | — | — | — | `publish-mcp-registry.yml` after the release and public GHCR. |
| GitHub MCP Registry (github.com/mcp) | 2026-09-25 | — | — | — | Populated from the official registry; check the listing after publication. |
| BioContextAI | 2026-09-25 | — | — | — | Render with the release commit; open the PR. |
| Docker MCP Catalog | 2026-09-25 | — | — | — | Run `registry-checks.yml` (`task build --tools`); open the PR. |
| Glama | 2026-09-25 | — | — | — | **Needs account setup:** Glama GitHub sign-in (timini). Search for an existing entry first, then add or claim it. |
| punkpeye/awesome-mcp-servers | 2026-09-25 | — | — | — | Only after a real Glama listing exists. |
| mcpservers.org | 2026-09-25 | — | — | — | Free form after the GitHub release (not the $39 option). |
| Smithery (local MCPB) | 2026-09-25 | — | — | — | **Needs account setup:** Smithery sign-in and an owned namespace (free tier). Use the bundle from the release run. |
| PulseMCP | — | — | — | — | Deferred: new submissions paused. |
| MCP.so | — | — | — | — | Deferred: $39 listing fee, outside the unpaid launch. |

## Validation done (2026-09-25)

- `server.json` (OCI only) and the rendered OCI + MCPB + PyPI variant (the PyPI entry carries positional `uvx` runtime arguments that force the pyBigWig source build with the release's `build-constraints.txt`): the 2025-12-11 JSON schema (check-jsonschema 0.34.1) and `mcp-publisher` 1.8.1 `validate` (checksum-verified binary) both pass. `validate` posts the file to the registry's validation endpoint and does not check that packages exist.
- `glama.json`: passes the live schema at `https://glama.ai/mcp/schemas/server.json`.
- BioContextAI, against `biocontext-ai/registry` `ed5eb26`:
  - `meta.yaml` passes `schema.json`, and `mcp.json` passes `mcp_schema.json` (check-jsonschema 0.33.0);
  - the repository's `validate_mcp_json_schema.py` passes: the single server key `rewire-bio/genomics-mcp` equals the identifier;
  - the folder name matches, and prettier `--check` passes.
- Docker MCP Catalog, against `docker/mcp-registry` `49b643c`: `go run ./cmd/validate --name genomics-mcp` (what `task validate` runs) passes every check, on a temporary copy of the repository. `task build --tools` needs Docker and is wired in `registry-checks.yml`; it has not run yet.
- MCPB: `@anthropic-ai/mcpb` 2.1.2 `validate`, `pack`, `unpack` and `validate` all pass. The launcher is tested over real MCP with a recording `docker` stand-in (`tests/release/test_mcpb_launcher.py`). A real container run is in CI (not yet passed). Host application install flows are untested. The manifest declares Linux only; the recording-`docker` launcher tests are not macOS bundle acceptance.

## Submission files

`python3 scripts/render_registry.py --commit <40-hex release commit> --out build/registry` writes:

- `biocontext/servers/rewire-bio-genomics-mcp/meta.yaml` and `mcp.json`. `mcp.json` runs `uvx` from `git+https://github.com/rewire-bio/genomics-mcp@<commit>` with the pyBigWig source build and pinned build constraints, because no PyPI package exists yet.
- `docker/servers/genomics-mcp/server.yaml`: title "Genomics", category `database`, source pinned to the commit, and `/data` mounted read-only from the `data` parameter.

## External actions only the maintainer or coordinator can take

1. Run the release workflows and make the GHCR package public in the GitHub UI.
2. Set up a personal PyPI account and pending publisher, then run `publish-pypi.yml`.
3. Sign in to Glama and Smithery. Use free tiers only.
4. Open the BioContextAI, Docker and punkpeye PRs and the mcpservers.org form, then record the dates and URLs here.
