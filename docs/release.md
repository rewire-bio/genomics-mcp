# Release procedure

Maintainer steps for version `X.Y.Z` (0.1.0 first). Everything runs from GitHub Actions in `rewire-bio/genomics-mcp`; no local credentials, cloud accounts or hosted services. Status of each route: [registry-ledger.md](registry-ledger.md).

## Before releasing

1. `pyproject.toml`, `server.json` and `packaging/mcpb/manifest.json` carry the same version, and `docs/release-notes/X.Y.Z.md` exists (the release workflow checks this).
2. `ci` and `package` workflows are green on the release commit. `package` builds the wheel/sdist, clean-installs it (pyBigWig source build, `remote == 1`), builds the linux/amd64 image and runs MCP smoke tests against the installed entry point, the container and the MCPB launcher.
3. Optional: run `registry-checks` with the release commit (Docker catalog `task validate` and `task build --tools`).

## 1. GitHub release and GHCR — `release.yml` (manual)

Input: `version`. Jobs:

- `dist`: version checks, full suite with oracles, build, `scripts/check_dist.py --install`, MCP smoke.
- `image`: build, `scripts/check_image.py` (ownership labels in the image config, non-root, stdio), MCP smoke, push `ghcr.io/rewire-bio/genomics-mcp:X.Y.Z`, record the digest.
- `mcpb`: `scripts/build_mcpb.py --image ghcr.io/rewire-bio/genomics-mcp@sha256:…`, then run the unpacked bundle against the pushed image.
- `github-release`: creates tag and release `vX.Y.Z` with the wheel, sdist, `requirements.lock.txt`, `build-constraints.txt`, the `.mcpb`, `image.json` (digest, commit, platform) and `SHA256SUMS`.

**Manual step after the first push:** a new GHCR package is private. An org owner opens rewire-bio → Packages → genomics-mcp → Package settings → Change visibility → Public. There is no supported API for this. Check anonymously afterwards:

```sh
empty=$(mktemp -d)
docker --config "$empty" pull --platform linux/amd64 ghcr.io/rewire-bio/genomics-mcp:X.Y.Z
python3 scripts/check_image.py ghcr.io/rewire-bio/genomics-mcp:X.Y.Z --docker-config "$empty"
```

## 2. PyPI — `publish-pypi.yml` (manual, needs a PyPI account)

One-time setup in a personal PyPI account: Account → Publishing → add a pending GitHub publisher:

| Field | Value |
| --- | --- |
| PyPI project name | `rewire-genomics-mcp` |
| Owner | `rewire-bio` |
| Repository | `genomics-mcp` |
| Workflow | `publish-pypi.yml` |
| Environment | `pypi` |

Also create the `pypi` environment in the repository settings (optionally with required reviewers). A pending publisher does not reserve the name; the first upload creates the project.

The workflow downloads the wheel and sdist from release `vX.Y.Z`, verifies `SHA256SUMS`, re-runs the clean install and `twine check --strict`, and uploads with trusted publishing (no token). It refuses if the version is already on PyPI. Until this runs, no PyPI package exists and none is listed anywhere.

## 3. Official MCP Registry — `publish-mcp-registry.yml` (manual)

Inputs: `version`, `include_mcpb` (default true), `include_pypi` (default false; set true only after step 2 succeeded).

Before publishing it checks, without credentials:

- the GHCR image pulls anonymously, carries `io.modelcontextprotocol.server.name=io.github.rewire-bio/genomics-mcp` in its config labels, and its digest equals the release's `image.json`;
- the MCPB asset downloads and matches `SHA256SUMS`;
- if requested, the PyPI release exists and its description has the `mcp-name` marker.

Then it renders `server.publish.json` (`scripts/render_server_json.py`), validates it with `mcp-publisher` 1.8.1 (checksum-verified), logs in with GitHub OIDC (`id-token: write`; the repository owner grants the `io.github.rewire-bio/*` namespace), publishes, and reads the exact version back from `registry.modelcontextprotocol.io`.

`mcp-publisher validate` checks the schema only. It does not check that packages exist; that is why the workflow checks them first. The committed `server.json` lists only the OCI image; the MCPB and PyPI entries are added only at publication.

## 4. Directory submissions

Rendered with `python3 scripts/render_registry.py --commit <release commit> --out build/registry`; details and status in [registry-ledger.md](registry-ledger.md).
