"""Internal self-test tasks for the native runner (isolation, deadline kill, stderr redaction).

Not exposed as MCP tools; used by tests and `--check-config`-style diagnostics only.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from genomics_mcp.errors import InvalidInputError


def env_report(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    keys = sorted(os.environ)
    return {
        "keys": keys,
        "HOME": os.environ.get("HOME"),
        "REF_PATH": os.environ.get("REF_PATH"),
        "REF_CACHE": os.environ.get("REF_CACHE"),
        "AWS_CONFIG_FILE": os.environ.get("AWS_CONFIG_FILE"),
        "cwd": os.getcwd(),
        "cwd_entries": sorted(os.listdir(".")),
    }


def sleep(p: dict[str, Any], deadline: float) -> dict[str, Any]:
    time.sleep(float(p.get("seconds", 60)))
    return {"slept": True}


def fail_with_stderr(p: dict[str, Any], deadline: float) -> None:
    sys.stderr.write(f"native message about {p['text']}\n")
    sys.stderr.flush()
    raise InvalidInputError("self-test failure")


NATIVE_TASKS = {"env_report": env_report, "sleep": sleep, "fail_with_stderr": fail_with_stderr}
