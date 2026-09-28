# genomics-mcp container: stdio MCP server by default. linux/amd64 is the built and tested platform.
#
#   docker run --rm -i \
#     --mount type=bind,source=/path/to/data,target=/data,readonly \
#     --mount type=bind,source=/path/to/workspace,target=/work \
#     ghcr.io/rewire-bio/genomics-mcp:0.1.0
#
# The default build is the standard image. `--target sra` builds the optional variant with
# NCBI SRA Toolkit prefetch and fasterq-dump for convert_sra_run (docs/sra-toolkit.md).
#
# Base images are pinned by index digest (python 3.12.14-slim-trixie, uv 0.8.2).
ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.2@sha256:a7999d42cba0e5af47ef3c06ac310229c7f29c5314e35902f8353e8e170eeed1
# NCBI's Linux x86_64 build of SRA Toolkit 3.4.1. SHA-256 computed from the download, whose
# MD5 (ec6e9056a2bfebcf23c6cd6e02951ef2) matches https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/3.4.1/md5sum.txt
ARG SRA_TOOLKIT_VERSION=3.4.1
ARG SRA_TOOLKIT_SHA256=b950362c054765a4184af41947f022f040e94e964862017c0ecb0b0273db3596

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
# pyBigWig is built from source so it links libcurl and can read remote bigWig/bigBed; the
# published Linux wheel has pyBigWig.remote == 0. UV_NO_CONFIG=1 also disables pyproject
# [tool.uv], so the policy comes from the reviewed packaging/uv.toml via UV_CONFIG_FILE.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libcurl4-openssl-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CONFIG=1 \
    UV_CONFIG_FILE=/src/packaging/uv.toml
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packaging/uv.toml ./packaging/uv.toml
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
 && /opt/venv/bin/python -c "import pyBigWig, sys; sys.exit(0 if pyBigWig.remote == 1 else 'pyBigWig built without remote support')"

FROM ${PYTHON_IMAGE} AS standard
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

# Only prefetch, fasterq-dump, their dispatcher and NCBI's bundled default configuration.
FROM ${PYTHON_IMAGE} AS sra-toolkit
ARG SRA_TOOLKIT_VERSION
ARG SRA_TOOLKIT_SHA256
ADD https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/${SRA_TOOLKIT_VERSION}/sratoolkit.${SRA_TOOLKIT_VERSION}-ubuntu64.tar.gz /tmp/sratoolkit.tar.gz
RUN echo "${SRA_TOOLKIT_SHA256}  /tmp/sratoolkit.tar.gz" | sha256sum -c - \
 && mkdir -p /opt/sratoolkit/bin \
 && tar -xzf /tmp/sratoolkit.tar.gz -C /tmp \
 && cd /tmp/sratoolkit.${SRA_TOOLKIT_VERSION}-ubuntu64 \
 && cp -a README.md CHANGES /opt/sratoolkit/ \
 && cp -a bin/ncbi bin/sratools.${SRA_TOOLKIT_VERSION} bin/prefetch-orig.${SRA_TOOLKIT_VERSION} \
      bin/fasterq-dump-orig.${SRA_TOOLKIT_VERSION} /opt/sratoolkit/bin/ \
 && cd /opt/sratoolkit/bin \
 && for tool in prefetch fasterq-dump; do \
      ln -s sratools.${SRA_TOOLKIT_VERSION} ${tool}.${SRA_TOOLKIT_VERSION} \
      && ln -s ${tool}.${SRA_TOOLKIT_VERSION} ${tool}; \
    done \
 && rm -rf /tmp/sratoolkit*

FROM standard AS sra
ARG SRA_TOOLKIT_VERSION
LABEL org.opencontainers.image.title="genomics-mcp-sra" \
      org.opencontainers.image.description="genomics-mcp with the optional NCBI SRA Toolkit (prefetch, fasterq-dump)" \
      io.github.rewire-bio.sra-toolkit.version="${SRA_TOOLKIT_VERSION}" \
      io.github.rewire-bio.sra-toolkit.license="Public domain (US Government work); see /opt/sratoolkit/README.md"
COPY --from=sra-toolkit /opt/sratoolkit /opt/sratoolkit
ENV PATH=/opt/sratoolkit/bin:$PATH

# The default target stays the standard image, without SRA Toolkit.
FROM standard
