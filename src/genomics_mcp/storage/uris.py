"""URI parsing shared by resolvers and listings."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from genomics_mcp.errors import InvalidInputError


def local_path_from_uri(uri: str) -> str:
    """Absolute filesystem path from `/abs/path`, `file:///abs/path` or `file://localhost/abs`.

    Any other file URI host is refused (no implicit network shares). Percent-escapes are decoded.
    Query strings and fragments are refused rather than silently dropped.
    """
    if uri.startswith("/"):
        return uri
    parts = urlsplit(uri)
    if parts.scheme.lower() != "file":
        raise InvalidInputError("not a local file URI", details={"scheme": parts.scheme})
    host = (parts.netloc or "").lower()
    if host not in ("", "localhost"):
        raise InvalidInputError(
            "file URIs must use an empty host or 'localhost'",
            hint="use file:///absolute/path",
            details={"host": host},
        )
    if parts.query or parts.fragment:
        raise InvalidInputError("file URIs must not carry a query or fragment")
    path = unquote(parts.path)
    if not path.startswith("/"):
        raise InvalidInputError("file URI path must be absolute")
    if "\x00" in path:
        raise InvalidInputError("path contains a NUL byte")
    return path


@dataclass(frozen=True)
class S3Location:
    bucket: str
    key: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def parse_s3_uri(uri: str, *, allow_prefix: bool = False) -> S3Location:
    parts = urlsplit(uri)
    if parts.scheme.lower() != "s3":
        raise InvalidInputError("not an s3:// URI")
    if parts.query or parts.fragment:
        raise InvalidInputError("s3:// URIs must not carry a query or fragment")
    bucket = parts.netloc
    key = parts.path.lstrip("/")
    if (
        not bucket
        or not (3 <= len(bucket) <= 63)
        or not all(c.isalnum() or c in "-." for c in bucket)
    ):
        raise InvalidInputError("invalid S3 bucket name", details={"bucket": bucket})
    if not key and not allow_prefix:
        raise InvalidInputError("s3:// URI must name an object key")
    return S3Location(bucket=bucket, key=key)
