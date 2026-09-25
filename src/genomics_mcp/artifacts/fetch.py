"""fetch_file / get_transfer_status / cancel_transfer handlers and the download runner."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from pathlib import Path
from typing import Any

import httpx

from genomics_mcp.artifacts.transfers import (
    PERSIST_EVERY,
    PREPARE_TIMEOUT_S,
    VERIFIABLE,
    WAIT_FOR_COMPLETION_S,
    Job,
    Part,
    TransferManager,
    artifact_model,
    ctx_budget_error,
    default_name,
    file_checksums,
    safe_name,
    transfer_key,
    verify_checksums,
)
from genomics_mcp.context import OperationContext
from genomics_mcp.contracts import ResolvedFile
from genomics_mcp.errors import (
    BudgetExceededError,
    GenomicsError,
    InvalidInputError,
    UnsupportedError,
    UpstreamError,
)
from genomics_mcp.models import Compression, FileFormat, FileRef, Provenance, ReadinessState
from genomics_mcp.public import Deadline
from genomics_mcp.requests import CancelTransferRequest, FetchFileRequest, TransferStatusRequest
from genomics_mcp.result import OperationOutput
from genomics_mcp.storage.resolved import StorageResolvedFile

COMPONENT = "transfers"
PREPARE_TASK = "genomics_mcp.artifacts.native_tasks:prepare"


class LazyTransfers:
    def __init__(self) -> None:
        self.manager: TransferManager | None = None

    def get(self, ctx: OperationContext) -> TransferManager:
        if self.manager is None:
            self.manager = TransferManager(ctx.settings)
        return self.manager


def _storage(ctx: OperationContext) -> Any:
    return ctx.require_component("storage").get(ctx)


def _tm(ctx: OperationContext) -> TransferManager:
    return ctx.require_component(COMPONENT).get(ctx)


def _strong(etag: str | None) -> bool:
    return bool(etag) and not etag.startswith("W/")


def _output(job: Job, extra_notes: list[str] | None = None) -> OperationOutput:
    model = job.to_model()
    data = {
        "transfer": model,
        "parts": [
            {
                "role": p.role,
                "name": p.name,
                "source": p.display,
                "bytes_done": p.done,
                "bytes_total": p.expected_size,
                "state": p.state,
            }
            for p in job.parts
        ],
        "artifact_file": job.artifact_file,
        "preparation": job.preparation,
        "source_verification": {
            "verified": job.source_verified,
            "checksums": job.source_checksums,
            "note": "checksums of the source bytes as received, before any preparation",
        },
        "notes": [*job.notes, *(extra_notes or [])],
    }
    prov = (
        [model.artifact.provenance]
        if model.artifact
        else [Provenance(source=job.access, url=job.file["uri"], method="bounded transfer")]
    )
    return OperationOutput(data=data, provenance=prov)


def _index_name(data_name: str, index_display: str | None, kind: str | None) -> str:
    base = Path(index_display or "").name
    data_base = data_name
    if base.startswith(data_base) and len(base) > len(data_base):
        return safe_name(data_base + base[len(data_base) :])
    return safe_name(f"{data_base}.{kind or 'idx'}")


async def fetch_file(req: FetchFileRequest, ctx: OperationContext) -> OperationOutput:
    tm = _tm(ctx)
    storage = _storage(ctx)
    key = transfer_key(req.file, req.include_index, req.prepare)
    existing = tm.find(key)
    if existing is not None and existing.state in ("running", "queued"):
        if _verification_compatible(existing, req):
            await tm.wait(existing, min(WAIT_FOR_COMPLETION_S, ctx.deadline.remaining() - 1.0))
            return _output(existing, ["a transfer for this file is already in progress"])
        # The running job was started without this request's checksum requirements: run a
        # separate job so this call is verified. The other job and its artifact are untouched.
        existing = None

    budget = tm.budget_for(req.budget_bytes)
    if _is_ncbi_genome_package(req.file) and req.prepare:
        return await _prepare_ncbi(req, ctx, tm, storage, key, budget)
    backend = ctx.component(f"{BACKEND_PREFIX}{req.file.scheme}")
    urls: dict[str, Any] = {}
    if backend is not None:
        # Schemes that cannot be opened as URLs (EGA) stream through their backend; this
        # manager keeps budget, quota, resume, cancellation and checksum checks.
        desc = await backend.describe(req.file, ctx)
        merged = [*req.file.checksums, *[c for c in desc.checksums if c not in req.file.checksums]]
        req = req.model_copy(update={"file": req.file.model_copy(update={"checksums": merged})})
        md5 = next((c.value for c in desc.checksums if c.algorithm == "md5"), None)
        resolved = StorageResolvedFile(
            file=req.file.model_copy(update={"size_bytes": desc.size_bytes}),
            open_uri=f"{BACKEND_PREFIX}{req.file.scheme}",
            access="https",
            size_bytes=desc.size_bytes,
            etag=f"md5:{md5}" if md5 else None,
            index_state="not_checked",
        )
        bctx = dataclasses.replace(ctx, deadline=Deadline(BACKGROUND_TRANSFER_S))
        urls["data"] = BackendSource(backend, req.file, bctx)
        name_override: str | None = safe_name(desc.suggested_name)
    else:
        resolved = await ctx.resolve_file(req.file)
        name_override = None
    local = resolved.local_path is not None and resolved.open_uri.startswith("/")
    etag = getattr(resolved, "etag", None)
    last_modified = getattr(resolved, "last_modified", None)
    size = getattr(resolved, "size_bytes", None) or resolved.file.size_bytes
    name = name_override or default_name(req.file)

    if existing is not None and existing.state == "completed" and existing.artifact:
        reuse, why = _reusable(tm, existing, req, etag, last_modified, size)
        if reuse:
            return _output(existing, [why])
        reuse_note = why
    else:
        reuse_note = None

    needs_prep = req.prepare and resolved.readiness.state in (
        ReadinessState.NOT_LOCUS_READY,
        ReadinessState.INDEX_REQUIRED,
    )
    parts: list[Part] = []
    local_notes: list[str] = []
    if local:
        if needs_prep:
            parts.append(
                Part(
                    "data",
                    name,
                    req.file.display_uri(),
                    size,
                    local_source=str(resolved.local_path),
                )
            )
        else:
            local_notes.append("the file is already local; it was not copied")
    else:
        parts.append(Part("data", name, req.file.display_uri(), size))
        urls.setdefault("data", resolved.open_uri)
        if req.include_index and resolved.index_open_uri and not needs_prep:
            shown = getattr(resolved, "index_display", None) or (req.file.index_uri or "")
            parts.append(
                Part(
                    "index",
                    _index_name(name, shown, getattr(resolved, "index_kind", None)),
                    FileRef(uri=shown).display_uri() if shown else "index",
                )
            )
            urls["index"] = resolved.index_open_uri
        comp = getattr(resolved, "companion_open_uris", {}) or {}
        if req.include_index and "gzi" in comp and not needs_prep:
            parts.append(Part("gzi", safe_name(name + ".gzi"), "gzi"))
            urls["gzi"] = comp["gzi"]

    known = sum(p.expected_size or 0 for p in parts)
    if size is not None and parts and size > budget:
        raise ctx_budget_error(
            size,
            budget,
            explicit=req.budget_bytes is not None,
            ceiling=ctx.settings.limits.transfer_budget_ceiling_bytes,
        )

    job: Job | None = None
    notes: list[str] = []
    if (
        existing is not None
        and existing.state == "failed"
        and not existing.no_resume
        and parts
        and (existing.etag is not None or existing.last_modified is not None)
        and (existing.etag, existing.last_modified, existing.size) == (etag, last_modified, size)
    ):
        job = existing
        job.parts = parts
        for p in parts:
            p.done = tm.part_size(job, p)
        job.budget_bytes = budget
        notes.append("resuming an interrupted transfer")
    elif existing is not None and existing.state in ("failed", "completed"):
        notes.append(reuse_note or "previous attempt is not resumable; starting again")

    needed = ((known or budget) - sum(p.done for p in parts)) if parts else 0
    tm.check_quota(needed, exclude=job.transfer_id if job else None)
    if job is None:
        job = tm.new_job(
            key,
            req.file,
            budget,
            include_index=req.include_index,
            prepare=req.prepare,
            verify=req.verify_checksum,
            parts=parts,
            access=getattr(resolved, "access", req.file.scheme),
        )
    job.etag, job.last_modified, job.size = etag, last_modified, size
    job.notes.extend(notes + local_notes)
    trusted = tuple(h for h in [getattr(resolved, "endpoint_trusted_host", None)] if h)

    async def runner(job: Job, cancel: asyncio.Event) -> None:
        await _run_job(tm, storage, ctx.settings, job, req, resolved, urls, trusted, cancel)

    tm.start(job, runner)
    await tm.wait(job, min(WAIT_FOR_COMPLETION_S, ctx.deadline.remaining() - 1.0))
    return _output(job)


def _checksum_set(checksums: Any) -> set[tuple[str, str]]:
    out = set()
    for c in checksums or []:
        alg = c["algorithm"] if isinstance(c, dict) else c.algorithm
        val = c["value"] if isinstance(c, dict) else c.value
        if alg in VERIFIABLE:
            out.add((alg, val.strip().lower()))
    return out


def _verification_compatible(job: Job, req: FetchFileRequest) -> bool:
    """May this request share a job that is still running? Only if every checksum it wants
    verified is one the running job will verify."""
    wanted = _checksum_set(req.file.checksums) if req.verify_checksum else set()
    if not wanted:
        return True
    return job.verify_checksum and wanted <= _checksum_set(job.file.get("checksums"))


def _reusable(
    tm: TransferManager,
    job: Job,
    req: FetchFileRequest,
    etag: str | None,
    last_modified: str | None,
    size: int | None,
) -> tuple[bool, str]:
    """A completed artifact is reused only if source and artifact are provably unchanged and
    the current request's verification is satisfied by checksums of the source bytes."""
    if etag is None and last_modified is None:
        return False, "the source has no validator (ETag/Last-Modified); fetched again"
    if (job.etag, job.last_modified, job.size) != (etag, last_modified, size):
        return False, "the source changed since the last fetch; fetched again"
    if not tm.artifact_unchanged(job):
        return False, "the previous artifact is missing or was modified; fetched again"
    if req.verify_checksum and req.file.checksums:
        wanted = [c for c in req.file.checksums if c.algorithm in VERIFIABLE]
        for c in wanted:
            got = job.source_checksums.get(c.algorithm)
            if got is None or got.lower() != c.value.strip().lower():
                return (
                    False,
                    "requested checksum is not satisfied by the previous fetch; "
                    "fetched and verified again",
                )
        if wanted:
            job.source_verified = True
            tm._persist(job)
    return True, "already fetched; source and artifact identity unchanged"


