"""Run native readers (pysam/HTSlib, pyBigWig) in scrubbed, killable child processes.

Why a process and not a thread: libcurl/HTSlib calls cannot be interrupted from Python, so
a hung remote read would outlive the call deadline. Each task runs in a fresh
`python -I -m genomics_mcp.storage._worker` with:

- an environment built from a small allowlist (no AWS_*/cloud variables, no proxies, no
  netrc/curl config), `HOME` set to an empty isolation directory, AWS config/credential
  files pointing at empty files, EC2 metadata disabled;
- `REF_PATH`/`REF_CACHE` pointing at an empty directory, so HTSlib cannot look up CRAM
  references from the network or a user cache;
- an empty working directory, so relative paths in file headers resolve to nothing;
- its own process group, killed with SIGKILL at the deadline.

Parameters (which may include signed URLs) travel on stdin, never argv. Native stderr is
captured, bounded and redacted before it is logged or returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from genomics_mcp.config import Settings
from genomics_mcp.errors import (
    DeadlineExceededError,
    ErrorCode,
    GenomicsError,
    InvalidInputError,
    UpstreamError,
    redact,
)
from genomics_mcp.security import scrubbed_env

log = logging.getLogger("genomics_mcp.storage.native")

WORKER_MODULE = "genomics_mcp.storage._worker"
MAX_STDERR_BYTES = 64 * 1024
MAX_STDOUT_BYTES = 256 * 1024 * 1024
KILL_MARGIN_S = 0.25

# Only these variables are copied from the parent environment.
ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CURL_CA_BUNDLE",
    "SYSTEMROOT",
)


class NativeRunner:
    def __init__(self, settings: Settings, *, max_concurrency: int = 4) -> None:
        self.settings = settings
        self.base = settings.paths.work_dir / ".isolation"
        self._sem = asyncio.Semaphore(max_concurrency)
        self._env: dict[str, str] | None = None

    @property
    def cwd(self) -> Path:
        return self.base / "cwd"

    def child_env(self) -> dict[str, str]:
        if self._env is None:
            home = self.base / "home"
            for d in (home, self.cwd):
                d.mkdir(parents=True, exist_ok=True)
            base = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
            env = scrubbed_env(
                base, empty_aws_config_dir=self.base / "aws", empty_ref_dir=self.base / "empty-ref"
            )
            env["HOME"] = str(home)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            self._env = env
        return dict(self._env)

    async def run(
        self,
        task: str,
        params: dict[str, Any],
        *,
        timeout_s: float,
        what: str = "native reader",
    ) -> Any:
        """Run `module:function` from a genomics_mcp module in a child; return its JSON result.

        Raises the child's typed error, `timeout` at the deadline (child killed), or
        `upstream_error` if the child crashed.
        """
        if timeout_s <= KILL_MARGIN_S:
            raise DeadlineExceededError(f"{what}: no time left before the deadline")
        payload = json.dumps(
            {"task": task, "params": params, "soft_deadline_s": max(0.1, timeout_s - 1.0)}
        ).encode()
        async with self._sem:
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                WORKER_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.child_env(),
                cwd=str(self.cwd),
                start_new_session=True,
            )
            try:
                out, err = await asyncio.wait_for(
                    _communicate(proc, payload), timeout=max(0.1, timeout_s - KILL_MARGIN_S)
                )
            except TimeoutError:
                _kill(proc)
                await proc.wait()
                raise DeadlineExceededError(
                    f"{what} did not finish before the deadline; the reader process was stopped",
                    hint="query a smaller interval",
                ) from None
            except BaseException:
                _kill(proc)
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise
            finally:
                if proc.returncode is None:
                    _kill(proc)
            stderr_text = redact(err.decode("utf-8", errors="replace"))
            elapsed_ms = (time.monotonic() - started) * 1000
            if stderr_text.strip():
                log.debug("native %s stderr: %s", task, stderr_text[-2000:])
            log.debug("native %s rc=%s ms=%d", task, proc.returncode, elapsed_ms)
        return _decode(task, proc.returncode, out, stderr_text, what)


async def _communicate(proc: asyncio.subprocess.Process, payload: bytes) -> tuple[bytes, bytes]:
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(payload)
    await proc.stdin.drain()
    proc.stdin.close()

    async def bounded(stream: asyncio.StreamReader, cap: int, name: str) -> bytes:
        buf = bytearray()
        while chunk := await stream.read(65536):
            if len(buf) + len(chunk) > cap:
                if name == "stdout":
                    raise UpstreamError("native reader output exceeded its size bound")
                buf.extend(chunk[: max(0, cap - len(buf))])
                continue
            buf.extend(chunk)
        return bytes(buf)

    out, err = await asyncio.gather(
        bounded(proc.stdout, MAX_STDOUT_BYTES, "stdout"),
        bounded(proc.stderr, MAX_STDERR_BYTES, "stderr"),
    )
    await proc.wait()
    return out, err


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


def _decode(task: str, rc: int | None, out: bytes, stderr_text: str, what: str) -> Any:
    tail = stderr_text.strip()[-1500:]
    try:
        message = json.loads(out) if out else None
    except ValueError:
        message = None
    if not isinstance(message, dict):
        raise UpstreamError(
            f"{what} failed (exit status {rc})",
            details={"native_stderr": tail} if tail else {},
        )
    if message.get("ok"):
        return message.get("result")
    err = message.get("error") or {}
    try:
        code = ErrorCode(err.get("code", "internal_error"))
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    details = dict(err.get("details") or {})
    if tail and "native_stderr" not in details:
        details["native_stderr"] = tail
    raise GenomicsError(
        err.get("message") or f"{what} failed",
        code=code,
        source=err.get("source"),
        hint=err.get("hint"),
        details=details,
    )


def validate_task(task: str) -> tuple[str, str]:
    module, _, func = task.partition(":")
    if not module.startswith("genomics_mcp.") or not func:
        raise InvalidInputError("invalid native task")
    return module, func
