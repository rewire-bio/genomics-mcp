# Installation and platforms

All routes run the same stdio server (`genomics-mcp`; `rewire-genomics-mcp` is an alias). There is no hosted service.

**Available for v0.1.0:**

- the public container `ghcr.io/rewire-bio/genomics-mcp:0.1.0` (digest `sha256:d80c94467a8b9a39a4e4b7906f553d04018fce3c78e4124ba470283576ab4d19`);
- the [GitHub release](https://github.com/rewire-bio/genomics-mcp/releases/tag/v0.1.0), with the wheel, sdist, MCPB bundle, `requirements.lock.txt`, `build-constraints.txt`, `image.json` and `SHA256SUMS`;
- source from tag `v0.1.0` (commit `0487de1b7867c652bcab12cf6a4cd110115a45d4`);
- the [official MCP Registry entry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.rewire-bio%2Fgenomics-mcp/versions/0.1.0), which lists the container and MCPB bundle.

There is no PyPI package. Directory listings: [publication ledger](registry-ledger.md).

## Platform support (0.1.0)

| Platform | Status |
| --- | --- |
| macOS arm64, Python 3.12 | Tested: full suite, clean wheel install, five live demos (2026-09-25). |
| Linux x86_64, Python 3.12 | Tested in CI: full suite with samtools/bcftools oracles and `pyBigWig.remote == 1`. |
| Container, linux/amd64 | Tested on Linux x86_64 in CI: built, then exercised over MCP stdio (23 tools, exact synthetic sequence) by the release run. The published image was pulled anonymously and got the same check. Not run under emulation on Apple silicon. |
| MCPB bundle | Linux only: run against the real image in CI. macOS Docker Desktop and host-app install flows are untested. |
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

Inside this repository uv applies the policy automatically (`[tool.uv]` in pyproject.toml). `pip`, `uvx` and `uv pip` do not read it from a dependency, so pass it explicitly as shown below. `UV_NO_CONFIG=1` also disables `[tool.uv]`. If you set it, also set `UV_CONFIG_FILE=packaging/uv.toml` (a reviewed copy of the same settings, used by the container and CI). Otherwise uv may install the Linux wheel without remote support.

## From source (uv)

```sh
git clone https://github.com/rewire-bio/genomics-mcp
cd genomics-mcp
git checkout v0.1.0
uv sync --locked --no-dev
uv run genomics-mcp --check-config
```

## uvx

From the v0.1.0 release commit. This route was tested. It needs the same compiler, libcurl and zlib as above, and the first launch builds pyBigWig, which takes a while.

```sh
uvx --python 3.12 --no-binary-package pybigwig \
  --build-constraints https://raw.githubusercontent.com/rewire-bio/genomics-mcp/0487de1b7867c652bcab12cf6a4cd110115a45d4/packaging/build-constraints.txt \
  --from git+https://github.com/rewire-bio/genomics-mcp@0487de1b7867c652bcab12cf6a4cd110115a45d4 genomics-mcp
```

There is no PyPI package, so `uvx rewire-genomics-mcp` does not work yet.

For fully pinned transitive dependencies, add `-c requirements.lock.txt` (a GitHub release asset exported from `uv.lock`). For an MCP client, use `uvx` as the command and the arguments above as `args`.

## Wheel from a GitHub release

Download the wheel, `requirements.lock.txt`, `build-constraints.txt` and `SHA256SUMS` from the release linked above. Check the downloaded files against `SHA256SUMS` before installing.

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
- The image is linux/amd64. On Apple silicon Docker would run it under emulation; that has not been tested.

MCP client entry (the same flags as above):

```json
{
  "mcpServers": {
    "genomics": {
      "command": "docker",
      "args": ["run", "--rm", "-i",
        "--mount", "type=bind,source=/path/to/data,target=/data,readonly",
        "--mount", "type=volume,source=genomics-mcp-work,target=/work",
        "ghcr.io/rewire-bio/genomics-mcp:0.1.0"]
    }
  }
}
```

## MCPB bundle

`genomics-mcp-0.1.0.mcpb` (GitHub release asset) is a Node launcher for the digest-pinned container. It needs Node.js 20+ and a running Docker daemon. The manifest declares Linux only. The bundle was run against the real image in Linux CI. macOS with Docker Desktop is untested and not claimed. At install time you choose the Docker executable, a read-only data directory and a separate workspace directory. The launcher refuses your home directory (or any parent of it), nested directories and relative paths, and passes no cloud credentials. Host application install flows have not been tested yet.

## Windows

Use WSL2 with a Linux distribution and follow the Linux steps, or run the container with Docker Desktop. Native Windows is not supported.