async def _run_job(
    tm: TransferManager,
    storage: Any,
    settings: Any,
    job: Job,
    req: FetchFileRequest,
    resolved: ResolvedFile,
    urls: dict[str, Any],
    trusted: tuple[str, ...],
    cancel: asyncio.Event,
) -> None:
    transformations: list[str] = []
    for part in job.parts:
        if part.state == "done":
            continue
        part.state = "running"
        if part.local_source:
            await _copy_local(tm, job, part, cancel)
            transformations.append("copied local source into the work dir (source unchanged)")
        elif isinstance(urls[part.role], BackendSource):
            await _download_backend(tm, job, part, urls[part.role], cancel)
        else:
            await _download(tm, storage, job, part, urls[part.role], trusted, cancel)
        if cancel.is_set():
            return
    # Finalize data first; checksums over the complete file only.
    data_part = next((p for p in job.parts if p.role == "data"), None)
    if data_part is not None:
        data_path = tm.finalize(job, data_part)
    else:
        data_path = resolved.local_path
    assert data_path is not None
    expected = {c.algorithm for c in req.file.checksums} if req.verify_checksum else set()
    computed = await asyncio.to_thread(
        file_checksums, Path(data_path), expected & {"md5", "sha1", "sha256", "sha512"}
    )
    job.source_checksums = computed
    verified = False
    if req.verify_checksum:
        verified, cnotes = verify_checksums(req.file, computed)
        job.notes.extend(cnotes)
        if not req.file.checksums:
            job.notes.append("no expected checksum was given; computed md5/sha256 are reported")
    job.source_verified = verified if req.file.checksums and req.verify_checksum else None
    index_path: Path | None = None
    for part in job.parts:
        if part.role in ("index", "gzi") and part.state != "done":
            p = tm.finalize(job, part)
            if part.role == "index":
                index_path = p
    if data_part is None and resolved.index_open_uri and resolved.index_open_uri.startswith("/"):
        index_path = Path(resolved.index_open_uri)
    fmt = req.file.effective_format()
    final_path = Path(data_path)
    transformed = False
    if job.prepare and data_part is not None:
        described = await storage.describe_local(FileRef(uri=str(final_path), format=fmt))
        state = described.readiness.state
        if state in (ReadinessState.NOT_LOCUS_READY, ReadinessState.INDEX_REQUIRED) and fmt:
            comp = getattr(described, "compression", None) or Compression.NONE
            growth, file_cap = tm.preparation_allowance(job)
            result = await storage.native.run(
                PREPARE_TASK,
                {
                    "path": str(final_path),
                    "format": fmt.value,
                    "compression": comp.value,
                    "work_dir": str(settings.paths.work_dir),
                    "max_growth_bytes": growth,
                    "max_file_bytes": file_cap,
                },
                timeout_s=PREPARE_TIMEOUT_S,
                what="preparation",
            )
            job.preparation = result
            if result.get("prepared"):
                final_path = Path(result["path"])
                index_path = Path(result["index"]) if result.get("index") else index_path
                transformations.extend(result.get("steps", []))
                transformed = any("compress" in step for step in result.get("steps", []))
        else:
            job.preparation = {"prepared": False, "reason": "already ready for region queries"}
    tm.check_quota(0, exclude=job.transfer_id)
    # Checksums describe the returned artifact bytes, recomputed after every transformation.
    final_sums = (
        computed if not transformed else await asyncio.to_thread(file_checksums, final_path, set())
    )
    if transformed:
        job.notes.append(
            "the artifact was transformed; its checksums differ from the source checksums "
            "(see source_verification)"
        )
    method = (
        "local file (not copied)"
        if data_part is None
        else ("staged copy" if data_part.local_source else "HTTP GET (bounded, resumable)")
    )
    artifact = artifact_model(
        job,
        final_path,
        index_path,
        final_sums,
        verified and not transformed,
        fmt,
        method,
        transformations,
    )
    job.artifact = artifact.model_dump(mode="json")
    origin = req.file
    art_ref = FileRef(
        uri=str(final_path),
        index_uri=str(index_path) if index_path else None,
        format=fmt,
        assembly=origin.assembly,
        source=origin.source,
        accession=origin.accession,
        visibility=origin.visibility,
        access_status=origin.access_status,
        checksums=artifact.checksums,
    )
    described = await storage.describe_local(art_ref)
    job.artifact_file = described.file.model_dump(mode="json")
    job.artifact_identity = _identity(final_path)
    job.state = "completed"
    job.error = None
    tm._persist(job)


