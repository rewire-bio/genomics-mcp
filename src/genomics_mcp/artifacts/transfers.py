"""Bounded, resumable file transfers into the work dir.

Layout under `paths.work_dir`:
    transfers/<id>/job.json     sanitized job state (no signed URLs, no credentials)
    transfers/<id>/<part>.part  bytes being downloaded
    artifacts/<id>/<name>       finalized files (atomic rename from .part)

Rules:
- Budget: default `limits.max_transfer_bytes`; a larger explicit `budget_bytes` is accepted
  up to `limits.transfer_budget_ceiling_bytes`. Data and index bytes share one budget.
- Quota: bytes already in the work dir plus bytes reserved by active transfers may not exceed
  `limits.workspace_max_bytes`.
- Resume: a job keeps the object's strong identity (size, ETag, Last-Modified). A resumed GET
  sends `Range: bytes=<done>-` with `If-Range`. A 200 reply, a changed identity or a
  Content-Range that does not start at <done> truncates the part and restarts from zero
  (within 2x the budget of network bytes). Jobs interrupted by a restart are reported as
  failed/resumable and continue when fetch_file is called again with the same file.
- Checksums: computed over the complete file only, and compared with the checksums given on
  the FileRef. A mismatch fails the job and removes the bytes.
- Cancel: stops the background task and removes partial bytes; the job never reads completed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from genomics_mcp.config import Settings
from genomics_mcp.errors import (
    BudgetExceededError,
    ErrorCode,
    ErrorInfo,
    GenomicsError,
    InvalidInputError,
    NotFoundError,
    UpstreamError,
    redact,
)
from genomics_mcp.models import (
    Checksum,
    FileFormat,
    FileRef,
    LocalArtifact,
    Provenance,
    TransferJob,
    TransferState,
    utcnow,
)

log = logging.getLogger("genomics_mcp.artifacts.transfers")

CHUNK = 1 << 20
PERSIST_EVERY = 4 << 20
MAX_CONCURRENT = 2
PREPARE_TIMEOUT_S = 900.0
WAIT_FOR_COMPLETION_S = 5.0
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
VERIFIABLE = {"md5", "sha1", "sha256", "sha512"}


def safe_name(name: str, fallback: str = "file") -> str:
    """A filename with no path separators, no leading dots and bounded length."""
    name = _SAFE.sub("_", name.replace("/", "_").replace("\\", "_")).lstrip(".")
    name = name[-150:] if len(name) > 150 else name
    return name or fallback


def transfer_key(file: FileRef, include_index: bool, prepare: bool) -> str:
    raw = json.dumps(
        [
            file.uri,
            file.index_uri,
            file.storage_profile,
            file.source,
            file.accession,
            include_index,
            prepare,
        ],
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def dir_usage(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


@dataclass
class Part:
    role: str  # data, index, gzi
    name: str  # final file name in the artifact dir
    display: str
    expected_size: int | None = None
    done: int = 0
    state: str = "pending"  # pending, running, done, failed
    local_source: str | None = None


@dataclass
class Job:
    transfer_id: str
    key: str
    state: str
    file: dict[str, Any]
    budget_bytes: int
    include_index: bool
    prepare: bool
    verify_checksum: bool
    parts: list[Part]
    etag: str | None = None
    last_modified: str | None = None
    size: int | None = None
    network_bytes: int = 0
    restarts: int = 0
    notes: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    artifact: dict[str, Any] | None = None
    artifact_file: dict[str, Any] | None = None
    preparation: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())
    access: str = "https"
    no_resume: bool = False
    source_checksums: dict[str, str] = field(default_factory=dict)
    """Checksums of the complete source bytes as received (before any preparation)."""
    source_verified: bool | None = None
    artifact_identity: dict[str, int] | None = None

    @property
    def bytes_done(self) -> int:
        return sum(p.done for p in self.parts)

    @property
    def bytes_total(self) -> int | None:
        sizes = [p.expected_size for p in self.parts]
        return None if any(s is None for s in sizes) else sum(s for s in sizes if s is not None)

    def to_model(self) -> TransferJob:
        err = ErrorInfo.model_validate(self.error) if self.error else None
        return TransferJob(
            transfer_id=self.transfer_id,
            state=TransferState(self.state),
            file=FileRef.model_validate(self.file),
            budget_bytes=self.budget_bytes,
            bytes_done=self.bytes_done,
            bytes_total=self.bytes_total,
            resumable=self.state in ("failed", "running", "queued")
            and not self.no_resume
            and (self.etag is not None or self.last_modified is not None)
            and any(p.done for p in self.parts),
            artifact=LocalArtifact.model_validate(self.artifact) if self.artifact else None,
            error=err,
            updated_at=self.updated_at,
        )

    @classmethod
    def load(cls, data: dict[str, Any]) -> Job:
        parts = [Part(**p) for p in data.pop("parts")]
        return cls(parts=parts, **data)


class TransferManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.paths.work_dir / "transfers"
        self.art_root = settings.paths.work_dir / "artifacts"
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._loaded = False

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.root.exists():
            return
        for jf in self.root.glob("*/job.json"):
            try:
                job = Job.load(json.loads(jf.read_text()))
            except (OSError, ValueError, TypeError) as exc:
                log.warning("skipping unreadable transfer record %s: %s", jf.parent.name, exc)
                continue
            if job.state in ("running", "queued"):
                job.state = "failed"
                job.error = ErrorInfo(
                    code=ErrorCode.UPSTREAM_ERROR,
                    message="transfer was interrupted (server stopped); call fetch_file again "
                    "with the same file to resume",
                    retryable=True,
                ).model_dump(mode="json")
                self._persist(job)
            self.jobs[job.transfer_id] = job

    def _persist(self, job: Job) -> None:
        job.updated_at = utcnow().isoformat()
        d = self.root / job.transfer_id
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(asdict(job), default=str))
        os.replace(tmp, d / "job.json")

    def _part_path(self, job: Job, part: Part) -> Path:
        return self.root / job.transfer_id / f"{part.role}.part"

    def _artifact_dir(self, job: Job) -> Path:
        return self.art_root / job.transfer_id

    # ------------------------------------------------------------------ queries
    def get(self, transfer_id: str) -> Job:
        self._load()
        if not re.fullmatch(r"[0-9a-f]{32}", transfer_id) or transfer_id not in self.jobs:
            raise NotFoundError("no such transfer", details={"transfer_id": transfer_id[:64]})
        return self.jobs[transfer_id]

    def find(self, key: str) -> Job | None:
        self._load()
        matches = [j for j in self.jobs.values() if j.key == key]
        return max(matches, key=lambda j: j.created_at) if matches else None

    def reserved_bytes(self, exclude: str | None = None) -> int:
        total = 0
        for j in self.jobs.values():
            if j.transfer_id == exclude or j.state not in ("running", "queued"):
                continue
            expected = j.bytes_total if j.bytes_total is not None else j.budget_bytes
            total += max(0, expected - j.bytes_done)
        return total

    def check_quota(self, needed: int, exclude: str | None = None) -> None:
        limit = self.settings.limits.workspace_max_bytes
        used = dir_usage(self.settings.paths.work_dir)
        reserved = self.reserved_bytes(exclude)
        if used + reserved + needed > limit:
            raise BudgetExceededError(
                "the work dir quota would be exceeded",
                hint="remove old artifacts from the work dir or raise limits.workspace_max_bytes",
                details={
                    "needed_bytes": needed,
                    "used_bytes": used,
                    "reserved_bytes": reserved,
                    "workspace_max_bytes": limit,
                },
            )

    def preparation_allowance(self, job: Job) -> tuple[int, int]:
        """(max net bytes preparation may add, max size of any single file it writes).

        Preparation outputs (BGZF copies, indexes, temporaries) count against both the job's
        byte budget and the work dir quota."""
        limit = self.settings.limits.workspace_max_bytes
        quota_free = (
            limit - dir_usage(self.settings.paths.work_dir) - self.reserved_bytes(job.transfer_id)
        )
        growth = min(quota_free, job.budget_bytes - job.bytes_done)
        if growth <= 0:
            raise BudgetExceededError(
                "no budget or work dir quota left for preparation outputs",
                hint="pass a larger budget_bytes or free space in the work dir",
                details={"workspace_max_bytes": limit, "budget_bytes": job.budget_bytes},
            )
        return growth, max(1, quota_free)

    def budget_for(self, requested: int | None) -> int:
        limits = self.settings.limits
        if requested is None:
            return limits.max_transfer_bytes
        if requested > limits.transfer_budget_ceiling_bytes:
            raise BudgetExceededError(
                f"budget_bytes {requested} exceeds the configured ceiling "
                f"{limits.transfer_budget_ceiling_bytes}",
                details={"ceiling_bytes": limits.transfer_budget_ceiling_bytes},
            )
        return requested

    # ------------------------------------------------------------------ lifecycle
    def new_job(
        self,
        key: str,
        file: FileRef,
        budget: int,
        *,
        include_index: bool,
        prepare: bool,
        verify: bool,
        parts: list[Part],
        access: str,
    ) -> Job:
        job = Job(
            transfer_id=uuid.uuid4().hex,
            key=key,
            state="queued",
            file=sanitized(file).model_dump(mode="json"),
            budget_bytes=budget,
            include_index=include_index,
            prepare=prepare,
            verify_checksum=verify,
            parts=parts,
            access=access,
        )
        self.jobs[job.transfer_id] = job
        self._persist(job)
        return job

    def start(self, job: Job, runner: Any) -> None:
        event = asyncio.Event()
        self.cancel_events[job.transfer_id] = event
        job.state = "queued"
        job.error = None
        self._persist(job)

        async def run() -> None:
            async with self._sem:
                if event.is_set():
                    return
                job.state = "running"
                self._persist(job)
                try:
                    await runner(job, event)
                except asyncio.CancelledError:
                    if job.state != "cancelled":
                        job.state = "failed"
                        job.error = ErrorInfo(
                            code=ErrorCode.UPSTREAM_ERROR,
                            message="transfer was interrupted; call fetch_file again to resume",
                            retryable=True,
                        ).model_dump(mode="json")
                        self._persist(job)
                    raise
                except GenomicsError as exc:
                    job.state = "failed"
                    job.error = exc.info.model_dump(mode="json")
                    if not exc.info.retryable:
                        # Integrity, budget and access failures restart from zero next time.
                        job.no_resume = True
                        self.discard_parts(job)
                    self.discard_artifacts(job)
                    self._persist(job)
                except Exception as exc:  # noqa: BLE001 - recorded on the job, not hidden
                    log.error("transfer %s failed: %s", job.transfer_id, redact(repr(exc)))
                    job.state = "failed"
                    job.error = ErrorInfo(
                        code=ErrorCode.INTERNAL_ERROR, message="transfer failed unexpectedly"
                    ).model_dump(mode="json")
                    job.no_resume = True
                    self.discard_parts(job)
                    self.discard_artifacts(job)
                    self._persist(job)

        self.tasks[job.transfer_id] = asyncio.create_task(run(), name=f"transfer-{job.transfer_id}")

    async def wait(self, job: Job, seconds: float) -> None:
        task = self.tasks.get(job.transfer_id)
        if task is not None and seconds > 0:
            await asyncio.wait({task}, timeout=seconds)

    async def cancel(self, transfer_id: str) -> Job:
        job = self.get(transfer_id)
        if job.state in ("completed", "cancelled"):
            return job
        event = self.cancel_events.get(transfer_id)
        if event is not None:
            event.set()
        task = self.tasks.get(transfer_id)
        job.state = "cancelled"
        job.error = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5)
        self.discard_parts(job)
        self.discard_artifacts(job)
        job.artifact = None
        job.artifact_file = None
        job.notes.append("cancelled; partial and staged bytes removed")
        self._persist(job)
        return job

    async def shutdown(self) -> None:
        for tid, task in list(self.tasks.items()):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(task, timeout=5)
            job = self.jobs.get(tid)
            if job is not None and job.state in ("running", "queued"):
                job.state = "failed"
                job.error = ErrorInfo(
                    code=ErrorCode.UPSTREAM_ERROR,
                    message="transfer was interrupted (server stopped); call fetch_file again "
                    "with the same file to resume",
                    retryable=True,
                ).model_dump(mode="json")
                self._persist(job)

    # ------------------------------------------------------------------ helpers
    def discard_parts(self, job: Job) -> None:
        for part in job.parts:
            with contextlib.suppress(OSError):
                self._part_path(job, part).unlink()
            part.done = 0
            part.state = "pending"

    def discard_artifacts(self, job: Job) -> None:
        """Remove this job's own artifact directory (never a source file). Only for jobs that
        did not complete; a completed job's artifacts belong to that job."""
        if job.state != "completed":
            shutil.rmtree(self._artifact_dir(job), ignore_errors=True)

    def artifact_unchanged(self, job: Job) -> bool:
        if not job.artifact or not job.artifact_identity:
            return False
        try:
            st = Path(job.artifact["path"]).stat()
        except OSError:
            return False
        return job.artifact_identity == {"size": st.st_size, "mtime_ns": st.st_mtime_ns}

    def part_size(self, job: Job, part: Part) -> int:
        try:
            return self._part_path(job, part).stat().st_size
        except OSError:
            return 0

    def artifact_exists(self, job: Job) -> bool:
        return bool(job.artifact) and Path(job.artifact["path"]).exists()

    def finalize(self, job: Job, part: Part) -> Path:
        src = self._part_path(job, part)
        dest_dir = self._artifact_dir(job)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = (dest_dir / safe_name(part.name)).resolve()
        if not dest.is_relative_to(dest_dir.resolve()):
            raise InvalidInputError("unsafe artifact file name")
        with src.open("rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(src, dest)
        part.state = "done"
        return dest


def sanitized(file: FileRef) -> FileRef:
    """FileRef safe to persist/return: signed query values redacted."""
    update: dict[str, Any] = {"uri": file.display_uri()}
    if file.index_uri:
        update["index_uri"] = FileRef(uri=file.index_uri).display_uri()
    return file.model_copy(update=update)


def file_checksums(path: Path, algorithms: set[str]) -> dict[str, str]:
    hashers = {a: hashlib.new(a) for a in sorted(algorithms | {"md5", "sha256"})}
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            for h in hashers.values():
                h.update(chunk)
    return {a: h.hexdigest() for a, h in hashers.items()}


def verify_checksums(file: FileRef, computed: dict[str, str]) -> tuple[bool, list[str]]:
    """Compare expected checksums from the FileRef with checksums of the complete file."""
    notes: list[str] = []
    verified = False
    for c in file.checksums:
        if c.algorithm not in VERIFIABLE:
            notes.append(f"{c.algorithm} checksum is not verified (identity only)")
            continue
        got = computed.get(c.algorithm)
        if got is None:
            continue
        if got.lower() != c.value.strip().lower():
            raise UpstreamError(
                f"{c.algorithm} checksum mismatch: the downloaded file differs from the "
                "expected checksum",
                retryable=False,
                details={"algorithm": c.algorithm, "expected": c.value, "computed": got},
            )
        verified = True
    return verified, notes


def artifact_model(
    job: Job,
    path: Path,
    index_path: Path | None,
    checksums: dict[str, str],
    verified: bool,
    fmt: FileFormat | None,
    method: str,
    transformations: list[str],
) -> LocalArtifact:
    origin = FileRef.model_validate(job.file)
    return LocalArtifact(
        path=str(path),
        size_bytes=path.stat().st_size,
        checksums=[Checksum(algorithm=a, value=v) for a, v in sorted(checksums.items())],
        checksum_verified=verified,
        format=fmt,
        index_path=str(index_path) if index_path else None,
        origin=origin,
        provenance=Provenance(
            source=origin.source or job.access,
            source_record_id=origin.accession,
            url=origin.uri,
            method=method,
            transformations=transformations,
        ),
    )


def default_name(file: FileRef) -> str:
    path = file.uri if file.uri.startswith("/") else urlsplit(file.uri).path
    return safe_name(Path(path).name or "file")


def cleanup_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def ctx_budget_error(
    size: int, budget: int, *, explicit: bool, ceiling: int
) -> BudgetExceededError:
    hint = (
        f"pass budget_bytes >= {size} (the ceiling is {ceiling})"
        if size <= ceiling
        else "the file is larger than the configured transfer ceiling"
    )
    return BudgetExceededError(
        f"transfer needs {size} bytes; the {'given' if explicit else 'default'} budget is {budget}",
        hint=hint,
        details={"size_bytes": size, "budget_bytes": budget, "ceiling_bytes": ceiling},
    )


__all__ = [
    "Job",
    "Part",
    "TransferManager",
    "artifact_model",
    "file_checksums",
    "safe_name",
    "sanitized",
    "transfer_key",
    "verify_checksums",
]
