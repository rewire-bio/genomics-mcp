"""EGA (European Genome-phenome Archive): public metadata, authorised htsget regions and files."""

from genomics_mcp.archives.ega.auth import EgaAuth, EgaPasswordGrant
from genomics_mcp.archives.ega.client import EgaClient

__all__ = ["EgaAuth", "EgaClient", "EgaPasswordGrant"]