async def _copy_local(tm: TransferManager, job: Job, part: Part, cancel: asyncio.Event) -> None:
    src = Path(part.local_source or "")
    size = _size(src)
    if size > job.budget_bytes:
        raise BudgetExceededError(f"staging copy needs {size} bytes; budget is {job.budget_bytes}")
    dest = tm._part_path(job, part)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def copy() -> None:
        with src.open("rb") as fin, dest.open("wb") as fout:
            while chunk := fin.read(1 << 20):
                if cancel.is_set():
                    return
                fout.write(chunk)
                part.done += len(chunk)

    part.done = 0
    await asyncio.to_thread(copy)
    part.expected_size = size
    if not cancel.is_set() and part.done != size:
        raise UpstreamError("the local source changed size while it was copied")


async def _download(
    tm: TransferManager,
    storage: Any,
    job: Job,
    part: Part,
    url: str,
    trusted: tuple[str, ...],
    cancel: asyncio.Event,
) -> None:
    from genomics_mcp.storage.http import parse_content_range, status_error

    path = tm._part_path(job, part)
    path.parent.mkdir(parents=True, exist_ok=True)
    source = job.access
    for _attempt in range(4):
        offset = path.stat().st_size if path.exists() else 0
        if part.expected_size is not None and offset > part.expected_size:
            path.write_bytes(b"")
            offset = 0
        part.done = offset
        if part.expected_size is not None and offset == part.expected_size and offset > 0:
            return
        headers: dict[str, str] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = job.etag if _strong(job.etag) else job.last_modified
            if part.role == "data" and validator:
                headers["If-Range"] = validator
        restart = False
        async with storage.http.open(
            url, source=source, headers=headers, extra_trusted=trusted
        ) as (
            resp,
            _final,
            _,
        ):
            status = resp.status_code
            if status == 416 and offset:
                restart = True
            elif status >= 400:
                raise status_error(source, status, "")
            else:
                got_etag = resp.headers.get("etag")
                if part.role == "data" and job.etag and got_etag and got_etag != job.etag:
                    job.notes.append("the object changed on the server; restarted from zero")
                    job.etag = got_etag
                    restart = status == 206
                    if status == 200:
                        offset = 0
                if not restart and status == 206:
                    cr = parse_content_range(resp.headers.get("content-range"))
                    if (
                        cr is None
                        or cr[0] != offset
                        or (
                            part.expected_size is not None
                            and cr[2] not in (None, part.expected_size)
                        )
                    ):
                        job.notes.append("Content-Range did not match the partial file; restarted")
                        restart = True
                    elif part.expected_size is None and cr[2] is not None:
                        part.expected_size = cr[2]
                elif not restart and status == 200:
                    if offset:
                        job.notes.append(
                            "server returned the whole object to a resume request; restarted "
                            "from zero"
                        )
                        offset = 0
                    cl = resp.headers.get("content-length")
                    if part.expected_size is None and cl and cl.isdigit():
                        part.expected_size = int(cl)
                if not restart:
                    await _stream(tm, job, part, resp, path, offset, cancel)
                    if cancel.is_set():
                        return
                    if part.expected_size is not None and part.done != part.expected_size:
                        raise UpstreamError(
                            f"transfer ended early ({part.done} of {part.expected_size} bytes); "
                            "call fetch_file again to resume",
                            retryable=True,
                        )
                    if part.expected_size is None:
                        part.expected_size = part.done  # size undeclared by the server
                    return
        job.restarts += 1
        path.write_bytes(b"")
        part.done = 0
        tm._persist(job)
    raise UpstreamError("transfer could not be completed consistently after restarts")


