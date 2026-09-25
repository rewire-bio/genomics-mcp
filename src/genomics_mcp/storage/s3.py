"""S3 and S3-compatible storage with no ambient AWS configuration.

Clients come from a botocore `Session` whose configuration store is rebuilt with:
- no environment lookups (an empty environ is given to the config chain factory),
- `config_file` / `credentials_file` pointing at empty files in the work dir,
- no profile, an explicit region and endpoint,
- a credential provider and a token provider that refuse to run.
Credentials are passed explicitly from the named storage profile, or requests are UNSIGNED
(anonymous public S3). Proxies from the environment are disabled (`proxies={}`).
Requester-pays is sent only when the profile enables it.

Readers never see `s3://`: data and index objects are presigned separately (short expiry,
reported as `expires_at`) and then go through the HTTP range preflight.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from botocore import UNSIGNED
from botocore import configprovider as cp
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import Session

from genomics_mcp.config import S3Profile, Settings
from genomics_mcp.errors import (
    GenomicsError,
    NotFoundError,
    UnauthorizedError,
    UpstreamError,
)
from genomics_mcp.models import FileRef
from genomics_mcp.storage.http import HttpAccess
from genomics_mcp.storage.remote import resolve_remote
from genomics_mcp.storage.resolved import StorageResolvedFile
from genomics_mcp.storage.uris import S3Location, parse_s3_uri

if TYPE_CHECKING:
    from genomics_mcp.context import OperationContext

PRESIGN_EXPIRY_S = 900


class AmbientCredentialsRefused(RuntimeError):
    """Raised if anything asks the isolated session for ambient credentials or tokens."""


class _RefuseCredentials:
    def load_credentials(self) -> Any:
        raise AmbientCredentialsRefused("ambient AWS credential lookup is disabled")

    def __getattr__(self, name: str) -> Any:
        raise AmbientCredentialsRefused("ambient AWS credential lookup is disabled")


class _NoToken:
    def load_token(self, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class S3Target:
    profile_name: str | None
    endpoint_url: str
    region: str
    anonymous: bool
    addressing_style: str
    requester_pays: bool
    buckets: tuple[str, ...]

    @property
    def endpoint_host(self) -> str:
        return (urlsplit(self.endpoint_url).hostname or "").lower()


def target_for(settings: Settings, profile_name: str | None) -> S3Target:
    if profile_name:
        p: S3Profile = settings.s3_profile(profile_name)
        return S3Target(
            profile_name,
            p.endpoint_url,
            p.region,
            p.anonymous,
            p.addressing_style,
            p.requester_pays,
            tuple(p.buckets),
        )
    if not settings.storage.allow_public_s3:
        raise UnauthorizedError(
            "anonymous public S3 is disabled in configuration",
            source="s3",
            hint="set file.storage_profile to a configured profile",
        )
    endpoint = settings.storage.public_s3_endpoint.rstrip("/")
    style = "virtual" if endpoint.endswith("amazonaws.com") else "path"
    return S3Target(None, endpoint, settings.storage.public_s3_region, True, style, False, ())


def isolated_session(isolation_dir: Path, region: str) -> Session:
    """A botocore session that cannot read environment variables, ~/.aws or metadata."""
    isolation_dir.mkdir(parents=True, exist_ok=True)
    cfg = isolation_dir / "config"
    creds = isolation_dir / "credentials"
    for f in (cfg, creds):
        if not f.exists():
            f.write_text("")
    session = Session()
    chain = cp.ConfigChainFactory(session=session, environ={})
    mapping = cp._create_config_chain_mapping(chain, cp.BOTOCORE_DEFAUT_SESSION_VARIABLES)
    mapping["s3"] = cp.SectionConfigProvider(
        "s3", session, cp._create_config_chain_mapping(chain, cp.DEFAULT_S3_CONFIG_VARS)
    )
    mapping["proxies_config"] = cp.SectionConfigProvider(
        "proxies_config",
        session,
        cp._create_config_chain_mapping(chain, cp.DEFAULT_PROXIES_CONFIG_VARS),
    )
    mapping["config_file"] = cp.ConstantProvider(str(cfg))
    mapping["credentials_file"] = cp.ConstantProvider(str(creds))
    mapping["profile"] = cp.ConstantProvider(None)
    mapping["region"] = cp.ConstantProvider(region)
    mapping["ca_bundle"] = cp.ConstantProvider(None)
    mapping["data_path"] = cp.ConstantProvider(None)
    session.register_component("config_store", cp.ConfigValueStore(mapping=mapping))
    session.register_component("credential_provider", _RefuseCredentials())
    session.register_component("token_provider", _NoToken())
    return session


class S3Clients:
    """Caches one isolated client per target."""

    def __init__(self, settings: Settings, isolation_dir: Path) -> None:
        self.settings = settings
        self.isolation_dir = isolation_dir
        self._clients: dict[S3Target, Any] = {}
        self._lock = threading.Lock()

    def client(self, target: S3Target) -> Any:
        with self._lock:
            existing = self._clients.get(target)
            if existing is not None:
                return existing
            creds = None
            if target.profile_name and not target.anonymous:
                creds = self.settings.s3_credentials(target.profile_name)
            session = isolated_session(self.isolation_dir, target.region)
            config = Config(
                signature_version=UNSIGNED if creds is None else "s3v4",
                s3={"addressing_style": target.addressing_style},
                connect_timeout=5,
                read_timeout=15,
                retries={"total_max_attempts": 2, "mode": "standard"},
                proxies={},
                region_name=target.region,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            )
            kwargs: dict[str, Any] = {}
            if creds is not None:
                kwargs = {
                    "aws_access_key_id": creds.access_key_id.get_secret_value(),
                    "aws_secret_access_key": creds.secret_access_key.get_secret_value(),
                    "aws_session_token": creds.session_token.get_secret_value()
                    if creds.session_token
                    else None,
                }
            client = session.create_client(
                "s3",
                region_name=target.region,
                endpoint_url=target.endpoint_url,
                config=config,
                **kwargs,
            )
            if target.requester_pays:
                # Readers (HTSlib, libBigWig) cannot send extra headers, so the flag must be a
                # signed query parameter of the presigned URL, not a signed header.
                client.meta.events.register("before-sign.s3", _requester_pays_query)
            self._clients[target] = client
            return client

    def presign(self, target: S3Target, loc: S3Location) -> str:
        params: dict[str, Any] = {"Bucket": loc.bucket, "Key": loc.key}
        return self.client(target).generate_presigned_url(
            "get_object", Params=params, ExpiresIn=PRESIGN_EXPIRY_S
        )


def _requester_pays_query(request: Any, **kwargs: Any) -> None:
    sep = "&" if "?" in request.url else "?"
    if "x-amz-request-payer=" not in request.url:
        request.url += f"{sep}x-amz-request-payer=requester"


def map_client_error(exc: Exception, *, what: str) -> GenomicsError:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = str(err.get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        details = {"s3_error": code, "http_status": status}
        if code in ("NoSuchKey", "NoSuchBucket", "404", "NotFound"):
            return NotFoundError(f"s3: {what} not found", source="s3", details=details)
        if code in (
            "AccessDenied",
            "403",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
            "ExpiredToken",
            "AllAccessDisabled",
        ):
            return UnauthorizedError(f"s3: access denied to {what}", source="s3", details=details)
        if code in ("PermanentRedirect", "301", "AuthorizationHeaderMalformed"):
            return UpstreamError(
                f"s3: {what} is in a different region or endpoint",
                source="s3",
                hint="configure a storage profile with the bucket's region and endpoint",
                details=details,
            )
        return UpstreamError(
            f"s3: request for {what} failed ({code})", source="s3", details=details
        )
    if isinstance(exc, AmbientCredentialsRefused):
        return UnauthorizedError("s3: ambient AWS credentials are never used", source="s3")
    if isinstance(exc, BotoCoreError):
        return UpstreamError(f"s3: {type(exc).__name__}", source="s3", retryable=True)
    return UpstreamError(f"s3: request for {what} failed", source="s3")


def check_bucket(target: S3Target, bucket: str) -> None:
    if target.buckets and bucket not in target.buckets:
        raise UnauthorizedError(
            f"bucket {bucket!r} is not in storage profile {target.profile_name!r}'s allowlist",
            source="s3",
        )


class S3Resolver:
    def __init__(
        self, parts_for: Callable[[OperationContext], tuple[S3Clients, HttpAccess]]
    ) -> None:
        self.parts_for = parts_for

    async def _resolve(self, file: FileRef, ctx: OperationContext, strict: bool):
        clients, http = self.parts_for(ctx)
        loc = parse_s3_uri(file.uri)
        target = target_for(ctx.settings, file.storage_profile)
        check_bucket(target, loc.bucket)
        explicit = None
        if file.index_uri is not None:
            iloc = parse_s3_uri(file.index_uri)
            if iloc.bucket != loc.bucket:
                check_bucket(target, iloc.bucket)
            explicit = iloc.uri

        async def sign(name: str) -> tuple[str, str]:
            obj = parse_s3_uri(name) if name.startswith("s3://") else S3Location(loc.bucket, name)
            try:
                url = await ctx.run_blocking(clients.presign, target, obj)
            except GenomicsError:
                raise
            except Exception as exc:  # noqa: BLE001 - mapped to a typed storage error
                raise map_client_error(exc, what="object") from None
            return url, obj.uri

        trusted = (target.endpoint_host,) if target.profile_name else ()
        return await resolve_remote(
            ctx,
            http,
            file,
            access="s3",
            source="s3",
            data_name=loc.uri,
            explicit_index=explicit,
            sign=sign,
            probe_sidecars=True,
            extra_trusted=trusted,
            strict=strict,
        )

    async def resolve(self, file: FileRef, ctx: OperationContext) -> StorageResolvedFile:
        return await self._resolve(file, ctx, True)

    async def stat(self, file: FileRef, ctx: OperationContext) -> FileRef:
        return (await self._resolve(file, ctx, False)).file
