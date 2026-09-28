"""Optional SRA Toolkit workflow (convert_sra_run): public run accession -> verified FASTQ files.

Only an explicit convert_sra_run call starts it; discovery and region reads never do. Each run
is a TransferManager job of kind "sra", so get_transfer_status, cancel_transfer, the transfer
concurrency limit and the work dir quota apply unchanged.

The job owns transfers/<id>/sra.part/, removed on cancel or permanent failure:
    home/ tmp/ aws/ *.mkfg  HOME, TMPDIR, empty AWS files and NCBI_SETTINGS for the toolkit, so
                            neither ~/.ncbi nor any user toolkit configuration is read or written
    download/<acc>/         prefetch output: the run file and its dependencies (resumable)
    scratch/ fastq/         fasterq-dump temporary files and output, reset on every attempt

Steps:
1. prefetch <acc> --type sra --transport http --max-size <budget in KB>, resume and verify on.
   prefetch continues an interrupted download and checks the finished one.
2. fasterq-dump --size-check only --details gives an output estimate, the sequence table and
   the run's biological base count. Without an estimate or base count, or with a table other than
   SEQUENCE (e.g. PacBio CONSENSUS), the job fails before converting. It needs budget for the
   bytes already used plus 1.25x the estimate (output) plus 1.5x the estimate (scratch, NCBI's
   prefetch/fasterq-dump guide).
3. fasterq-dump --split-3 --skip-technical with remote access disabled, so a run whose local
   material is incomplete fails instead of being read from the network.
4. Every FASTQ record is checked: four lines, @ and + headers, equal sequence and quality
   lengths, `length=` matching the sequence, and a read name `<acc>.<spot>` with spot 1..spot
   count. Mate files must hold the same spots in the same order; taken together with the
   unpaired file, spot numbers must strictly increase (no spot duplicated or in two files). The
   read total must equal fasterq-dump's "reads written" and the bases written must equal the
   run's biological base count, so no biological read was dropped. Only then are the files
   moved to artifacts/<id>/ and the job marked completed. Output other than split-3's
   <acc>_1.fastq, <acc>_2.fastq and <acc>.fastq is refused, not guessed.

`budget_bytes` limits the job directory on disk: download, dependencies, scratch and FASTQ. It
is measured every 0.5 s while a toolkit process runs and once more when it exits; past the
budget the process group is killed and the job fails. This is a watchdog, not a filesystem
quota: between samples a process can overshoot by what it writes in 0.5 s. prefetch --max-size
applies to the run file only (prefetch 3.4.1 does not size-check dependencies). Network bytes are not metered separately; prefetch writes what it downloads
and resumes rather than restarting. NCBI's size figures are estimates (in testing the output
estimate was slightly below the real output), so they are used only to refuse early.

Toolkit settings: a random GUID, no cloud instance identity, no AWS/GCP charges, local cache off.
Children get the native-reader environment allowlist without cloud credentials, no shell, no
caller-supplied flags, and their output is captured (never written to MCP stdout).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from genomics_mcp.artifacts.fetch import _output, _tm
from genomics_mcp.artifacts.transfers import WAIT_FOR_COMPLETION_S, Job, Part, dir_usage
from genomics_mcp.config import Settings
from genomics_mcp.context import OperationContext
from genomics_mcp.errors import (
    BudgetExceededError,
    DeadlineExceededError,
    NotFoundError,
    UnauthorizedError,
    UnsupportedError,
    UpstreamError,
    redact,
)
from genomics_mcp.models import (
    AccessStatus,
    Checksum,
    FileFormat,
    FileRef,
    LocalArtifact,
    Provenance,
    Readiness,
    ReadinessState,
    Visibility,
)
from genomics_mcp.requests import ConvertSraRunRequest
from genomics_mcp.result import OperationOutput
from genomics_mcp.security import scrubbed_env
from genomics_mcp.storage.native import ENV_ALLOWLIST, kill_process_group

log = logging.getLogger("genomics_mcp.artifacts.sra")

COMPONENT = "sra_toolkit"
MIN_VERSION = (3, 0, 0)
TESTED_VERSION = "3.4.1"
OUTPUT_MARGIN = 1.25
SCRATCH_FACTOR = 1.5
WATCH_S = 0.5
MAX_OUTPUT_CHARS = 64 * 1024
MAX_LINE_BYTES = 16 * 1024 * 1024
DRAIN_S = 5.0
INSTALL_HINT = (
    "install SRA Toolkit 3.x (https://github.com/ncbi/sra-tools/wiki/01.-Downloading-SRA-Toolkit) "
    "and put prefetch and fasterq-dump on PATH or set sra_toolkit.bin_dir, or use the "
    "genomics-mcp SRA container image. ENA FASTQ files (list_files, fetch_file) need no toolkit."
)
CONVERSION = {
    "mode": "split-3",
    "technical_reads": "skipped (--skip-technical)",
    "deflines": "fasterq-dump default",
    "remote_access_during_conversion": "disabled",
    "note": "split-3 writes mates to <acc>_1/_2.fastq and reads without a mate to <acc>.fastq",
}

_VERSION = re.compile(r":\s*(\d+)\.(\d+)\.(\d+)\s*$", re.MULTILINE)
_ESTIMATE = re.compile(r"est\. output\s*:\s*([\d,]+) bytes")
_TABLE = re.compile(r"uses '(\w+)' as sequence-table")
_SPOTS = re.compile(r"SEQ\.spot_count\s*=\s*([\d,]+)")
_BIO_BASES = re.compile(r"SEQ\.bio_base_count\s*=\s*([\d,]+)")
_WRITTEN = re.compile(r"reads written\s*:\s*([\d,]+)")


# ---------------------------------------------------------------------------- toolkit


@dataclass(frozen=True)
class Toolkit:
    prefetch: str
    fasterq_dump: str
    version: str


class SraToolkit:
    """Finds prefetch and fasterq-dump and their version. A found toolkit is remembered;
    a missing one is looked up again on the next call, so installing it needs no restart."""

    def __init__(self) -> None:
        self.found: Toolkit | None = None

    def probe(self, settings: Settings) -> tuple[Toolkit | None, str]:
        if self.found is None:
            self.found, reason = _probe(settings)
            return self.found, reason
        return self.found, "ok"

    def require(self, settings: Settings) -> Toolkit:
        toolkit, reason = self.probe(settings)
        if toolkit is None:
            raise UnsupportedError(
                f"SRA Toolkit is not available: {reason}", source="sra", hint=INSTALL_HINT
            )
        return toolkit

    def status(self, settings: Settings) -> dict[str, Any]:
        toolkit, reason = self.probe(settings)
        return {
            "available": toolkit is not None,
            "version": toolkit.version if toolkit else None,
            "tested_version": TESTED_VERSION,
            "prefetch": toolkit.prefetch if toolkit else None,
            "fasterq_dump": toolkit.fasterq_dump if toolkit else None,
            "detail": reason if toolkit is None else "ok",
        }


def _probe(settings: Settings) -> tuple[Toolkit | None, str]:
    bin_dir = settings.sra_toolkit.bin_dir
    paths: dict[str, str] = {}
    for name in ("prefetch", "fasterq-dump"):
        if bin_dir is not None:
            candidate = bin_dir / name
            exe = str(candidate) if os.access(candidate, os.X_OK) else None
        else:
            exe = shutil.which(name)
        if exe is None:
            return None, f"{name} was not found in {bin_dir or 'PATH'}"
        # Not resolved: the toolkit's driver dispatches on the name it was started as.
        paths[name] = os.path.abspath(exe)
    root = settings.paths.work_dir / ".isolation" / "sra-probe"
    env = toolkit_env(root, remote=False)
    versions = set()
    for name, exe in paths.items():
        try:
            done = subprocess.run(  # noqa: S603 - fixed executable and arguments, no shell
                [exe, "--version"],
                env=env,
                cwd=root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"{name} --version failed ({type(exc).__name__})"
        match = _VERSION.search(done.stdout + done.stderr)
        if done.returncode != 0 or match is None:
            return None, f"{name} --version did not report a version"
        versions.add(tuple(int(x) for x in match.groups()))
    if len(versions) != 1:
        return None, "prefetch and fasterq-dump report different versions"
    version = versions.pop()
    shown = ".".join(str(x) for x in version)
    if version < MIN_VERSION:
        return None, f"version {shown} is older than the supported 3.0.0"
    return Toolkit(paths["prefetch"], paths["fasterq-dump"], shown), "ok"


def toolkit_env(root: Path, *, remote: bool) -> dict[str, str]:
    """Environment for a toolkit child: allowlisted variables without cloud credentials, and
    HOME, TMPDIR and NCBI_SETTINGS inside `root`. `remote=False` disables network access."""
    for d in (root / "home", root / "tmp"):
        d.mkdir(parents=True, exist_ok=True)
    cfg = root / ("remote.mkfg" if remote else "local.mkfg")
    if not cfg.exists():
        values = {
            "/LIBS/GUID": str(uuid.uuid4()),
            "/libs/cloud/report_instance_identity": "false",
            "/libs/cloud/accept_aws_charges": "false",
            "/libs/cloud/accept_gcp_charges": "false",
            "/repository/user/cache-disabled": "true",
            "/repository/remote/disabled": "false" if remote else "true",
        }
        cfg.write_text("".join(f'{k} = "{v}"\n' for k, v in values.items()))
    base = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env = scrubbed_env(base, empty_aws_config_dir=root / "aws")
    env.update(HOME=str(root / "home"), TMPDIR=str(root / "tmp"), NCBI_SETTINGS=str(cfg))
    return env


# ---------------------------------------------------------------------------- handler


def _origin(accession: str) -> FileRef:
    return FileRef(
        uri=f"https://www.ncbi.nlm.nih.gov/sra/{accession}",
        source="sra",
        accession=accession,
        access_status=AccessStatus.OPEN,
        visibility=Visibility.PUBLIC,
    )


def _unchanged(job: Job) -> bool:
    identities = (job.preparation or {}).get("identities") or {}
    for art in job.artifacts:
        try:
            st = Path(art["path"]).stat()
        except OSError:
            return False
        if identities.get(art["path"]) != {"size": st.st_size, "mtime_ns": st.st_mtime_ns}:
            return False
    return bool(job.artifacts)


async def convert_sra_run(req: ConvertSraRunRequest, ctx: OperationContext) -> OperationOutput:
    toolkit = await asyncio.to_thread(ctx.require_component(COMPONENT).require, ctx.settings)
    tm = _tm(ctx)
    acc = req.accession
    key = hashlib.sha256(json.dumps(["sra-toolkit", acc]).encode()).hexdigest()
    existing = tm.find(key)
    wait_s = min(WAIT_FOR_COMPLETION_S, ctx.deadline.remaining() - 1.0)
    if existing is not None and existing.state in ("running", "queued"):
        await tm.wait(existing, wait_s)
        return _output(existing, ["a conversion of this run is already in progress"])
    if existing is not None and existing.state == "completed" and _unchanged(existing):
        return _output(existing, ["already converted; the FASTQ files are unchanged"])

    budget = tm.budget_for(req.budget_bytes)
    limit = ctx.settings.sra_toolkit.timeout_s
    timeout = min(req.timeout_s or limit, limit)
    job = existing if existing and existing.state == "failed" and not existing.no_resume else None
    kept = dir_usage(tm._part_path(job, job.parts[0])) if job else 0
    work = ctx.settings.paths.work_dir
    work.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(work).free
    if free + kept < budget:
        raise BudgetExceededError(
            "the disk does not have room for the job budget",
            hint="free disk space or pass a smaller budget_bytes",
            details={"free_bytes": free, "budget_bytes": budget},
        )
    tm.check_quota(max(0, budget - kept), exclude=job.transfer_id if job else None)
    if job is None:
        job = tm.new_job(
            key,
            _origin(acc),
            budget,
            include_index=False,
            prepare=True,
            verify=True,
            parts=[Part("sra", acc, f"sra:{acc}")],
            access="sra",
        )
        job.kind = "sra"
        if existing is not None:
            job.notes.append("the previous result is not reusable; converting again")
    else:
        job.budget_bytes = budget
        job.parts[0].done = kept  # the reservation is the budget minus what is on disk
        job.notes.append(
            "resuming: prefetch continues and verifies the download; conversion starts again"
        )

    async def runner(job: Job, cancel: asyncio.Event) -> None:
        try:
            async with asyncio.timeout(timeout):
                await _run(tm, job, toolkit, ctx.settings, cancel)
        except TimeoutError:
            raise DeadlineExceededError(
                f"the conversion did not finish within {timeout:g}s; the toolkit was stopped",
                source="sra",
                hint=f"{job.retry_hint} (the download is kept)",
            ) from None

    tm.start(job, runner)
    await tm.wait(job, wait_s)
    return _output(job)


# ---------------------------------------------------------------------------- job


async def _run(
    tm: Any, job: Job, toolkit: Toolkit, settings: Settings, cancel: asyncio.Event
) -> None:
    part = job.parts[0]
    acc = part.name
    root = tm._part_path(job, part)
    download, scratch, fastq = root / "download", root / "scratch", root / "fastq"
    for d in (scratch, fastq):
        shutil.rmtree(d, ignore_errors=True)
    for d in (download, scratch, fastq):
        d.mkdir(parents=True, exist_ok=True)
    watch = _Watch(tm, job, part, root)
    try:
        started = time.monotonic()
        # --max-size is compared with each file's full size (also when resuming), so it is the
        # whole budget; the watchdog bounds the total.
        max_kb = max(1, job.budget_bytes // 1024)
        rc, out = await watch.run(
            [
                toolkit.prefetch,
                acc,
                "--type",
                "sra",
                "--transport",
                "http",
                "--max-size",
                str(max_kb),
                "--resume",
                "yes",
                "--verify",
                "yes",
                "--output-directory",
                str(download),
            ],
            toolkit_env(root, remote=True),
        )
        run_dir = download / acc
        run_file = next(
            (
                run_dir / f"{acc}{s}"
                for s in (".sra", ".sralite")
                if (run_dir / f"{acc}{s}").is_file()
            ),
            None,
        )
        if rc != 0 or run_file is None:
            raise _prefetch_error(acc, rc, out)
        download_s = time.monotonic() - started
        if cancel.is_set():
            return

        common = [
            str(run_dir),
            "--outdir",
            str(fastq),
            "--temp",
            str(scratch),
            "--split-3",
            "--skip-technical",
            "--threads",
            str(settings.sra_toolkit.threads),
        ]
        local = toolkit_env(root, remote=False)
        rc, details = await watch.run(
            [toolkit.fasterq_dump, *common, "--size-check", "only", "--details"], local
        )
        estimate = _number(_ESTIMATE, details)
        table = _text(_TABLE, details)
        spots = _number(_SPOTS, details)
        bio_bases = _number(_BIO_BASES, details)
        if rc != 0 or None in (estimate, spots, bio_bases):
            raise UpstreamError(
                "fasterq-dump did not report the output estimate, spot count and biological "
                "base count needed to bound and verify the conversion; it was not started",
                source="sra",
                retryable=False,
                details={"toolkit_output": _tail(details)},
            )
        if table != "SEQUENCE":
            raise UnsupportedError(
                f"runs read from the {table} table are not supported: completeness of their "
                "FASTQ output cannot be verified here",
                source="sra",
                details={"sequence_table": table},
            )
        used = dir_usage(root)
        needed = used + math.ceil(estimate * OUTPUT_MARGIN) + math.ceil(estimate * SCRATCH_FACTOR)
        estimates = {
            "run_bytes_on_disk": used,
            "output_bytes_estimate": estimate,
            "required_bytes": needed,
            "rule": "bytes used + 1.25 x output estimate + 1.5 x output estimate for scratch",
        }
        if needed > job.budget_bytes:
            raise BudgetExceededError(
                f"conversion needs about {needed} bytes; the budget is {job.budget_bytes}",
                source="sra",
                retryable=True,
                hint=f"pass budget_bytes >= {needed}; the download is kept",
                details=estimates,
            )
        free = shutil.disk_usage(root).free
        if needed - used > free:
            raise BudgetExceededError(
                "not enough free disk space for the conversion",
                source="sra",
                retryable=True,
                details={**estimates, "free_bytes": free},
            )
        if cancel.is_set():
            return

        started = time.monotonic()
        room = str(job.budget_bytes - used)
        rc, conv = await watch.run(
            [toolkit.fasterq_dump, *common, "--disk-limit", room, "--disk-limit-tmp", room], local
        )
        if rc != 0:
            raise _conversion_error(rc, conv)
        written = _number(_WRITTEN, conv)
        if written is None:
            raise UpstreamError(
                "fasterq-dump did not report how many reads it wrote; the output is not used",
                source="sra",
                retryable=False,
                details={"toolkit_output": _tail(conv)},
            )
        stop = threading.Event()
        verifying = asyncio.ensure_future(
            asyncio.to_thread(verify_fastq, fastq, acc, spots, stop.is_set)
        )
        try:
            files = await asyncio.shield(verifying)
        finally:
            # On cancel or timeout the reader thread stops and is waited for before its files
            # are removed.
            stop.set()
            with contextlib.suppress(BaseException):
                await verifying
        convert_s = time.monotonic() - started
        if cancel.is_set():
            return
        counted = sum(f["reads"] for f in files)
        bases = sum(f["bases"] for f in files)
        pairs = next((f["reads"] for f in files if f["role"] == "mate_1"), 0)
        unpaired = next((f["reads"] for f in files if f["role"] == "unpaired"), 0)
        if counted != written or bases != bio_bases:
            raise UpstreamError(
                "the FASTQ files do not match fasterq-dump's report; the output is not used",
                source="sra",
                retryable=False,
                details={
                    "reads_counted": counted,
                    "reads_written": written,
                    "spots": spots,
                    "bases_counted": bases,
                    "biological_bases_in_run": bio_bases,
                },
            )
        _publish(tm, job, toolkit, run_file, fastq, files)
        job.preparation.update(
            {
                "estimates": estimates,
                "sequence_table": table,
                "spot_count": spots,
                "verification": {
                    "reads_written_by_fasterq_dump": written,
                    "reads_counted": counted,
                    "bases_counted": bases,
                    "biological_bases_in_run": bio_bases,
                    "all_biological_bases_written": True,
                    "spot_numbers_strictly_increasing": True,
                    "pairs": pairs,
                    "unpaired_reads": unpaired,
                    "records_well_formed": True,
                    "mate_read_names_match": bool(pairs),
                },
                "resources": {
                    "peak_job_bytes_sampled": watch.peak,
                    "download_s": round(download_s, 2),
                    "conversion_s": round(convert_s, 2),
                },
            }
        )
    except BaseException:
        shutil.rmtree(fastq, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
        part.done = dir_usage(root)
        raise
    shutil.rmtree(root, ignore_errors=True)
    part.state = "done"
    part.done = part.expected_size = sum(a["size_bytes"] for a in job.artifacts)
    job.state = "completed"
    job.error = None
    tm._persist(job)


def _publish(
    tm: Any, job: Job, toolkit: Toolkit, run_file: Path, fastq_dir: Path, files: list[dict]
) -> None:
    acc = job.parts[0].name
    art_dir = tm.art_root / job.transfer_id
    art_dir.mkdir(parents=True, exist_ok=True)
    origin = _origin(acc)
    run_dir = run_file.parent
    dependencies = sorted(
        p.name
        for p in run_dir.iterdir()
        if p != run_file and p.is_file() and not p.name.endswith((".prf", ".tmp", ".lock"))
    )
    provenance = Provenance(
        source="sra",
        source_record_id=acc,
        url=origin.uri,
        method=f"SRA Toolkit {toolkit.version}: prefetch, fasterq-dump --split-3 --skip-technical",
        transformations=[
            "whole SRA run converted to FASTQ by fasterq-dump",
            CONVERSION["note"],
            "technical reads skipped (fasterq-dump default)",
        ],
    )
    artifacts, fastq, identities = [], [], {}
    for f in files:
        dest = art_dir / f["name"]
        os.replace(fastq_dir / f["name"], dest)
        st = dest.stat()
        identities[str(dest)] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        artifacts.append(
            LocalArtifact(
                path=str(dest),
                size_bytes=st.st_size,
                checksums=[
                    Checksum(algorithm="md5", value=f["md5"]),
                    Checksum(algorithm="sha256", value=f["sha256"]),
                ],
                format=FileFormat.FASTQ,
                origin=origin,
                provenance=provenance,
            ).model_dump(mode="json")
        )
        fastq.append(
            {
                "role": f["role"],
                "reads": f["reads"],
                "bases": f["bases"],
                "file": FileRef(
                    uri=str(dest),
                    format=FileFormat.FASTQ,
                    source="sra",
                    accession=acc,
                    access_status=AccessStatus.OPEN,
                    visibility=Visibility.PUBLIC,
                    size_bytes=st.st_size,
                    readiness=Readiness(
                        state=ReadinessState.NOT_LOCUS_READY,
                        reasons=["FASTQ reads are unaligned and cannot be queried by locus"],
                    ),
                ).model_dump(mode="json"),
            }
        )
    job.artifacts = artifacts
    job.preparation = {
        "workflow": "sra_toolkit",
        "accession": acc,
        "toolkit": {
            "version": toolkit.version,
            "prefetch": toolkit.prefetch,
            "fasterq_dump": toolkit.fasterq_dump,
        },
        "run_file": {
            "name": run_file.name,
            "size_bytes": run_file.stat().st_size,
            "note": "SRA Lite: simplified base quality scores"
            if run_file.suffix == ".sralite"
            else "SRA Normalized format (full base quality scores)",
        },
        "dependencies": dependencies,
        "conversion": CONVERSION,
        "fastq": fastq,
        "readiness": ReadinessState.NOT_LOCUS_READY.value,
        "checksums_note": "md5/sha256 describe the FASTQ files as written; the archive publishes "
        "no FASTQ checksums for toolkit output, so checksum_verified is false",
        "cleanup": "the downloaded run and scratch files were removed after conversion",
        "identities": identities,
    }


class _Watch:
    """Runs toolkit commands and keeps the job directory within the budget."""

    def __init__(self, tm: Any, job: Job, part: Part, root: Path) -> None:
        self.tm, self.job, self.part, self.root = tm, job, part, root
        self.peak = 0

    def measure(self) -> None:
        used = dir_usage(self.root)
        self.peak = max(self.peak, used)
        self.part.done = used
        if used > self.job.budget_bytes:
            raise BudgetExceededError(
                f"the job used {used} bytes on disk, over its budget of {self.job.budget_bytes}; "
                "the toolkit was stopped",
                source="sra",
                hint="pass a larger budget_bytes",
            )

    async def run(self, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        """Run one command without a shell in its own process group; return (rc, output)."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(self.root / "tmp"),
            start_new_session=True,
        )
        assert proc.stdout is not None
        reader = asyncio.create_task(_read_tail(proc.stdout))
        waiter = asyncio.create_task(proc.wait())
        try:
            # returncode, not the waiter: wait() does not return while a descendant still holds
            # the output pipe.
            while True:
                await asyncio.wait({waiter}, timeout=WATCH_S)
                self.measure()  # also once after the leader exits
                if proc.returncode is not None:
                    break
            # Descendants that outlived the leader are stopped too, so none can keep writing
            # or hold the output pipe open.
            kill_process_group(proc)
            self.tm._persist(self.job)
            try:
                output = await asyncio.wait_for(reader, DRAIN_S)
            except TimeoutError:
                output = "(toolkit output was not closed)"
        finally:
            kill_process_group(proc)
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    await proc.wait()
            for task in (reader, waiter):
                task.cancel()
        log.debug("sra %s rc=%s output: %s", Path(argv[0]).name, proc.returncode, output[-2000:])
        return proc.returncode or 0, output