async def _stream(
    tm: TransferManager,
    job: Job,
    part: Part,
    resp: Any,
    path: Path,
    offset: int,
    cancel: asyncio.Event,
) -> None:
    other = sum(p.done for p in job.parts if p is not part)
    since = 0
    with path.open("ab" if offset else "wb") as fh:
        part.done = offset
        try:
            async for chunk in resp.aiter_raw():
                if cancel.is_set():
                    return
                n = len(chunk)
                if part.expected_size is not None and part.done + n > part.expected_size:
                    raise UpstreamError(
                        "server sent more bytes than the object size", retryable=False
                    )
                if other + part.done + n > job.budget_bytes:
                    fh.close()
                    _unlink(path)
                    raise BudgetExceededError(
                        f"transfer exceeded its budget of {job.budget_bytes} bytes",
                        hint="pass a larger budget_bytes (up to the configured ceiling)",
                    )
                job.network_bytes += n
                if job.network_bytes > 2 * job.budget_bytes:
                    raise BudgetExceededError("transfer restarts exceeded twice the byte budget")
                fh.write(chunk)
                part.done += n
                since += n
                if since >= PERSIST_EVERY:
                    fh.flush()
                    tm._persist(job)
                    since = 0
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            fh.flush()
            raise UpstreamError(
                f"the connection was interrupted after {part.done} bytes ({type(exc).__name__}); "
                "call fetch_file again with the same file to resume",
                retryable=True,
            ) from None


