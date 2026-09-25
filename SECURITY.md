# Security

Report vulnerabilities privately through GitHub private vulnerability reporting: **Security → Report a vulnerability** on https://github.com/rewire-bio/genomics-mcp. Do not put details, credentials, signed URLs or private data in public issues.

Supported version: the latest release.

In scope: the server, the container image, the MCPB launcher and the release workflows. For example: reading outside `paths.allowed_roots`, use of ambient cloud credentials, unauthenticated HTTP access, secrets or signed URLs in logs or results, or data sent to external services without consent.

The server has no hosted instance. Upstream data sources (EGA, ENA, ENCODE, NCBI, and others) have their own security contacts.
