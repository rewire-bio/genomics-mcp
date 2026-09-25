# Installation and platforms

All routes run the same stdio server (`genomics-mcp`; `rewire-genomics-mcp` is an alias). There is no hosted service.

## Platform support (0.1.0)

| Platform | Status |
| --- | --- |
| macOS arm64, Python 3.12 | Tested: full suite, clean wheel install, five live demos (2026-09-25). |
| Linux x86_64, Python 3.12 | Tested in CI: full suite with samtools/bcftools oracles and `pyBigWig.remote == 1`. |
| Container, linux/amd64 | Built and exercised over MCP stdio by CI (`package.yml`, `release.yml`). Not run on the maintainer's machine (no local Docker). |
| Linux arm64, macOS x86_64 | Not tested. May work from source; not claimed. |
| Windows | Only through WSL2 (follow the Linux steps) or the container. No native Windows support. |

## Native dependency: pyBigWig

Remote bigWig/bigBed reading needs pyBigWig built with libcurl. The published pyBigWig 0.3.26 Linux wheel is built without it (`pyBigWig.remote == 0`), and there are no macOS wheels. Every Python route therefore builds pyBigWig from source, with pinned build requirements from [packaging/build-constraints.txt](../packaging/build-constraints.txt). You need:

- a C compiler (Xcode Command Line Tools on macOS; `build-essential` on Debian/Ubuntu);
- libcurl and zlib development files (`libcurl4-openssl-dev zlib1g-dev` on Debian/Ubuntu; macOS provides both).

Check an install with:

```sh
python -c "import pyBigWig; print(pyBigWig.remote)"   # must print 1
```

Inside this repository uv applies the policy automatically (`[tool.uv]` in pyproject.toml). `pip`, `uvx` and `uv pip` do not read it from a dependency, so pass it explicitly as shown below. `UV_NO_CONFIG=1` also disables `[tool.uv]`; isolated builds (the container, CI) therefore set `UV_CONFIG_FILE=packaging/uv.toml`, a reviewed copy of the same settings.

## From source (uv)

```sh
git clone https://github.com/rewire-bio/genomics-mcp
cd genomics-mcp
uv sync --locked --no-dev
uv run genomics-mcp --check-config
```

## uvx

From a Git commit (works before PyPI publication):

```sh
uvx --python 3.12 --no-binary-package pybigwig \
  --build-constraints https://raw.githubusercontent.com/rewire-bio/genomics-mcp/<commit>/packaging/build-constraints.txt \
  --from git+https://github.com/rewire-bio/genomics-mcp@<commit> genomics-mcp
```

From PyPI, once `rewire-genomics-mcp` is published there (not yet). These are the arguments the registry entry gives clients:

```sh
uvx --python 3.12 --no-binary-package pybigwig \
  --build-constraints https://github.com/rewire-bio/genomics-mcp/releases/download/v0.1.0/build-constraints.txt \
  rewire-genomics-mcp==0.1.0
```

For fully pinned transitive dependencies, add `-c requirements.lock.txt` (a GitHub release asset exported from `uv.lock`).

## Wheel from a GitHub release

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements.lock.txt \
  --no-binary pybigwig --build-constraints build-constraints.txt
uv pip install --python .venv/bin/python --no-deps rewire_genomics_mcp-0.1.0-py3-none-any.whl
.venv/bin/genomics-mcp --version
```

`scripts/check_dist.py dist --install DIR` runs exactly these steps and checks `pyBigWig.remote`.

## Container

```sh
docker run --rm -i \
  --mount type=bind,source=/path/to/data,target=/data,readonly \
  --mount type=volume,source=genomics-mcp-work,target=/work \
  ghcr.io/rewire-bio/genomics-mcp:0.1.0
```

- Runs as the non-root user `genomics` (uid 10001). The default command is `--transport stdio`.
- `/data` is the only allowed local input root. Mount it read-only. Do not mount your home directory.
- `/work` is the workspace (downloads, indexes, cache), limited to 10 GiB by `/etc/genomics-mcp/config.toml`. A named volume works as-is. For a host directory on Linux, add `--user "$(id -u):$(id -g)"` so the container can write to it.
- Optional hardening that the CI smoke test uses: `--read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m --cap-drop ALL --security-opt no-new-privileges`.
- No cloud credentials are in the image. Pass private S3 keys only as explicitly named variables for a `[storage.profiles.<name>]` entry in your own config file.
- Public sources need no keys. `docker run --rm IMAGE --check-config` prints sources and capabilities.
- The image is linux/amd64. On Apple silicon Docker runs it under emulation; that has not been tested.

## MCPB bundle

`genomics-mcp-0.1.0.mcpb` (GitHub release asset) is a Node launcher for the digest-pinned container. It needs Node.js 20+ and a running Docker daemon. The manifest declares Linux only: the bundle runs against the real image only in Linux CI. macOS with Docker Desktop is untested and not claimed. At install time you choose the Docker executable, a read-only data directory and a separate workspace directory. The launcher refuses your home directory (or any parent of it), nested directories and relative paths, and passes no cloud credentials. Host application install flows have not been tested yet.

## Windows

Use WSL2 with a Linux distribution and follow the Linux steps, or run the container with Docker Desktop. Native Windows is not supported.