BACKEND_PREFIX = "transfer_backend:"
BACKGROUND_TRANSFER_S = 6 * 3600.0
BACKEND_CHUNK_BYTES = 16 * 1024 * 1024
BACKEND_ALIGN_BYTES = 64 * 1024
"""Deadline for background backend streams (they outlive the interactive call)."""
NCBI_PREPARER = "preparer:ncbi_genome_fasta"


@dataclasses.dataclass
class BackendSource:
    backend: Any
    file: FileRef
    ctx: OperationContext


class _Body:
    def __init__(self, body: Any) -> None:
        self.body = body

    def aiter_raw(self) -> Any:
        return self.body


async def _download_backend(
    tm: TransferManager, job: Job, part: Part, src: BackendSource, cancel: asyncio.Event
) -> None:
    """Stream [offset, size) from a transfer backend into the part (resumable by offset)."""
    path = tm._part_path(job, part)
    path.parent.mkdir(parents=True, exist_ok=True)
    offset = tm.part_size(job, part)
    if part.expected_size is not None and offset > part.expected_size:
        offset = 0
    # Ranges start only at aligned offsets: EGA's decrypted plain stream returns wrong bytes
    # for ranges starting inside a cipher block (observed live). Resume from an aligned point.
    aligned = offset - offset % BACKEND_ALIGN_BYTES
    if aligned != offset or not path.exists():
        _truncate(path, aligned)
        offset = aligned
    part.done = offset
    if part.expected_size is not None and offset == part.expected_size:
        return
    size = part.expected_size
    if size is None:
        raise UpstreamError("the transfer backend did not report a size", retryable=False)
    # Bounded ranged chunks. A range spanning the whole object is never requested: EGA
    # answers such a request with 200, which a strict range client must refuse.
    while part.done < size and not cancel.is_set():
        start = part.done
        end = min(start + BACKEND_CHUNK_BYTES, size)
        if start == 0 and end == size and size > BACKEND_ALIGN_BYTES:
            end = (size // 2) - (size // 2) % BACKEND_ALIGN_BYTES
        async with src.backend.open(src.file, src.ctx, start=start, end=end) as body:
            before = part.done
            await _stream(tm, job, part, _Body(body), path, start, cancel)
        if part.done != end and not cancel.is_set():
            raise UpstreamError(
                f"backend returned {part.done - before} bytes for a {end - start} byte range; "
                "call fetch_file again to resume",
                retryable=True,
            )
    if not cancel.is_set() and part.expected_size is not None and part.done != part.expected_size:
        raise UpstreamError(
            f"transfer ended early ({part.done} of {part.expected_size} bytes); "
            "call fetch_file again to resume",
            retryable=True,
        )


def _is_ncbi_genome_package(file: FileRef) -> bool:
    native = file.native or {}
    return file.source == "ncbi_datasets" and (
        native.get("annotation_type") == "GENOME_FASTA"
        or "include_annotation_type=GENOME_FASTA" in file.uri
    )


async def _prepare_ncbi(
    req: FetchFileRequest,
    ctx: OperationContext,
    tm: TransferManager,
    storage: Any,
    key: str,
    budget: int,
) -> OperationOutput:
    """Explicit NCBI genome preparation (ZIP -> verified FASTA + .fai) under E3 accounting."""
    preparer = ctx.component(NCBI_PREPARER)
    if preparer is None:
        raise UnsupportedError("NCBI genome preparation is not available in this build")
    if not req.file.accession:
        raise InvalidInputError("the NCBI package FileRef has no assembly accession")
    tm.check_quota(budget)
    # A placeholder part with unknown size makes the job reserve its whole budget while it runs.
    package = Part("package", safe_name(f"{req.file.accession}.zip"), req.file.display_uri())
    job = tm.new_job(
        key,
        req.file,
        budget,
        include_index=True,
        prepare=True,
        verify=req.verify_checksum,
        parts=[package],
        access="https",
    )
    # Runs as a managed background job (status/cancel work; it outlives a timed-out call)
    # with its own deadline; the preparer bounds bytes by `budget` and cleans up on failure.
    bctx = dataclasses.replace(ctx, deadline=Deadline(PREPARE_TIMEOUT_S))

    async def runner(job: Job, cancel: asyncio.Event) -> None:
        # Job-owned output directory: failure/cancel cleanup removes only this job's files,
        # never an earlier completed artifact or a concurrent request's outputs.
        owned = tm.art_root / job.transfer_id
        art = await preparer.prepare(
            req.file.accession, bctx, budget_bytes=job.budget_bytes, workspace=owned
        )
        outputs = [Path(f) for f in (art.path, art.index_path) if f]
        if cancel.is_set():
            for f in outputs:
                _unlink(f)
            return
        try:
            tm.check_quota(0, exclude=job.transfer_id)
        except GenomicsError:
            for f in outputs:
                _unlink(f)
            raise
        described = await storage.describe_local(
            FileRef(
                uri=art.path,
                index_uri=art.index_path,
                format=FileFormat.FASTA,
                assembly=req.file.assembly,
                source=req.file.source,
                accession=req.file.accession,
                visibility=req.file.visibility,
            )
        )
        job.artifact = art.model_dump(mode="json")
        job.artifact_file = described.file.model_dump(mode="json")
        job.preparation = {
            "prepared": True,
            "steps": [
                "NCBI Datasets genome package downloaded, member MD5s verified, FASTA "
                "extracted, .fai built (preparer:ncbi_genome_fasta)"
            ],
        }
        job.source_verified = art.checksum_verified
        package.state = "done"
        package.done = art.size_bytes
        package.expected_size = art.size_bytes
        job.artifact_identity = _identity(Path(art.path))
        if cancel.is_set():
            return
        job.state = "completed"
        job.error = None
        tm._persist(job)

    tm.start(job, runner)
    await tm.wait(job, min(WAIT_FOR_COMPLETION_S * 4, ctx.deadline.remaining() - 1.0))
    return _output(job)


def _truncate(path: Path, size: int) -> None:
    with path.open("ab") as fh:
        fh.truncate(size)


def _identity(path: Path) -> dict[str, int]:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _size(path: Path) -> int:
    return path.stat().st_size


async def get_transfer_status(req: TransferStatusRequest, ctx: OperationContext) -> OperationOutput:
    return _output(_tm(ctx).get(req.transfer_id))


async def cancel_transfer(req: CancelTransferRequest, ctx: OperationContext) -> OperationOutput:
    return _output(await _tm(ctx).cancel(req.transfer_id))
