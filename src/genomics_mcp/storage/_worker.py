"""Child-process entry point for native reader tasks. See `native.py`.

Protocol: one JSON object on stdin `{"task": "module:function", "params": {...},
"soft_deadline_s": float}`; one JSON object on stdout `{"ok": true, "result": ...}` or
`{"ok": false, "error": {...}}`. Only functions listed in a genomics_mcp module's
`NATIVE_TASKS` mapping can run. Task signature: `fn(params: dict, deadline: float) -> JSON`,
where `deadline` is a `time.monotonic()` value the task should stop at (returning a result
marked incomplete) before the parent kills the process.
"""

from __future__ import annotations

import importlib
import json
import sys
import time


def _error(code: str, message: str, **extra: object) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def main() -> int:
    started = time.monotonic()
    try:
        request = json.loads(sys.stdin.buffer.read())
        task = str(request["task"])
        params = request.get("params") or {}
        deadline = started + float(request.get("soft_deadline_s", 10.0))
    except (ValueError, KeyError, TypeError):
        sys.stdout.write(json.dumps(_error("internal_error", "invalid worker request")))
        return 2
    module_name, _, func_name = task.partition(":")
    if not module_name.startswith("genomics_mcp."):
        sys.stdout.write(json.dumps(_error("internal_error", "task not allowed")))
        return 2
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        sys.stdout.write(
            json.dumps(_error("internal_error", f"native task module failed to import: {exc.name}"))
        )
        return 2
    func = getattr(module, "NATIVE_TASKS", {}).get(func_name)
    if func is None:
        sys.stdout.write(json.dumps(_error("internal_error", "unknown native task")))
        return 2

    from genomics_mcp.errors import GenomicsError, redact

    try:
        result = func(params, deadline)
        text = json.dumps({"ok": True, "result": result}, allow_nan=False)
    except GenomicsError as exc:
        text = json.dumps({"ok": False, "error": exc.info.model_dump(mode="json")})
    except Exception as exc:  # noqa: BLE001 - reported as a typed error to the parent
        text = json.dumps(
            _error(
                "internal_error",
                f"native task failed: {type(exc).__name__}: {redact(str(exc))[:300]}",
            )
        )
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
