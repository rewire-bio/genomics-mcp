# Genomics MCP project instructions

This is a personal project independent of rewire.it. Read PRD.md and docs/implementation-plan.md before implementation. The preserved docs/prd-original.md is historical: the approved unified scope supersedes its separation of reference and sequencing data.

## Absolute account boundary

- Never use, inspect, enumerate or authenticate to any work AWS account, profile, credentials, SSO, container/instance role, infrastructure or billing.
- Development uses synthetic local fixtures, local MinIO with explicit test credentials, anonymous public HTTPS/S3, and EGA's documented public test account only.
- Do not read ~/.aws, invoke aws/SSO, or use the default boto3/HTSlib credential chain. Scrub inherited AWS and cloud-provider variables in every worker and test subprocess; disable EC2 metadata and use empty project-local AWS configuration files.
- Authenticated object storage in the product requires explicit personal configuration. Never fall back to ambient credentials. Requester-pays is disabled by default and cannot silently enable.
- Do not read unrelated personal/work files or credentials. Never write secrets, signed URL queries, patient identifiers or raw private records into logs, issues or commits.

## Implementation workflow

- Claude Code implements each epic. Run in an isolated branch/worktree with explicit file ownership; preserve other workers' changes. Independent review and testing happens before integration.
- Use Python 3.12, official MCP Python SDK, Pydantic, httpx, pysam, pyBigWig, boto3 and uv. Pin dependencies and commit uv.lock.
- Source storage is read-only. Local cache/workspace writes must be bounded and configured.
- Structured intervals are always 0-based half-open, assembly and accession version explicit; never silently liftover, guess CRAM references or invent phenotype/sample relationships.
- Return actual records or local artifacts, explicit provenance and partial errors. Metadata-only placeholders do not satisfy retrieval acceptance.
- Default limits: 1 Mb region, 10,000 records, 1 MiB response, 30 second interactive deadline, 100 MiB transfers. Larger transfers require an explicit caller budget.
- External annotation of private file-derived queries requires explicit per-call consent. No arbitrary shell MCP tool, clinical verdict or variant calling.
- Do not claim a test ran, source works, release published or registry approved without evidence. Keep outstanding items explicit.
- Small, meaningful tests: verify biological coordinate/filter semantics, access boundaries and real protocol behavior. Use current primary documentation for source APIs.
- No operated public service. stdio and authenticated loopback-default Streamable HTTP. macOS/Linux supported; Windows through WSL2/container.
- Keep user-facing prose short and literal.

