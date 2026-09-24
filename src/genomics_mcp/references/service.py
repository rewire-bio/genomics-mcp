"""Reference tool implementations: resolve_identifier, normalize_variant,
lookup_variant, lookup_gene and lookup_protein.

Independent source calls run concurrently; each failure is isolated into a
typed ``SourceError`` and never hides results from other sources.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import SecretStr

from .adapters.atlas import AtlasClient, AtlasTransport, GrpcAtlasTransport
from .adapters.clinvar import ClinvarClient
from .adapters.ensembl import ENSEMBL_ID_RE, EnsemblClient, split_version
from .adapters.gnomad import GnomadClient
from .adapters.hgnc import GeneResolution, HgncClient, detect_gene_id_type
from .adapters.ncbi import NcbiIdentity, NcbiSequenceClient, NcbiVariationClient, SpdiPlacement
from .adapters.opentargets import OpenTargetsClient
from .adapters.uniprot import ACCESSION_RE, UniprotClient
from .assemblies import AssemblyError, normalize_assembly, normalize_contig, refseq_accession
from .http import RateLimiter, SourceFailure, SourceHttp, failure
from .models import (
    Assembly,
    CanonicalVariant,
    Evidence,
    IdentifierCandidate,
    SourceError,
    Transformation,
    Truncation,
    VariantCandidate,
    VcfRepresentation,
)
from .schemas import (
    GenomicAlleleInput,
    LookupGeneRequest,
    LookupGeneResult,
    LookupProteinRequest,
    LookupProteinResult,
    LookupVariantRequest,
    LookupVariantResult,
    NormalizeVariantRequest,
    NormalizeVariantResult,
    ResolveIdentifierRequest,
    ResolveIdentifierResult,
    ToolResult,
    VariantEvidence,
)
from .variants import (
    HGVS_RE,
    NeedMoreReference,
    RawAllele,
    ReferenceWindow,
    VariantInputError,
    accession_has_version,
    detect_kind,
    normalize_allele,
    parse_hgvs_genomic,
    parse_spdi,
    parse_vcf_string,
    structured_allele,
)

T = TypeVar("T")

MAX_RESPONSE_BYTES = 1024 * 1024
REFERENCE_PAD = 64
MAX_REFERENCE_PAD = 4096
MAX_VARIANT_SPAN = 10_000
REFSEQ_TX_RE = re.compile(r"^(NM_|NR_|XM_|XR_)\d+\.\d+$")
REFSEQ_PROTEIN_RE = re.compile(r"^(NP_|XP_)\d+(?:\.\d+)?$")
VCV_RE = re.compile(r"^VCV0*(\d+)(?:\.(\d+))?$", re.IGNORECASE)


class ReferenceProvider(Protocol):
    """Optional local reference (e.g. an indexed FASTA prepared by the core)."""

    async def sequence(self, assembly: Assembly, contig: str, start: int, end: int, *, deadline: float | None = None) -> ReferenceWindow: ...


@dataclass
class ReferenceConfig:
    """Explicit configuration; nothing here is read from the environment."""

    ncbi_api_key: SecretStr | None = None
    ncbi_email: str | None = None
    atlas_api_key: SecretStr | None = None
    deadline_seconds: float = 30.0
    timeouts: dict[str, float] = field(default_factory=dict)
    max_retries: int = 2
    enabled_sources: frozenset[str] | None = None

    def timeout(self, source: str) -> float:
        return self.timeouts.get(source, 15.0)


@dataclass
class _Collector:
    errors: list[SourceError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    transformations: list[Transformation] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    network_allowed: bool = True

    def error(self, err: SourceError) -> None:
        if err not in self.errors:
            self.errors.append(err)


def _unexpected(source: str, operation: str, exc: BaseException) -> SourceError:
    return SourceError(source=source, operation=operation, kind="upstream",
                       message=f"unexpected {type(exc).__name__} while processing the response")


async def _guard(
    source: str, operation: str, awaitable: Awaitable[T], col: _Collector, timeout: float | None = None
) -> T | None:
    """Run one source call; its failure or overrun becomes a SourceError and never affects other sources."""
    try:
        if timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, max(0.0, timeout))
    except TimeoutError:
        col.error(SourceError(source=source, operation=operation, kind="timeout", retryable=True,
                              message="source did not finish before the call deadline; other sources are unaffected"))
    except SourceFailure as exc:
        col.error(exc.error)
    except (VariantInputError, AssemblyError) as exc:
        col.error(SourceError(source=source, operation=operation, kind="invalid_input", message=str(exc)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - isolate one source's defect
        col.error(_unexpected(source, operation, exc))
    return None


def _status(result_present: bool, col: _Collector) -> str:
    if not result_present:
        return "error"
    return "partial" if col.errors else "ok"


class ReferenceService:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: ReferenceConfig | None = None,
        *,
        atlas_transport: AtlasTransport | None = None,
        reference_provider: ReferenceProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.config = config or ReferenceConfig()
        self.clock = clock
        cfg = self.config
        ncbi_rate = 10 if cfg.ncbi_api_key is not None else 3

        def http(source: str, limiter: RateLimiter) -> SourceHttp:
            return SourceHttp(client, source=source, limiter=limiter, timeout=cfg.timeout(source),
                              max_retries=cfg.max_retries, clock=clock, sleep=sleep)

        def limiter(rate: int, per: float) -> RateLimiter:
            return RateLimiter(rate, per, clock=clock, sleep=sleep)

        ncbi_limiter = limiter(ncbi_rate, 1.0)
        identity = NcbiIdentity(api_key=cfg.ncbi_api_key, email=cfg.ncbi_email)
        self.hgnc = HgncClient(http("hgnc", limiter(10, 1.0)))
        self.ensembl = EnsemblClient(http("ensembl", limiter(10, 1.0)))
        self.clinvar = ClinvarClient(http("clinvar", ncbi_limiter), identity)
        self.ncbi_sequence = NcbiSequenceClient(http("ncbi_nuccore", ncbi_limiter), identity)
        self.ncbi_variation = NcbiVariationClient(http("ncbi_variation", ncbi_limiter))
        self.gnomad = GnomadClient(http("gnomad", limiter(10, 60.0)))
        self.uniprot = UniprotClient(http("uniprot", limiter(10, 1.0)))
        self.opentargets = OpenTargetsClient(http("opentargets", limiter(5, 1.0)))
        self._atlas_transport = atlas_transport
        self._atlas: AtlasClient | None = None
        self._atlas_limiter = limiter(2, 1.0)
        self.reference_provider = reference_provider

    # ----------------------------------------------------------------- helpers
    def _deadline(self) -> float:
        return self.clock() + self.config.deadline_seconds

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self.clock())

    def _enabled(self, source: str) -> bool:
        return self.config.enabled_sources is None or source in self.config.enabled_sources

    def _disabled(self, source: str, operation: str) -> SourceError:
        return SourceError(source=source, operation=operation, kind="not_configured",
                           message=f"{source} is disabled by configuration")

    def atlas(self) -> AtlasClient:
        if self._atlas is None:
            transport = self._atlas_transport
            if transport is None and self.config.atlas_api_key is not None:
                transport = GrpcAtlasTransport(self.config.atlas_api_key)
            self._atlas = AtlasClient(transport, limiter=self._atlas_limiter, timeout=self.config.timeout("alphagenome_atlas"),
                                      secret=self.config.atlas_api_key, clock=self.clock)
        return self._atlas

    def _finish(self, result: ToolResult, col: _Collector) -> None:
        result.errors = col.errors
        result.warnings = list(dict.fromkeys(col.warnings))
        result.transformations = col.transformations
        result.limitations = list(dict.fromkeys(col.limitations))
        _enforce_size(result)

    # --------------------------------------------------------- normalization
    def _supplied_window(self, request: NormalizeVariantRequest, assembly: Assembly, contig: str) -> ReferenceWindow | None:
        supplied = request.reference
        if supplied is None:
            return None
        tmp: list[Transformation] = []
        try:
            s_asm = normalize_assembly(supplied.assembly, tmp)
            s_contig = normalize_contig(supplied.contig, s_asm, tmp)
        except AssemblyError as exc:
            raise VariantInputError(f"reference: {exc}") from None
        if (s_asm, s_contig) != (assembly, contig):
            raise VariantInputError("supplied reference sequence is for a different assembly/contig; no liftover is applied")
        return ReferenceWindow(assembly=assembly, contig=contig, start=supplied.start,
                               sequence=supplied.sequence.upper(), source=supplied.source,
                               at_contig_start=supplied.at_contig_start or supplied.start == 0,
                               at_contig_end=supplied.at_contig_end)

    async def _fetched_window(
        self, request: NormalizeVariantRequest, assembly: Assembly, contig: str, start: int, end: int,
        deadline: float, col: _Collector,
    ) -> ReferenceWindow | None:
        """Reference from the local provider, then (with consent and use_remote_reference) Ensembl, then NCBI.

        Every provider is asked for the same assembly; no other build is ever substituted.
        """
        providers: list[tuple[str, Any]] = []
        if self.reference_provider is not None:
            providers.append(("local_reference", self.reference_provider.sequence))
        if request.use_remote_reference and col.network_allowed:
            if self._enabled("ensembl"):
                providers.append(("ensembl", self.ensembl.sequence))
            providers.append(("ncbi_nuccore", self.ncbi_sequence.sequence))
        for name, fetch in providers:
            try:
                window = await fetch(assembly, contig, max(0, start), end, deadline=deadline)
            except SourceFailure as exc:
                col.error(exc.error)
                continue
            except Exception as exc:  # noqa: BLE001
                col.error(_unexpected(name, "sequence", exc))
                continue
            if (window.assembly, window.contig) != (assembly, contig):
                col.error(SourceError(source=name, operation="sequence", kind="invalid_response",
                                      message=f"provider returned {window.assembly} {window.contig}, "
                                      f"requested {assembly} {contig}; not used"))
                continue
            if name not in col.sources:
                col.sources.append(name)
            return window
        return None

    async def _normalize_raw(
        self, raw: RawAllele, request: NormalizeVariantRequest, deadline: float, col: _Collector
    ) -> tuple[CanonicalVariant, list[Transformation], list[str]]:
        if raw.end - raw.start > MAX_VARIANT_SPAN:
            raise VariantInputError(f"variant spans more than {MAX_VARIANT_SPAN} bases")
        can_fetch = self.reference_provider is not None or (request.use_remote_reference and col.network_allowed)
        supplied = self._supplied_window(request, raw.assembly, raw.contig)
        if supplied is not None:
            # REF verification only needs the allele span; shifting may need more.
            if supplied.covers(raw.start, raw.end):
                try:
                    outcome = normalize_allele(raw, supplied)
                    return outcome.variant, outcome.transformations, outcome.limitations
                except NeedMoreReference:
                    if not can_fetch:
                        outcome = normalize_allele(raw, supplied, allow_incomplete=True)
                        outcome.limitations.append(
                            "Supplied reference verified REF but is too short to shift the indel.")
                        return outcome.variant, outcome.transformations, outcome.limitations
                    col.warnings.append("Supplied reference verified REF; more sequence was fetched to shift the indel.")
            else:
                col.warnings.append("Supplied reference sequence does not cover the variant; it was not used.")
        pad = REFERENCE_PAD
        while can_fetch:
            window = await self._fetched_window(
                request, raw.assembly, raw.contig, max(0, raw.start - pad), raw.end + pad, deadline, col
            )
            if window is None:
                break
            if not window.covers(raw.start, raw.end):
                col.warnings.append(f"{window.source} did not return the full variant interval; it was not used.")
                break
            try:
                outcome = normalize_allele(raw, window, allow_incomplete=pad >= MAX_REFERENCE_PAD)
                return outcome.variant, outcome.transformations, outcome.limitations
            except NeedMoreReference:
                pad *= 4
        if request.use_remote_reference and not col.network_allowed:
            col.limitations.append(
                "Remote reference sequence was not fetched: the variant is from a private file and "
                "allow_external_queries was not set; REF was not verified.")
        elif can_fetch or supplied is not None:
            col.limitations.append("Reference sequence was unavailable; REF was not verified.")
        outcome = normalize_allele(raw, None)
        return outcome.variant, outcome.transformations, outcome.limitations

    async def normalize_variant(self, request: NormalizeVariantRequest) -> NormalizeVariantResult:
        deadline = self._deadline()
        col = _Collector()
        result = await self._normalize(request, deadline, col)
        result.query = _query(request)
        self._finish(result, col)
        return result

    async def _normalize(self, request: NormalizeVariantRequest, deadline: float, col: _Collector) -> NormalizeVariantResult:
        variant = request.variant
        kind = "structured" if isinstance(variant, GenomicAlleleInput) else detect_kind(variant)
        result = NormalizeVariantResult(tool="normalize_variant", status="error", query={}, input_kind=kind)
        col.network_allowed = _external_allowed(request)
        if not col.network_allowed and _needs_remote_resolution(variant, kind):
            col.error(_egress_forbidden("normalize_variant"))
            return result
        try:
            raws = await self._raw_alleles(request, kind, deadline, col, result)
        except VariantInputError as exc:
            col.error(SourceError(source="local", operation="parse", kind="invalid_input", message=str(exc)))
            return result
        if raws is None:
            return result
        alleles: list[CanonicalVariant] = []
        for raw in raws:
            try:
                variant_out, steps, limits = await self._normalize_raw(raw, request, deadline, col)
            except VariantInputError as exc:
                col.error(SourceError(source="local", operation="normalize", kind="invalid_input", message=str(exc)))
                continue
            alleles.append(variant_out)
            prefix = f"allele {raw.alt or '-'}: " if len(raws) > 1 else ""
            col.transformations.extend(
                t if not prefix else t.model_copy(update={"detail": prefix + t.detail}) for t in steps
            )
            col.limitations.extend(limits)
        result.alleles = alleles
        if not alleles:
            return result
        mismatch = [a for a in alleles if a.normalization_status == "reference_mismatch"]
        if mismatch:
            col.error(SourceError(source=mismatch[0].reference_check.source or "local", operation="reference_check",
                                  kind="invalid_input",
                                  message=f"REF {mismatch[0].reference_check.expected_ref!r} does not match reference "
                                  f"{mismatch[0].reference_check.observed_ref!r}"))
            result.status = "error"
            return result
        if len(alleles) == 1 and len(raws) == 1:
            result.canonical_variant = alleles[0]
            result.status = "partial" if col.errors else "ok"
        else:
            result.status = "ambiguous"
            col.warnings.append(f"Input describes {len(alleles)} alleles; each is normalized separately and none was selected.")
        return result

    async def _raw_alleles(
        self, request: NormalizeVariantRequest, kind: str, deadline: float, col: _Collector,
        result: NormalizeVariantResult,
    ) -> list[RawAllele] | None:
        v = request.variant
        if isinstance(v, GenomicAlleleInput):
            if request.assembly and request.assembly != v.assembly:
                raise VariantInputError("assembly given twice with different values")
            return structured_allele(assembly=v.assembly, contig=v.contig, ref=v.ref, alt=v.alt,
                                     position=v.position, start=v.start)
        text = v.strip()
        if kind == "vcf":
            return parse_vcf_string(text, request.assembly)
        if kind == "spdi":
            return parse_spdi(text, request.assembly)
        if kind == "hgvs_genomic":
            m = HGVS_RE.match(text)
            assert m
            if m.group("acc").upper().startswith("NC_") or not m.group("acc").upper().startswith(("NG_", "LRG_")):
                return parse_hgvs_genomic(text, request.assembly)
            return await self._remote_hgvs(text, kind, request, deadline, col, result)
        if kind in ("hgvs_coding", "hgvs_noncoding"):
            return await self._remote_hgvs(text, kind, request, deadline, col, result)
        if kind == "hgvs_protein":
            await self._protein_candidates(text, request, deadline, col, result)
            return None
        if kind == "rsid":
            return await self._rsid(text, request, deadline, col, result)
        raise VariantInputError(
            "unrecognized variant; use a VCF string (7-140753336-A-T), SPDI, HGVS with a versioned accession, "
            "an rsID, or a structured allele"
        )

    def _require_assembly(self, request: NormalizeVariantRequest, col: _Collector) -> Assembly:
        try:
            return normalize_assembly(request.assembly, col.transformations)
        except AssemblyError as exc:
            raise VariantInputError(str(exc)) from None

    async def _remote_hgvs(
        self, text: str, kind: str, request: NormalizeVariantRequest, deadline: float, col: _Collector,
        result: NormalizeVariantResult,
    ) -> list[RawAllele] | None:
        if not col.network_allowed:
            raise VariantInputError("external resolution is not permitted for this private-file query")
        m = HGVS_RE.match(text)
        assert m
        accession = m.group("acc")
        if m.group("paren"):
            col.transformations.append(Transformation(
                operation="drop_hgvs_gene_label", detail=f"gene label ({m.group('paren')}) removed from the HGVS reference"))
            text = f"{accession}:{m.group('kind')}.{m.group('change')}"
        if not accession_has_version(accession):
            raise VariantInputError(
                f"{accession} has no version; transcript/accession versions are required to avoid silent remapping"
            )
        assembly = self._require_assembly(request, col)
        placements: list[tuple[str, str, int, str, str, list[str]]] = []
        notes: list[str] = []
        used = None
        is_refseq = accession.upper().startswith(("NM_", "NR_", "XM_", "XR_", "NG_", "NC_", "LRG_"))
        order = ["ncbi_variation", "ensembl"] if is_refseq else ["ensembl", "ncbi_variation"]
        for source in order:
            if source == "ensembl" and not self._enabled("ensembl"):
                continue
            try:
                if source == "ncbi_variation":
                    if not is_refseq:
                        continue
                    found, extra = await self.ncbi_variation.hgvs_to_genomic(text, assembly, deadline=deadline)
                    placements = [(p.accession, p.contig, p.position, p.deleted, p.inserted, []) for p in found]
                    notes = extra
                else:
                    placements, notes = await self._recoder_placements(text, assembly, deadline, accession)
            except SourceFailure as exc:
                col.error(exc.error)
                if exc.error.kind == "invalid_input":
                    return None
                continue
            used = source
            break
        if used is None:
            return None
        col.sources.append(used)
        col.warnings.extend(notes)
        unique = {(p[1], p[2], p[3], p[4]): p for p in placements}
        if len(unique) == 1:
            p = next(iter(unique.values()))
            if p[5]:
                result.candidates = [self._placement_candidate(p, assembly, used)]
                col.warnings.extend(p[5])
                result.status = "unresolved"
                return None
            raw = self._raw_from_placement(p, assembly, used)
            raw.transformations.insert(0, Transformation(
                operation="transcript_to_genome" if kind != "hgvs_genomic" else "hgvs_to_chromosome",
                source=used, detail=f"{text} mapped to {assembly} chromosome {p[1]} by {used}",
                before={"hgvs": text}, after={"spdi": f"{p[0]}:{p[2]}:{p[3]}:{p[4]}"},
            ))
            return [raw]
        if not unique:
            col.warnings.append(f"{used} did not place {text} on {assembly}; no liftover or remapping is attempted")
            result.status = "unresolved"
            return None
        result.candidates = [self._placement_candidate(p, assembly, used) for p in unique.values()]
        result.status = "ambiguous"
        col.warnings.append(f"{text} maps to {len(unique)} genomic alleles on {assembly}; none was selected")
        return None

    async def _recoder_placements(
        self, text: str, assembly: Assembly, deadline: float, accession: str | None
    ) -> tuple[list[tuple[str, str, int, str, str, list[str]]], list[str]]:
        items = await self.ensembl.variant_recoder(text, assembly, deadline=deadline)
        out: list[tuple[str, str, int, str, str, list[str]]] = []
        notes: list[str] = []
        for item in items:
            for w in item.get("warnings") or []:
                notes.append(f"Ensembl warning: {' '.join(str(w).split())[:300]}")
            for allele_key, entry in item.items():
                if not isinstance(entry, dict):
                    continue
                named = [h for key in ("hgvsc", "hgvsn", "hgvsg") for h in entry.get(key) or [] if isinstance(h, str)]
                caveats: list[str] = []
                if accession and not any(h.split(":", 1)[0] == accession for h in named):
                    caveats.append(
                        f"{accession} does not appear in the Ensembl output; Ensembl may have used a different "
                        "transcript version, so this allele was not selected"
                    )
                for vcf in entry.get("vcf_string") or []:
                    parts = str(vcf).split("-")
                    if len(parts) != 4 or not parts[1].isdigit():
                        continue
                    contig, pos, ref, alt = parts[0], int(parts[1]), parts[2].upper(), parts[3].upper()
                    acc = refseq_accession(assembly, contig) or contig
                    out.append((acc, contig, pos - 1, ref, alt, caveats))
        return out, notes

    @staticmethod
    def _raw_from_placement(p: tuple[str, str, int, str, str, list[str]], assembly: Assembly, source: str) -> RawAllele:
        acc, contig, start, deleted, inserted, _ = p
        return RawAllele(assembly=assembly, contig=contig, start=start, end=start + len(deleted),
                         ref=deleted, alt=inserted, origin=source)

    @staticmethod
    def _placement_candidate(p: tuple[str, str, int, str, str, list[str]], assembly: Assembly, source: str) -> VariantCandidate:
        acc, contig, start, deleted, inserted, caveats = p
        vcf = VcfRepresentation(contig=contig, pos=start + 1, ref=deleted, alt=inserted) if deleted and inserted else None
        return VariantCandidate(
            description=f"{contig}:{start}-{start + len(deleted)} {deleted or '-'}>{inserted or '-'} (0-based)",
            source=source, assembly=assembly, contig=contig, vcf=vcf,
            spdi=f"{acc}:{start}:{deleted}:{inserted}" if acc.startswith("NC_") else None, notes=list(caveats),
        )

    async def _rsid(
        self, text: str, request: NormalizeVariantRequest, deadline: float, col: _Collector,
        result: NormalizeVariantResult,
    ) -> list[RawAllele] | None:
        if not col.network_allowed:
            raise VariantInputError("external resolution is not permitted for this private-file query")
        assembly = self._require_assembly(request, col)
        placements: list[SpdiPlacement] | None = None
        used = None
        try:
            ref = await self.ncbi_variation.refsnp(text, assembly, deadline=deadline)
            placements = ref.placements
            col.transformations.extend(ref.transformations)
            col.warnings.extend(ref.notes)
            used = "ncbi_variation"
            if ref.last_update_build:
                col.warnings.append(f"dbSNP record last updated in build {ref.last_update_build}")
        except SourceFailure as exc:
            col.error(exc.error)
            if exc.error.kind == "not_found":
                result.status = "unresolved"
                return None
        if placements is None and self._enabled("ensembl"):
            try:
                rec, notes = await self._recoder_placements(text, assembly, deadline, None)
                col.warnings.extend(notes)
                placements = [SpdiPlacement(assembly=assembly, contig=p[1], accession=p[0], position=p[2],
                                            deleted=p[3], inserted=p[4]) for p in rec]
                used = "ensembl"
            except SourceFailure as exc:
                col.error(exc.error)
        if placements is None or used is None:
            return None
        col.sources.append(used)
        if not placements:
            col.warnings.append(f"{text} has no {assembly} chromosome placement in {used}; no liftover is applied")
            result.status = "unresolved"
            return None
        if len(placements) == 1:
            p = placements[0]
            raw = RawAllele(assembly=assembly, contig=p.contig, start=p.position, end=p.position + len(p.deleted),
                            ref=p.deleted, alt=p.inserted, origin=used)
            raw.transformations.append(Transformation(operation="rsid_to_allele", source=used,
                                                      detail=f"{text} has one alternate allele on {assembly}",
                                                      after={"spdi": p.spdi}))
            return [raw]
        result.candidates = [
            VariantCandidate(description=f"{text} allele {p.deleted or '-'}>{p.inserted or '-'}", source=used,
                             assembly=assembly, contig=p.contig, spdi=p.spdi, hgvs=[p.hgvs] if p.hgvs else [],
                             vcf=VcfRepresentation(contig=p.contig, pos=p.position + 1, ref=p.deleted, alt=p.inserted)
                             if p.deleted and p.inserted else None,
                             identifiers=[text])
            for p in placements
        ]
        result.status = "ambiguous"
        col.warnings.append(f"{text} is multi-allelic ({len(placements)} alternate alleles); specify the allele")
        return None

    async def _protein_candidates(
        self, text: str, request: NormalizeVariantRequest, deadline: float, col: _Collector,
        result: NormalizeVariantResult,
    ) -> None:
        if not col.network_allowed:
            raise VariantInputError("external resolution is not permitted for this private-file query")
        result.status = "unresolved"
        col.limitations.append(
            "Protein HGVS does not identify a unique genomic allele (codon degeneracy, multiple transcripts); "
            "candidates are listed and none is selected."
        )
        m = HGVS_RE.match(text)
        assert m
        if not self._enabled("ensembl"):
            col.error(self._disabled("ensembl", "variant_recoder"))
            return
        assembly = self._require_assembly(request, col)
        acc = m.group("acc")
        try:
            placements, notes = await self._recoder_placements(text, assembly, deadline, None)
        except SourceFailure as exc:
            col.error(exc.error)
            return
        col.sources.append("ensembl")
        col.warnings.extend(notes)
        if acc.upper().startswith(("NP_", "XP_", "ENSP")) and not accession_has_version(acc):
            col.warnings.append(f"{acc} has no version; candidates may come from any version")
        unique = {(p[1], p[2], p[3], p[4]): p for p in placements}
        result.candidates = [self._placement_candidate(p, assembly, "ensembl") for p in unique.values()]
        if not unique:
            col.warnings.append("Ensembl returned no genomic candidates for this protein change")

    # ---------------------------------------------------------- lookup_variant
    async def lookup_variant(self, request: LookupVariantRequest) -> LookupVariantResult:
        deadline = self._deadline()
        col = _Collector()
        norm = await self._normalize(request, deadline, col)
        norm.query = _query(request)
        n_norm_errors = len(col.errors)
        n_steps, n_warn, n_lim = len(col.transformations), len(col.warnings), len(col.limitations)
        result = LookupVariantResult(tool="lookup_variant", status=norm.status, query=_query(request),
                                     normalization=norm, canonical_variant=norm.canonical_variant)
        variant = norm.canonical_variant
        if variant is not None and not col.network_allowed and request.sources:
            col.error(_egress_forbidden("lookup_variant"))
            result.status = "error"
            self._finish_norm_copy(norm, col, n_norm_errors)
            self._finish(result, col)
            return result
        if variant is None or norm.status in ("error", "ambiguous", "unresolved"):
            if norm.status in ("ok", "partial"):
                result.status = "error"
            col.warnings.append("No source lookups were made because the input did not resolve to one allele.")
            self._finish_norm_copy(norm, col, n_norm_errors)
            self._finish(result, col)
            return result
        evidence = VariantEvidence()
        tasks: dict[str, Awaitable[Any]] = {}
        sources = list(dict.fromkeys(request.sources))
        for source in sources:
            if not self._enabled(source):
                col.error(self._disabled(source, "lookup_variant"))
                continue
            if source == "ensembl":
                tasks[source] = _guard("ensembl", "vep/region", self.ensembl.vep(variant, deadline=deadline), col, timeout=self._remaining(deadline))
            elif source == "clinvar":
                tasks[source] = _guard("clinvar", "lookup", self._clinvar(variant, request.max_clinvar_records, deadline, col), col, timeout=self._remaining(deadline))
            elif source == "gnomad":
                tasks[source] = _guard("gnomad", "variant",
                                       self.gnomad.variant(variant, dataset=request.gnomad_dataset, deadline=deadline), col, timeout=self._remaining(deadline))
            elif source == "alphagenome_atlas":
                tasks[source] = _guard("alphagenome_atlas", "get_dense_variant_scores",
                                       self._atlas_scores(variant, request.atlas_scorers, deadline), col, timeout=self._remaining(deadline))
        results = await asyncio.gather(*tasks.values())
        for source, value in zip(tasks.keys(), results, strict=True):
            if value is None:
                continue
            col.sources.append(source)
            if source == "ensembl":
                evidence.consequence.append(value)
                result.gene_context = _gene_context(value)
            elif source == "clinvar":
                evidence.clinical.extend(value)
            elif source == "gnomad":
                evidence.population.append(value)
            elif source == "alphagenome_atlas":
                evidence.functional_prediction.append(value)
        result.evidence = evidence
        result.status = "partial" if col.errors else "ok"
        if not any((evidence.consequence, evidence.clinical, evidence.population, evidence.functional_prediction)):
            result.status = "error" if col.errors else "ok"
        self._finish_norm_copy(norm, col, n_norm_errors)
        # The normalization trace lives in result.normalization; keep only lookup steps at top level.
        col.transformations = col.transformations[n_steps:]
        col.warnings = col.warnings[n_warn:]
        col.limitations = col.limitations[n_lim:]
        self._finish(result, col)
        return result

    @staticmethod
    def _finish_norm_copy(norm: NormalizeVariantResult, col: _Collector, n_errors: int) -> None:
        norm.errors = list(col.errors[:n_errors])
        norm.transformations = list(col.transformations)
        norm.limitations = list(dict.fromkeys(col.limitations))
        norm.warnings = list(dict.fromkeys(col.warnings))

    async def _atlas_scores(self, variant: CanonicalVariant, scorers: list[str], deadline: float) -> Evidence:
        return await self.atlas().variant_scores(variant, requested_scorers=scorers, deadline=deadline)

    async def _clinvar(self, variant: CanonicalVariant, max_records: int, deadline: float, col: _Collector) -> list[Evidence]:
        match = await self.clinvar.match_variant(variant, deadline=deadline)
        summary = Evidence(
            source="clinvar",
            evidence_type="clinical_allele_match",
            source_url="https://www.ncbi.nlm.nih.gov/clinvar/",
            terms_url=self.clinvar_terms(),
            data={
                "matched_variation_ids": match.matched_ids,
                "other_records_at_position": match.other_records,
            },
            transformations=match.transformations,
            limitations=match.notes + ([] if match.matched_ids else [
                "No ClinVar record matched this exact allele; absence from ClinVar is not evidence of benignity."
            ]),
        )
        if variant.normalization_status != "reference_normalized" and variant.variant_class not in ("SNV", "MNV"):
            summary.limitations.append("Indel was not reference-normalized; ClinVar matching may miss equivalent records.")
        out = [summary]
        ids = match.matched_ids[:max_records]
        if len(match.matched_ids) > max_records:
            summary.truncation.append(Truncation(field="matched records fetched", returned=max_records,
                                                 available=len(match.matched_ids), reason="max_clinvar_records"))
        if ids:
            out.extend(await self.clinvar.evidence_for_ids(ids, deadline=deadline))
        return out

    @staticmethod
    def clinvar_terms() -> str:
        from .sources import SOURCES

        return SOURCES["clinvar"].terms_url

    # ------------------------------------------------------------- lookup_gene
    async def lookup_gene(self, request: LookupGeneRequest) -> LookupGeneResult:
        deadline = self._deadline()
        col = _Collector()
        result = LookupGeneResult(tool="lookup_gene", status="error", query=request.model_dump())
        resolution, ensg, symbol = await self._resolve_gene(request.gene, request.id_type, request.assembly, deadline, col)
        if resolution is not None:
            result.evidence.extend(resolution.evidence)
            result.candidates = resolution.candidates
            if resolution.status == "ambiguous":
                result.status = "ambiguous"
                self._finish(result, col)
                return result
        if ensg is None:
            result.status = "unresolved" if not col.errors else "error"
            self._finish(result, col)
            return result
        record = resolution.record if resolution and resolution.record else {}
        result.gene = {
            "symbol": symbol,
            "hgnc_id": record.get("hgnc_id"),
            "ensembl_gene_id": ensg,
            "entrez_id": record.get("entrez_id"),
            "uniprot_ids": record.get("uniprot_ids"),
            "mane_select": record.get("mane_select"),
            "assembly": request.assembly,
        }
        result.gene = {k: v for k, v in result.gene.items() if v is not None}
        include = set(request.include)
        tasks: dict[str, Awaitable[Any]] = {}
        if "transcripts" in include and self._enabled("ensembl"):
            tasks["ensembl"] = _guard("ensembl", "lookup/id", self._ensembl_gene(ensg, request.assembly, deadline), col, timeout=self._remaining(deadline))
        if "protein" in include and self._enabled("uniprot"):
            tasks["uniprot"] = _guard("uniprot", "entry", self._gene_proteins(record, symbol, deadline, col), col, timeout=self._remaining(deadline))
        if "disease" in include and self._enabled("opentargets"):
            tasks["opentargets"] = _guard("opentargets", "target_associations",
                                          self.opentargets.associations(ensg, size=request.max_associations, deadline=deadline), col, timeout=self._remaining(deadline))
        if "constraint" in include and self._enabled("gnomad"):
            tasks["gnomad"] = _guard("gnomad", "gene_constraint", self.gnomad.constraint(ensg, request.assembly, deadline=deadline), col, timeout=self._remaining(deadline))
        values = await asyncio.gather(*tasks.values())
        for source, value in zip(tasks.keys(), values, strict=True):
            if value is None:
                continue
            col.sources.append(source)
            result.evidence.extend(value if isinstance(value, list) else [value])
        if "identifiers" not in include and resolution is not None:
            result.evidence = [e for e in result.evidence if e.source != "hgnc"]
        result.status = "partial" if col.errors else "ok"
        self._finish(result, col)
        return result

    async def _ensembl_gene(self, ensg: str, assembly: Assembly, deadline: float) -> Evidence:
        record = await self.ensembl.lookup_id(ensg, assembly, expand=True, deadline=deadline)
        return await self.ensembl.entity_evidence(record, assembly, requested_id=ensg, deadline=deadline)

    async def _gene_proteins(self, record: dict[str, Any], symbol: str | None, deadline: float, col: _Collector) -> list[Evidence]:
        ids = [i for i in record.get("uniprot_ids") or [] if isinstance(i, str)]
        if not ids and symbol:
            search = await self.uniprot.search(f"gene_exact:{symbol} AND organism_id:9606 AND reviewed:true", deadline=deadline)
            ids = search.reviewed
            if ids:
                col.warnings.append("HGNC lists no UniProt accession; reviewed UniProt entries found by gene name search")
        if len(ids) > 3:
            col.warnings.append(f"{len(ids)} UniProt accessions linked; only the first 3 entries are returned")
        out = []
        for acc in ids[:3]:
            entry, release, steps = await self.uniprot.entry(acc, deadline=deadline)
            out.append(self.uniprot.entry_evidence(entry, release, steps))
        return out

    async def _resolve_gene(
        self, query: str, id_type: Any, assembly: Assembly, deadline: float, col: _Collector
    ) -> tuple[GeneResolution | None, str | None, str | None]:
        resolution: GeneResolution | None = None
        if self._enabled("hgnc"):
            try:
                resolution = await self.hgnc.resolve(query, id_type=id_type, deadline=deadline)
            except SourceFailure as exc:
                col.error(exc.error)
        else:
            col.error(self._disabled("hgnc", "fetch"))
        if resolution is not None:
            col.sources.append("hgnc")
            for err in resolution.errors:
                col.error(err)
            col.warnings.extend(resolution.warnings)
            col.transformations.extend(resolution.transformations)
            if resolution.status == "resolved" and resolution.record:
                return resolution, resolution.record.get("ensembl_gene_id"), resolution.record.get("symbol")
            if resolution.status == "ambiguous":
                return resolution, None, None
            if resolution.status == "not_found":
                col.warnings.append(f"HGNC has no record for {query!r}")
                return resolution, None, None
        # HGNC unavailable: fall back to exact Ensembl identifiers only.
        kind, value = detect_gene_id_type(query)
        if kind == "ensembl_gene_id":
            col.warnings.append("HGNC unavailable; continuing with the Ensembl gene ID as given")
            return None, value, None
        if kind == "symbol" and self._enabled("ensembl"):
            try:
                rec = await self.ensembl.lookup_symbol(value, assembly, deadline=deadline)
            except SourceFailure as exc:
                col.error(exc.error)
                return None, None, None
            col.warnings.append(
                "HGNC unavailable; symbol matched an Ensembl gene display name. Aliases and previous symbols were not checked."
            )
            col.transformations.append(Transformation(operation="symbol_via_ensembl", source="ensembl",
                                                      detail=f"{value!r} resolved to {rec.get('id')} by Ensembl lookup/symbol"))
            return None, rec.get("id"), rec.get("display_name")
        return None, None, None

    # ---------------------------------------------------------- lookup_protein
    async def lookup_protein(self, request: LookupProteinRequest) -> LookupProteinResult:
        deadline = self._deadline()
        col = _Collector()
        result = LookupProteinResult(tool="lookup_protein", status="error", query=request.model_dump())
        query = request.protein.strip()
        accession: str | None = None
        search_query: str | None = None
        requested_version: tuple[str, int] | None = None
        upper = query.upper()
        if ACCESSION_RE.match(upper) and not upper.isalpha():
            accession = upper
        elif (m := ENSEMBL_ID_RE.match(upper)) and upper.startswith(("ENSP", "ENST")):
            stable, version = split_version(upper)
            if version is not None:
                requested_version = (stable, version)
            search_query = f"xref:ensembl-{stable}"
        elif REFSEQ_PROTEIN_RE.match(upper) or REFSEQ_TX_RE.match(upper):
            if not accession_has_version(upper):
                col.warnings.append(f"{query} has no version; UniProt cross-references are versioned")
            search_query = f"xref:refseq-{upper}"
        else:
            resolution, _, symbol = await self._resolve_gene(query, None, request.assembly, deadline, col)
            if resolution is not None and resolution.status == "ambiguous":
                result.candidates = resolution.candidates
                result.evidence.extend(resolution.evidence)
                result.status = "ambiguous"
                self._finish(result, col)
                return result
            record = resolution.record if resolution and resolution.record else {}
            ids = [i for i in record.get("uniprot_ids") or [] if isinstance(i, str)]
            if len(ids) == 1:
                accession = ids[0]
                col.transformations.append(Transformation(operation="gene_to_protein", source="hgnc",
                                                          detail=f"HGNC links {record.get('symbol')} to UniProt {ids[0]}"))
            elif len(ids) > 1:
                result.candidates = [IdentifierCandidate(entity_type="protein", identifier=i, source="hgnc",
                                                         match_type="hgnc_uniprot_ids") for i in ids]
                result.status = "ambiguous"
                col.warnings.append(f"HGNC links {len(ids)} UniProt accessions; none was selected")
                self._finish(result, col)
                return result
            elif symbol:
                search_query = f"gene_exact:{symbol} AND organism_id:9606"
            else:
                result.status = "unresolved" if not col.errors else "error"
                self._finish(result, col)
                return result
        if accession is None and search_query is not None:
            try:
                search = await self.uniprot.search(search_query, deadline=deadline)
            except SourceFailure as exc:
                col.error(exc.error)
                self._finish(result, col)
                return result
            col.sources.append("uniprot")
            col.warnings.extend(search.notes)
            result.candidates = search.candidates
            if len(search.reviewed) == 1:
                accession = search.reviewed[0]
                col.transformations.append(Transformation(
                    operation="select_reviewed_entry", source="uniprot",
                    detail=f"UniProt query {search_query!r} has exactly one reviewed (Swiss-Prot) entry, {accession}; "
                    "other matches remain in candidates"))
            elif not search.reviewed and len(search.candidates) == 1:
                accession = search.candidates[0].identifier
            else:
                result.status = "ambiguous" if search.candidates else "unresolved"
                if search.candidates:
                    col.warnings.append(f"UniProt query {search_query!r} matched {len(search.candidates)} entries "
                                        f"({len(search.reviewed)} reviewed); none was selected")
                self._finish(result, col)
                return result
        assert accession is not None
        try:
            entry, release, steps = await self.uniprot.entry(accession, deadline=deadline)
        except SourceFailure as exc:
            col.error(exc.error)
            result.status = "unresolved" if exc.error.kind == "not_found" else "error"
            self._finish(result, col)
            return result
        if "uniprot" not in col.sources:
            col.sources.append("uniprot")
        ev = self.uniprot.entry_evidence(entry, release, steps, include_all_features=request.include_all_features)
        if requested_version:
            stable, version = requested_version
            linked = [x for x in _flatten_xrefs(ev.data.get("cross_references", {}).get("Ensembl", []))
                      if x.split(".")[0] == stable]
            if linked and all(x != f"{stable}.{version}" for x in linked):
                ev.limitations.append(f"UniProt links {', '.join(linked)}, not the requested {stable}.{version}.")
        result.evidence.append(ev)
        result.protein = {k: ev.data.get(k) for k in ("accession", "uniprotkb_id", "reviewed", "recommended_name", "genes")}
        result.status = "partial" if col.errors else "ok"
        self._finish(result, col)
        return result

    # ------------------------------------------------------ resolve_identifier
    async def resolve_identifier(self, request: ResolveIdentifierRequest) -> ResolveIdentifierResult:
        deadline = self._deadline()
        col = _Collector()
        text = request.identifier.strip()
        kind = detect_kind(text)
        if kind != "unknown":
            norm = await self._normalize(NormalizeVariantRequest(variant=text, assembly=request.assembly), deadline, col)
            result = ResolveIdentifierResult(tool="resolve_identifier", status=norm.status, query=request.model_dump(),
                                             identifier_type=f"variant:{kind}", variant_candidates=norm.candidates)
            if norm.canonical_variant is not None:
                result.resolved = {"variant": norm.canonical_variant.model_dump(mode="json")}
            elif norm.alleles:
                result.resolved = {"alleles": [a.model_dump(mode="json") for a in norm.alleles]}
            self._finish(result, col)
            return result
        upper = text.upper()
        if (m := VCV_RE.match(upper)) is not None:
            return await self._resolve_vcv(m, request, deadline, col)
        if (ENSEMBL_ID_RE.match(upper) and upper.startswith(("ENST", "ENSP"))):
            return await self._resolve_ensembl_feature(upper, request, deadline, col)
        if REFSEQ_TX_RE.match(upper) or REFSEQ_PROTEIN_RE.match(upper) or re.match(r"^(NM_|NR_)\d+$", upper):
            return await self._resolve_refseq(upper, request, deadline, col)
        id_type = request.id_type
        detected, _ = detect_gene_id_type(text)
        result = ResolveIdentifierResult(tool="resolve_identifier", status="error", query=request.model_dump(),
                                         identifier_type=id_type or detected)
        if (id_type or detected) == "uniprot_ids":
            try:
                entry, release, steps = await self.uniprot.entry(upper, deadline=deadline)
            except SourceFailure as exc:
                col.error(exc.error)
            else:
                col.sources.append("uniprot")
                ev = self.uniprot.entry_evidence(entry, release, steps)
                result.evidence.append(ev)
                result.resolved["uniprot"] = {k: ev.data.get(k) for k in ("accession", "reviewed", "uniprotkb_id", "genes")}
                col.transformations.extend(steps)
        assembly: Assembly = "GRCh38"
        if request.assembly:
            try:
                assembly = normalize_assembly(request.assembly, col.transformations)
            except AssemblyError as exc:
                col.error(SourceError(source="local", operation="parse", kind="invalid_input", message=str(exc)))
        resolution, ensg, symbol = await self._resolve_gene(text, id_type, assembly, deadline, col)
        if resolution is not None:
            result.evidence.extend(resolution.evidence)
            result.candidates = resolution.candidates
            if resolution.status == "ambiguous":
                result.status = "ambiguous"
                self._finish(result, col)
                return result
            if resolution.record:
                rec = resolution.record
                result.resolved["gene"] = {k: rec.get(k) for k in (
                    "hgnc_id", "symbol", "name", "status", "ensembl_gene_id", "entrez_id", "uniprot_ids",
                    "mane_select", "refseq_accession") if rec.get(k) is not None}
                result.resolved["match_type"] = resolution.match_type
        elif ensg:
            result.resolved["gene"] = {"ensembl_gene_id": ensg, "symbol": symbol}
        if result.resolved:
            result.status = "partial" if col.errors else "ok"
        else:
            result.status = "error" if col.errors else "unresolved"
        self._finish(result, col)
        return result

    async def _resolve_vcv(self, m: re.Match[str], request: ResolveIdentifierRequest, deadline: float,
                           col: _Collector) -> ResolveIdentifierResult:
        result = ResolveIdentifierResult(tool="resolve_identifier", status="error", query=request.model_dump(),
                                         identifier_type="clinvar_vcv")
        variation_id = m.group(1)
        try:
            evidence = await self.clinvar.evidence_for_ids([variation_id], deadline=deadline)
        except SourceFailure as exc:
            col.error(exc.error)
            result.status = "unresolved" if exc.error.kind == "not_found" else "error"
            self._finish(result, col)
            return result
        col.sources.append("clinvar")
        record = evidence[0]
        result.evidence = [record]
        if m.group(2) and record.source_record_version and m.group(2) != record.source_record_version:
            col.warnings.append(f"Requested VCV version {m.group(2)}; ClinVar returned current version {record.source_record_version}")
        locations = record.data.get("locations") or []
        if request.assembly:
            try:
                asm = normalize_assembly(request.assembly, col.transformations)
            except AssemblyError as exc:
                col.error(SourceError(source="local", operation="parse", kind="invalid_input", message=str(exc)))
                self._finish(result, col)
                return result
            locations = [loc for loc in locations if loc.get("assembly") == asm]
        result.resolved = {
            "clinvar": {"variation_id": record.data.get("variation_id"), "vcv": record.data.get("vcv"),
                        "name": record.data.get("name"), "canonical_spdi": record.data.get("canonical_spdi")},
            "locations": locations,
        }
        col.limitations.append("Locations are as reported by ClinVar (1-based VCF fields); run normalize_variant on "
                               "a location to get a verified 0-based representation.")
        result.status = "partial" if col.errors else "ok"
        self._finish(result, col)
        return result

    async def _resolve_ensembl_feature(self, identifier: str, request: ResolveIdentifierRequest, deadline: float,
                                       col: _Collector) -> ResolveIdentifierResult:
        result = ResolveIdentifierResult(tool="resolve_identifier", status="error", query=request.model_dump(),
                                         identifier_type="ensembl_transcript" if identifier.startswith("ENST") else "ensembl_protein")
        assembly: Assembly = "GRCh38"
        if request.assembly:
            try:
                assembly = normalize_assembly(request.assembly, col.transformations)
            except AssemblyError as exc:
                col.error(SourceError(source="local", operation="parse", kind="invalid_input", message=str(exc)))
                self._finish(result, col)
                return result
        record = None
        if self._enabled("ensembl"):
            try:
                record = await self.ensembl.lookup_id(identifier, assembly, deadline=deadline)
            except SourceFailure as exc:
                col.error(exc.error)
        if record is not None:
            col.sources.append("ensembl")
            ev = await self.ensembl.entity_evidence(record, assembly, requested_id=identifier, deadline=deadline)
            result.evidence.append(ev)
            result.resolved["ensembl"] = {k: ev.data.get(k) for k in ("object_type", "id", "Parent", "biotype", "assembly")}
            parent = record.get("Parent")
            if identifier.startswith("ENSP") and parent:
                try:
                    tx = await self.ensembl.lookup_id(parent, assembly, deadline=deadline)
                    parent = tx.get("Parent")
                    result.resolved["ensembl"]["transcript"] = tx.get("id")
                except SourceFailure as exc:
                    col.error(exc.error)
                    parent = None
            if parent:
                resolution, _, _ = await self._resolve_gene(parent, "ensembl_gene_id", assembly, deadline, col)
                if resolution and resolution.record:
                    result.resolved["gene"] = {k: resolution.record.get(k) for k in ("hgnc_id", "symbol", "ensembl_gene_id")}
                    result.evidence.extend(resolution.evidence)
        stable, _ = split_version(identifier)
        try:
            search = await self.uniprot.search(f"xref:ensembl-{stable}", deadline=deadline)
            col.sources.append("uniprot")
            result.candidates.extend(search.candidates)
            if search.reviewed:
                result.resolved["uniprot_reviewed"] = search.reviewed
        except SourceFailure as exc:
            col.error(exc.error)
        result.status = ("partial" if col.errors else "ok") if result.resolved else ("error" if col.errors else "unresolved")
        self._finish(result, col)
        return result

    async def _resolve_refseq(self, identifier: str, request: ResolveIdentifierRequest, deadline: float,
                              col: _Collector) -> ResolveIdentifierResult:
        protein = identifier.startswith(("NP_", "XP_"))
        result = ResolveIdentifierResult(tool="resolve_identifier", status="error", query=request.model_dump(),
                                         identifier_type="refseq_protein" if protein else "refseq_transcript")
        if not accession_has_version(identifier):
            col.warnings.append(f"{identifier} has no version")
        tasks: list[Awaitable[Any]] = [
            _guard("uniprot", "search", self.uniprot.search(f"xref:refseq-{identifier}", deadline=deadline), col, timeout=self._remaining(deadline))
        ]
        if not protein:
            unversioned = identifier.split(".")[0]
            if unversioned != identifier:
                col.transformations.append(Transformation(
                    operation="drop_version_for_lookup", source="hgnc",
                    detail="HGNC stores unversioned RefSeq accessions; version dropped for the HGNC lookup only"))
            tasks.append(_guard("hgnc", "fetch/refseq_accession", self.hgnc.fetch("refseq_accession", unversioned, deadline), col, timeout=self._remaining(deadline)))
        values = await asyncio.gather(*tasks)
        search = values[0]
        if search is not None:
            col.sources.append("uniprot")
            result.candidates.extend(search.candidates)
            if search.reviewed:
                result.resolved["uniprot_reviewed"] = search.reviewed
        if not protein and values[1] is not None:
            col.sources.append("hgnc")
            docs = values[1]
            if len(docs) == 1:
                result.resolved["gene"] = {k: docs[0].get(k) for k in ("hgnc_id", "symbol", "ensembl_gene_id", "mane_select")}
                if identifier not in (docs[0].get("mane_select") or []):
                    col.warnings.append("HGNC links this RefSeq accession without version; the gene mapping is not version-specific")
            elif docs:
                result.candidates.extend(HgncClient._candidate(d, "refseq_accession") for d in docs)
        result.status = ("partial" if col.errors else "ok") if result.resolved else (
            "ambiguous" if len(result.candidates) > 1 else ("error" if col.errors else "unresolved"))
        self._finish(result, col)
        return result


def _external_allowed(request: Any) -> bool:
    """Private file-derived queries may only leave the machine with explicit per-call consent."""
    return getattr(request, "query_origin", "user_supplied") != "private_file" or bool(
        getattr(request, "allow_external_queries", False))


def _needs_remote_resolution(variant: Any, kind: str) -> bool:
    if kind in ("rsid", "hgvs_coding", "hgvs_noncoding", "hgvs_protein", "hgvs_other"):
        return True
    if kind == "hgvs_genomic" and isinstance(variant, str):
        m = HGVS_RE.match(variant.strip())
        return bool(m and m.group("acc").upper().startswith(("NG_", "LRG_")))
    return False


def _egress_forbidden(operation: str) -> SourceError:
    return SourceError(
        source="local", operation="egress_check", kind="forbidden",
        message=f"{operation}: variant derived from a private file; set allow_external_queries=true to consent to "
        "sending it to public services for this call. No external request was made.",
    )


def _flatten_xrefs(rows: list[Any]) -> list[str]:
    out = []
    for r in rows:
        if isinstance(r, dict):
            if r.get("ProteinId"):
                out.append(r["ProteinId"])
            if r.get("id"):
                out.append(r["id"])
        elif isinstance(r, str):
            out.append(r)
    return out


def _gene_context(vep: Evidence) -> list[dict[str, Any]]:
    genes: dict[str, dict[str, Any]] = {}
    for c in vep.data.get("transcript_consequences") or []:
        gid = c.get("gene_id")
        if not gid:
            continue
        g = genes.setdefault(gid, {"gene_id": gid, "symbol": c.get("gene_symbol"), "hgnc_id": c.get("hgnc_id"),
                                   "source": "ensembl", "consequences": []})
        for term in c.get("consequence_terms") or []:
            if term not in g["consequences"]:
                g["consequences"].append(term)
    return list(genes.values())


def _query(request: Any) -> dict[str, Any]:
    data = request.model_dump(mode="json")
    ref = data.get("reference")
    if isinstance(ref, dict) and isinstance(ref.get("sequence"), str) and len(ref["sequence"]) > 200:
        ref["sequence"] = f"<{len(ref['sequence'])} bases>"
    return data


def _enforce_size(result: ToolResult) -> None:
    """Drop trailing evidence until the serialized result fits the 1 MiB default."""
    size = len(result.model_dump_json())
    if size <= MAX_RESPONSE_BYTES:
        return
    lists: list[tuple[str, list[Evidence]]] = []
    ev = getattr(result, "evidence", None)
    if isinstance(ev, list):
        lists.append(("evidence", ev))
    elif isinstance(ev, VariantEvidence):
        lists.extend((f"evidence.{k}", getattr(ev, k)) for k in ("clinical", "consequence", "population", "functional_prediction"))
    for name, items in lists:
        original = len(items)
        while items and len(result.model_dump_json()) > MAX_RESPONSE_BYTES:
            items.pop()
        if len(items) != original:
            result.truncation.append(Truncation(field=name, returned=len(items), available=original,
                                                reason="1 MiB response limit"))
        if len(result.model_dump_json()) <= MAX_RESPONSE_BYTES:
            return
