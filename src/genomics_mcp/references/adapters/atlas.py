"""Optional AlphaGenome Atlas adapter (precomputed variant scores only).

Only the AtlasService RPCs defined by the official client
(google-deepmind/alphagenome, ``alphagenome/protos/atlas_service.proto``) are
used: ``GetDenseVariantScores`` and ``ListVariantScoresMetadata``. There is no
path to live AlphaGenome model inference (the separate DnaModel service) and no
fallback to it. The adapter is disabled unless an API key is supplied
explicitly; the key is never included in errors or evidence.

Terms: https://alphagenome.google/terms — outputs are for non-commercial use,
not for clinical decision-making, and must not be used to train other models.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import math
import re
import struct
import time
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import SecretStr

from ..http import RateBudgetExceeded, RateLimiter, SourceFailure, failure, redact_text
from ..models import CanonicalVariant, Evidence, Transformation, Truncation
from ..sources import SOURCES

INFO = SOURCES["alphagenome_atlas"]
DEFAULT_ADDRESS = "dns:///gdmscience.googleapis.com:443"
SCORER_NAME_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
MAX_VALUES = 64
MAX_LABELS = 64
TOP_N = 10

_GRPC_KIND = {
    "UNAUTHENTICATED": "unauthorized",
    "PERMISSION_DENIED": "forbidden",
    "NOT_FOUND": "not_found",
    "INVALID_ARGUMENT": "invalid_input",
    "OUT_OF_RANGE": "invalid_input",
    "RESOURCE_EXHAUSTED": "rate_limited",
    "DEADLINE_EXCEEDED": "timeout",
    "UNAVAILABLE": "upstream",
    "UNIMPLEMENTED": "unsupported",
}


class AtlasTransport(Protocol):
    """Async access to the named AtlasService RPCs. Returns proto-like objects."""

    async def get_dense_variant_scores(
        self,
        *,
        chromosome: str,
        position: int,
        reference_bases: str,
        alternate_bases: str,
        filter: str,
        timeout_s: float,
    ) -> Any: ...

    async def list_variant_scores_metadata(self, *, timeout_s: float) -> Any: ...


class GrpcAtlasTransport:
    """Transport built on the official ``alphagenome`` protos and gRPC asyncio.

    Requires the optional ``alphagenome`` package (which brings ``grpcio``). It is
    not a dependency of this project; without it the adapter reports
    ``unsupported``.
    """

    def __init__(self, api_key: SecretStr, *, address: str = DEFAULT_ADDRESS):
        try:
            import grpc  # type: ignore[import-not-found]
            from alphagenome.protos import (  # type: ignore[import-not-found]
                atlas_service_pb2,
                atlas_service_pb2_grpc,
                dna_model_pb2,
            )
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise failure(
                "alphagenome_atlas",
                "connect",
                "unsupported",
                "the optional 'alphagenome' client package is not installed",
            ) from exc
        self._grpc = grpc
        self._pb = atlas_service_pb2
        self._dna = dna_model_pb2
        service_config = (
            importlib.resources.files("alphagenome") / "protos/grpc_service_config.json"
        ).read_text()
        channel = grpc.aio.secure_channel(
            address,
            grpc.ssl_channel_credentials(),
            options=(("grpc.service_config", service_config),),
        )
        self._channel = channel
        self._stub = atlas_service_pb2_grpc.AtlasServiceStub(channel)
        self._metadata = (("x-goog-api-key", api_key.get_secret_value()),)

    def build_request(
        self,
        *,
        chromosome: str,
        position: int,
        reference_bases: str,
        alternate_bases: str,
        filter: str,
    ) -> Any:
        return self._pb.GetDenseVariantScoresRequest(
            variant=self._dna.Variant(
                chromosome=chromosome,
                position=position,
                reference_bases=reference_bases,
                alternate_bases=alternate_bases,
            ),
            organism=self._dna.ORGANISM_HOMO_SAPIENS,
            filter=filter,
        )

    async def get_dense_variant_scores(
        self,
        *,
        chromosome: str,
        position: int,
        reference_bases: str,
        alternate_bases: str,
        filter: str,
        timeout_s: float,
    ) -> Any:
        request = self.build_request(
            chromosome=chromosome,
            position=position,
            reference_bases=reference_bases,
            alternate_bases=alternate_bases,
            filter=filter,
        )
        return await self._stub.GetDenseVariantScores(
            request, metadata=self._metadata, timeout=timeout_s
        )

    async def aclose(self) -> None:
        await self._channel.close()

    async def list_variant_scores_metadata(self, *, timeout_s: float) -> Any:
        request = self._pb.ListVariantScoresMetadataRequest(
            organism=self._dna.ORGANISM_HOMO_SAPIENS
        )
        return await self._stub.ListVariantScoresMetadata(
            request, metadata=self._metadata, timeout=timeout_s
        )


def build_filter(requested_scorers: Sequence[str]) -> str:
    """Same scorer filter syntax as ``alphagenome.atlas.atlas_utils.build_filter``."""
    for name in requested_scorers:
        if not SCORER_NAME_RE.match(name):
            raise failure(
                "alphagenome_atlas",
                "get_dense_variant_scores",
                "invalid_input",
                f"invalid scorer name {name!r}",
            )
    joined = " OR ".join(f'scores.variant_scorer.name = "{s}"' for s in requested_scorers)
    return f"({joined})" if joined else ""


def decode_floats(raw: bytes, shape: Sequence[int]) -> list[float]:
    n = math.prod(shape) if shape else 0
    if len(raw) != 4 * n:
        raise failure(
            "alphagenome_atlas",
            "get_dense_variant_scores",
            "invalid_response",
            f"score buffer has {len(raw)} bytes; shape {list(shape)} needs {4 * n}",
        )
    return list(struct.unpack(f"<{n}f", raw)) if n else []


def _which(md: Any) -> str | None:
    if hasattr(md, "WhichOneof"):
        return md.WhichOneof("payload")
    return getattr(md, "payload", None)


def _has(msg: Any, name: str) -> bool:
    if hasattr(msg, "HasField"):
        try:
            return bool(msg.HasField(name))
        except ValueError:
            return bool(getattr(msg, name, None))
    return bool(getattr(msg, name, None))


class AtlasClient:
    def __init__(
        self,
        transport: AtlasTransport | None,
        *,
        limiter: RateLimiter | None = None,
        timeout: float = 20.0,
        secret: SecretStr | None = None,
        clock: Any = time.monotonic,
    ):
        self.transport = transport
        self.limiter = limiter or RateLimiter(2, 1.0)
        self.timeout = timeout
        self._secret = secret
        self._clock = clock
        self._scorers: list[dict[str, Any]] | None = None

    @property
    def configured(self) -> bool:
        return self.transport is not None

    def _require(self, operation: str) -> AtlasTransport:
        if self.transport is None:
            raise failure(
                "alphagenome_atlas",
                operation,
                "not_configured",
                "AlphaGenome Atlas is disabled: no API key was configured explicitly. See "
                + INFO.terms_url,
            )
        return self.transport

    def _map_error(self, exc: Exception, operation: str) -> SourceFailure:
        if isinstance(exc, SourceFailure):
            return exc
        if isinstance(exc, asyncio.TimeoutError):
            return failure(
                "alphagenome_atlas",
                operation,
                "timeout",
                "no response before the deadline",
                retryable=True,
            )
        code = getattr(exc, "code", None)
        name = None
        if callable(code):
            try:
                name = getattr(code(), "name", None)
            except Exception:  # noqa: BLE001 - defensive: foreign exception type
                name = None
        details = ""
        if callable(getattr(exc, "details", None)):
            try:
                details = str(exc.details() or "")
            except Exception:  # noqa: BLE001
                details = ""
        if self._secret is not None:
            details = details.replace(self._secret.get_secret_value(), "REDACTED")
        details = redact_text(" ".join(details.split()))[:300]
        kind = _GRPC_KIND.get(name or "", "upstream")
        message = f"gRPC {name or type(exc).__name__}" + (f": {details}" if details else "")
        return failure(
            "alphagenome_atlas",
            operation,
            kind,
            message,  # type: ignore[arg-type]
            retryable=name in ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED"),
        )

    async def _call(self, operation: str, deadline: float | None, fn: Any, **kwargs: Any) -> Any:
        try:
            await self.limiter.acquire(deadline)
        except RateBudgetExceeded as exc:
            raise failure(
                "alphagenome_atlas", operation, "rate_limited", str(exc), retryable=True
            ) from None
        remaining = None if deadline is None else deadline - self._clock()
        if remaining is not None and remaining <= 0:
            raise failure(
                "alphagenome_atlas", operation, "timeout", "deadline exhausted before request"
            )
        timeout = self.timeout if remaining is None else max(0.1, min(self.timeout, remaining))
        try:
            return await asyncio.wait_for(fn(timeout_s=timeout, **kwargs), timeout + 1.0)
        except Exception as exc:  # noqa: BLE001 - mapped to structured errors
            raise self._map_error(exc, operation) from None

    async def scorers(self, *, deadline: float | None = None) -> list[dict[str, Any]]:
        transport = self._require("list_variant_scores_metadata")
        if self._scorers is None:
            response = await self._call(
                "list_variant_scores_metadata", deadline, transport.list_variant_scores_metadata
            )
            out = []
            for item in getattr(response, "variant_scorer_metadata", []) or []:
                info = item.variant_scorer
                track_count = 0
                for md in getattr(item, "metadata", []) or []:
                    if _which(md) == "tracks":
                        track_count = len(md.tracks.metadata)
                out.append(
                    {
                        "name": info.name,
                        "is_signed": bool(info.is_signed),
                        "track_count": track_count,
                    }
                )
            self._scorers = out
        return self._scorers

    async def variant_scores(
        self,
        variant: CanonicalVariant,
        *,
        requested_scorers: Sequence[str] | None = None,
        max_values: int = MAX_VALUES,
        deadline: float | None = None,
    ) -> Evidence:
        operation = "get_dense_variant_scores"
        transport = self._require(operation)
        if variant.assembly != "GRCh38":
            raise failure(
                "alphagenome_atlas",
                operation,
                "unsupported",
                "this adapter only queries Atlas with GRCh38 variants; no liftover is applied",
            )
        if variant.variant_class != "SNV" or variant.vcf is None:
            raise failure(
                "alphagenome_atlas",
                operation,
                "unsupported",
                "Atlas precomputed scores are documented for single-nucleotide variants only",
            )
        chromosome = "chrM" if variant.contig == "MT" else f"chr{variant.contig}"
        filter_text = build_filter(list(requested_scorers or []))
        response = await self._call(
            operation,
            deadline,
            transport.get_dense_variant_scores,
            chromosome=chromosome,
            position=variant.vcf.pos,
            reference_bases=variant.vcf.ref,
            alternate_bases=variant.vcf.alt,
            filter=filter_text,
        )
        returned = getattr(response, "variant", None)
        if returned is not None and (
            returned.chromosome,
            int(returned.position),
            returned.reference_bases,
            returned.alternate_bases,
        ) != (chromosome, variant.vcf.pos, variant.vcf.ref, variant.vcf.alt):
            raise failure(
                "alphagenome_atlas",
                operation,
                "invalid_response",
                "Atlas returned a different variant",
            )
        scorers = []
        truncation: list[Truncation] = []
        for score in getattr(response, "scores", []) or []:
            scorers.append(self._decode_score(score, max_values, truncation))
        interval = None
        if _has(response, "interval"):
            iv = response.interval
            interval = {"chromosome": iv.chromosome, "start": int(iv.start), "end": int(iv.end)}
        limitations = [
            "Precomputed AlphaGenome Atlas model predictions, not observed data.",
            "Non-commercial use only; not for clinical decision-making; must not be used to train other ML models "
            f"({INFO.terms_url}).",
            "The Atlas response does not report a dataset or model version.",
        ]
        if not scorers:
            limitations.append("Atlas returned no scores for this variant and filter.")
        return Evidence(
            source="alphagenome_atlas",
            evidence_type="functional_prediction",
            source_record_id=f"{chromosome}:{variant.vcf.pos}:{variant.vcf.ref}>{variant.vcf.alt}",
            source_url="https://alphagenome.google/atlas",
            terms_url=INFO.terms_url,
            data={
                "prediction_origin": "precomputed (AtlasService.GetDenseVariantScores)",
                "live_inference": False,
                "variant": {
                    "chromosome": chromosome,
                    "position_1based": variant.vcf.pos,
                    "reference_bases": variant.vcf.ref,
                    "alternate_bases": variant.vcf.alt,
                },
                "scoring_interval_0based": interval,
                "requested_scorers": list(requested_scorers or []),
                "scorers": scorers,
            },
            transformations=[
                Transformation(
                    operation="to_atlas_variant",
                    detail=f"contig {variant.contig} written as {chromosome}; "
                    "position is 1-based as required by the Atlas Variant message",
                ),
                Transformation(
                    operation="decode_scores",
                    detail="float32 row-major score buffers decoded; values beyond "
                    "max_values omitted; top_by_abs_score is a local ranking of the returned matrix",
                ),
            ],
            limitations=limitations,
            truncation=truncation,
        )

    def _decode_score(
        self, score: Any, max_values: int, truncation: list[Truncation]
    ) -> dict[str, Any]:
        info = score.variant_scorer
        shape = [int(x) for x in score.shape]
        values = decode_floats(bytes(score.scores), shape)
        calibrated_raw = bytes(getattr(score, "calibrated_scores", b"") or b"")
        calibrated = decode_floats(calibrated_raw, shape) if calibrated_raw else None
        rows: list[str] | None = None
        cols: list[str] | None = None
        for md in getattr(score, "metadata", []) or []:
            kind = _which(md)
            if kind == "gene_scorers":
                rows = [
                    f"{g.gene_id}" + (f" ({g.name})" if _has(g, "name") else "")
                    for g in md.gene_scorers.metadata
                ]
            elif kind == "tracks":
                cols = [t.name for t in md.tracks.metadata]
        ncols = shape[-1] if len(shape) >= 2 else (shape[0] if shape else 0)

        def label(i: int) -> dict[str, Any]:
            r, c = divmod(i, ncols) if len(shape) >= 2 and ncols else (0, i)
            out: dict[str, Any] = {"index": i}
            if rows is not None and r < len(rows):
                out["row"] = rows[r]
            if cols is not None and c < len(cols):
                out["column"] = cols[c]
            return out

        ranked = sorted(range(len(values)), key=lambda i: abs(values[i]), reverse=True)[:TOP_N]
        top = [
            {**label(i), "score": values[i], **({"quantile": calibrated[i]} if calibrated else {})}
            for i in ranked
        ]
        name = info.name
        if len(values) > max_values:
            truncation.append(
                Truncation(
                    field=f"scorers[{name}].values",
                    returned=max_values,
                    available=len(values),
                    reason="compact response limit (max_values)",
                )
            )
        return {
            "name": name,
            "is_signed": bool(getattr(info, "is_signed", False)),
            "shape": shape,
            "values_row_major": values[:max_values],
            "calibrated_quantiles_row_major": calibrated[:max_values] if calibrated else None,
            "row_labels": rows[:MAX_LABELS] if rows else None,
            "column_labels": cols[:MAX_LABELS] if cols else None,
            "top_by_abs_score": top,
        }