async def _read_tail(stream: asyncio.StreamReader) -> str:
    buf = bytearray()
    while chunk := await stream.read(65536):
        buf.extend(chunk)
        if len(buf) > MAX_OUTPUT_CHARS:
            del buf[:-MAX_OUTPUT_CHARS]
    return redact(buf.decode("utf-8", errors="replace"))


def _tail(text: str) -> str:
    return text.strip()[-1500:]


def _number(pattern: re.Pattern[str], text: str) -> int | None:
    m = pattern.search(text)
    return int(m.group(1).replace(",", "")) if m else None


def _text(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def _prefetch_error(acc: str, rc: int, out: str) -> Exception:
    details = {"exit_status": rc, "toolkit_output": _tail(out)}
    if "larger than maximum allowed" in out:
        return BudgetExceededError(
            "the run file is larger than the remaining budget; prefetch skipped it",
            source="sra",
            hint="pass a larger budget_bytes",
            details=details,
        )
    if re.search(r"\b403\b|access denied|forbidden", out, re.IGNORECASE):
        return UnauthorizedError(
            f"NCBI refused access to {acc}; controlled-access (dbGaP) runs are not supported",
            source="sra",
            details=details,
        )
    if re.search(r"no data \( 404 \)|not found", out, re.IGNORECASE):
        return NotFoundError(
            f"NCBI could not resolve {acc} to a downloadable run",
            source="sra",
            hint=f"check https://www.ncbi.nlm.nih.gov/sra/{acc}; ENA may still serve its FASTQ files",
            details=details,
        )
    return UpstreamError(
        f"prefetch did not produce the run file (exit status {rc})",
        source="sra",
        retryable=True,
        details=details,
    )


def _conversion_error(rc: int, out: str) -> Exception:
    details = {"exit_status": rc, "toolkit_output": _tail(out)}
    if "disk-limit exeeded" in out or "disk-limit exceeded" in out:
        return BudgetExceededError(
            "fasterq-dump estimated that the conversion does not fit in the remaining budget",
            source="sra",
            retryable=True,
            hint="pass a larger budget_bytes; the download is kept",
            details=details,
        )
    return UpstreamError(
        f"fasterq-dump failed (exit status {rc}); partial output was removed",
        source="sra",
        retryable=True,
        details=details,
    )


# ---------------------------------------------------------------------------- FASTQ checks


class _Reader:
    """Reads four-line FASTQ records, hashing every byte and counting reads and bases."""

    def __init__(self, path: Path, acc: str, spots: int | None) -> None:
        self.path = path
        self.fh = path.open("rb")
        self.prefix = f"{acc}.".encode()
        self.spots = spots
        self.md5 = hashlib.md5()  # noqa: S324 - file checksum, not security
        self.sha256 = hashlib.sha256()
        self.reads = 0
        self.bases = 0

    def bad(self, why: str) -> UpstreamError:
        return UpstreamError(
            f"{self.path.name}: {why} at read {self.reads + 1}; the output is not used",
            source="sra",
            retryable=False,
        )

    def next(self) -> int | None:
        """The next record's spot number, or None at the end of the file."""
        lines = [self.fh.readline(MAX_LINE_BYTES + 1) for _ in range(4)]
        for line in lines:
            self.md5.update(line)
            self.sha256.update(line)
        if not lines[0]:
            return None
        if any(len(line) > MAX_LINE_BYTES for line in lines):
            raise self.bad(f"a line longer than {MAX_LINE_BYTES} bytes")
        if not all(line.endswith(b"\n") for line in lines):
            raise self.bad("truncated record")
        head, seq, plus, qual = (line.rstrip(b"\r\n") for line in lines)
        if not head.startswith(b"@") or not plus.startswith(b"+"):
            raise self.bad("not a FASTQ record")
        if len(seq) != len(qual):
            raise self.bad("sequence and quality lengths differ")
        words = head[1:].split()
        name = words[0] if words else b""
        spot = name[len(self.prefix) :]
        if not name.startswith(self.prefix) or not spot.isdigit():
            raise self.bad("read name is not <accession>.<spot> from this run")
        length = next((w[7:] for w in words if w.startswith(b"length=")), None)
        if length is not None and length != str(len(seq)).encode():
            raise self.bad("length= does not match the sequence")
        number = int(spot)
        if not 1 <= number <= (self.spots or number):
            raise self.bad("spot number outside the run")
        self.reads += 1
        self.bases += len(seq)
        return number

    def summary(self, role: str) -> dict[str, Any]:
        self.fh.close()
        return {
            "name": self.path.name,
            "role": role,
            "reads": self.reads,
            "bases": self.bases,
            "size_bytes": self.path.stat().st_size,
            "md5": self.md5.hexdigest(),
            "sha256": self.sha256.hexdigest(),
        }


def verify_fastq(
    directory: Path, acc: str, spots: int | None, stop: Callable[[], bool]
) -> list[dict[str, Any]]:
    """Check fasterq-dump split-3 output independently of the toolkit's own report.

    Mates are read in step; the pair stream and the unpaired file are merged by spot number,
    which must strictly increase, so a spot cannot repeat or appear in both."""
    names = sorted(p.name for p in directory.iterdir())
    mates = [m for m in (f"{acc}_1.fastq", f"{acc}_2.fastq") if m in names]
    unpaired = f"{acc}.fastq" if f"{acc}.fastq" in names else None
    unexpected = sorted(set(names) - set(mates) - {unpaired})
    if unexpected or not names or len(mates) == 1:
        raise UpstreamError(
            "fasterq-dump output files are not the expected split-3 set; the output is not used",
            source="sra",
            retryable=False,
            details={"files": names},
        )
    readers = [_Reader(directory / m, acc, spots) for m in mates]
    single = _Reader(directory / unpaired, acc, spots) if unpaired else None

    def next_pair() -> int | None:
        if not readers:
            return None
        ids = [r.next() for r in readers]
        if len(set(ids)) != 1:
            raise readers[0].bad("mate files disagree on spots or read counts")
        return ids[0]

    try:
        pair, alone, last = next_pair(), single.next() if single else None, 0
        while (pair is not None or alone is not None) and not stop():
            if alone is None or (pair is not None and pair < alone):
                current, pair = pair, next_pair()
            else:
                current, alone = alone, single.next() if single else None
            if current is None or current <= last:
                raise (readers or [single])[0].bad(
                    "spot numbers repeat or are out of order across the split-3 files"
                )
            last = current
    finally:
        results = [r.summary(f"mate_{n}") for n, r in enumerate(readers, 1)]
        if single:
            results.append(single.summary("unpaired"))
    return results


__all__ = ["COMPONENT", "SraToolkit", "convert_sra_run", "toolkit_env", "verify_fastq"]
