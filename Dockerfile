# genomics-mcp container: stdio MCP server by default. linux/amd64 is the built and tested platform.
#
#   docker run --rm -i \
#     --mount type=bind,source=/path/to/data,target=/data,readonly \
#     --mount type=bind,source=/path/to/workspace,target=/work \
#     ghcr.io/rewire-bio/genomics-mcp:0.1.0
#
# Base images are pinned by index digest (python 3.12.14-slim-trixie, uv 0.8.2).
ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.2@sha256:a7999d42cba0e5af47ef3c06ac310229c7f29c5314e35902f8353e8e170eeed1

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
# pyBigWig is built from source (pyproject [tool.uv] no-binary-package) so it links libcurl and
# can read remote bigWig/bigBed. The published Linux wheel has pyBigWig.remote == 0.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libcurl4-openssl-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CONFIG=1
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
 && /opt/venv/bin/python -c "import pyBigWig, sys; sys.exit(0 if pyBigWig.remote == 1 else 'pyBigWig built without remote support')"

FROM ${PYTHON_IMAGE}
ARG VERSION=0.1.0
LABEL org.opencontainers.image.source="https://github.com/rewire-bio/genomics-mcp" \
      org.opencontainers.image.title="genomics-mcp" \
      org.opencontainers.image.description="MCP server for bounded genomic data retrieval and versioned reference evidence" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      io.modelcontextprotocol.server.name="io.github.rewire-bio/genomics-mcp"
# Runtime libraries for the source-built pyBigWig (libcurl) and TLS roots.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcurl4t64 zlib1g ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --user-group --home-dir /work --no-create-home --shell /usr/sbin/nologin genomics \
 && mkdir -p /work /data /etc/genomics-mcp \
 && chown genomics:genomics /work
COPY --from=build /opt/venv /opt/venv
COPY packaging/container-config.toml /etc/genomics-mcp/config.toml
# No ambient cloud credentials: empty AWS config, no instance metadata.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/work \
    GENOMICS_MCP_CONFIG=/etc/genomics-mcp/config.toml \
    AWS_EC2_METADATA_DISABLED=true \
    AWS_CONFIG_FILE=/dev/null \
    AWS_SHARED_CREDENTIALS_FILE=/dev/null
USER genomics:genomics
WORKDIR /work
VOLUME ["/work"]
ENTRYPOINT ["genomics-mcp"]
CMD ["--transport", "stdio"]
