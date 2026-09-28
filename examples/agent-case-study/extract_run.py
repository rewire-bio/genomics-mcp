#!/usr/bin/env python3
"""Turn a raw Claude Code stream-json log into committed, redacted run evidence.

    python3 examples/agent-case-study/extract_run.py RUN_DIR

Reads RUN_DIR/raw/transcript.jsonl (git-ignored, never published) and RUN_DIR/launch.json.
Publishes only what the model visibly did: its visible text messages, its MCP tool requests,
the tool results exactly as returned, and its final message. Hidden thinking, token/rate-limit
events, request IDs, message UUIDs and local paths are not written. Writes:

- transcript.redacted.jsonl: visible messages, tool requests/results, model changes, final result;
- tool_calls.json: one entry per MCP call, with per-file signal means (null when a file failed);
- agent_report.md / agent_report.json: the final message and its JSON block, or an explicit
  incomplete marker when the run produced no final report;
- run.json: completion status, models, timing, token totals, call counts and the SHA-256 of the
  unmodified local log.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

HOME = str(Path.home())
_SIGNED = re.compile(r"(?i)(x-amz-[a-z-]+|signature|sig|token|expires)=[^&\s\"'\\]+")
_TMP = re.compile(r"(/private)?(/var/folders/[^\s\"'\\]+?|/tmp)/gmcp-agent-[A-Za-z0-9_]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_FORBIDDEN = ("sk-ant-", "AKIA", "aws_secret_access_key", "/Users/", "/home/")
PREFIX = "mcp__genomics__"


def redact(text: str) -> str:
    text = _SIGNED.sub(lambda m: f"{m.group(1)}=REDACTED", text)
    text = _TMP.sub("$SCRATCH", text)
    return text.replace(HOME, "~")


def check_clean(text: str, what: str) -> None:
    bad = [s for s in _FORBIDDEN if s in text]
    emails = sorted(set(_EMAIL.findall(text)))
    if bad or emails:
        raise SystemExit(f"{what}: refusing to write, found {bad or emails[:3]}")


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def measurements(tool: str | None, args: dict, body: Any) -> list[dict]:
    """Per-file whole-interval signal means requested by this call, one entry per requested file.

    A file whose component failed, or a call that failed, keeps its entry with mean null and
    the status/error, so missing data is explicit rather than absent.
    """
    iv = args.get("interval") or {}
    span = {k: iv.get(k) for k in ("contig", "start", "end", "assembly")}
    requested = args.get("files") or ([args["file"]] if tool == "get_signal" else [])
    if tool not in ("get_signal", "compare_samples") or not requested:
        return []
    ok = isinstance(body, dict) and body.get("status") in ("ok", "partial")
    data = (body.get("data") or {}) if ok else {}
    out = []
    for i, f in enumerate(requested):
        entry: dict[str, Any] = {"file": f.get("accession") or f.get("uri"), **span}
        if not ok:
            err = body.get("error") if isinstance(body, dict) else None
            entry.update(mean=None, status="error", error=err or "call failed")
        elif tool == "get_signal":
            if args.get("summary", "mean") != "mean":
                continue
            entry.update(mean=(data.get("summary") or {}).get("value"), status="ok")
        else:
            comp = next((e for e in data.get("files") or [] if e.get("id") == f"f{i}"), None)
            if comp is None:
                entry.update(mean=None, status="missing", error="no component entry")
            else:
                if (comp.get("accession") or comp.get("file")) != entry["file"]:
                    raise SystemExit(
                        f"component f{i} does not match requested file {entry['file']}"
                    )
                entry.update(
                    mean=(comp.get("summary") or {}).get("value"),
                    status=comp.get("status"),
                    error=comp.get("errors") or None,
                )
        out.append(entry)
    return out


def final_json(report: str) -> Any:
    blocks = re.findall(r"```json\s*\n(.*?)\n```", report, flags=re.S)
    return parse_json(blocks[-1]) if blocks else None


def main(run: Path) -> None:
    raw_path = run / "raw" / "transcript.jsonl"
    raw_bytes = raw_path.read_bytes()
    launch = json.loads((run / "launch.json").read_text())
    events: list[dict] = []
    calls: dict[str, dict] = {}
    order: list[str] = []
    final: dict | None = None
    init: dict = {}
    models: list[str] = []
    for line in raw_bytes.decode().splitlines():
        e = parse_json(line)
        if not isinstance(e, dict):
            continue
        t, sub = e.get("type"), e.get("subtype")
        if t == "system" and sub == "init":
            init = {k: e.get(k) for k in ("model", "claude_code_version")}
            init["mcp_servers"] = [
                {"name": s.get("name"), "status": s.get("status")}
                for s in e.get("mcp_servers") or []
            ]
            init["tools"] = sorted(e.get("tools") or [])
            events.append({"type": "init", **init})
        elif t == "system" and sub == "model_refusal_fallback":
            events.append(
                {
                    "type": "model_fallback",
                    "trigger": e.get("trigger"),
                    "category": e.get("api_refusal_category"),
                    "original_model": e.get("original_model"),
                    "fallback_model": e.get("fallback_model"),
                }
            )
        elif t == "assistant":
            model = e["message"].get("model")
            if model and (not models or models[-1] != model):
                models.append(model)
            for c in e["message"].get("content", []):
                if c.get("type") == "text":
                    events.append({"type": "assistant_text", "model": model, "text": c["text"]})
                elif c.get("type") == "tool_use":
                    name = c["name"].removeprefix(PREFIX)
                    events.append(
                        {"type": "tool_use", "id": c["id"], "tool": name, "input": c["input"]}
                    )
                    calls[c["id"]] = {"tool": name, "input": c["input"]}
                    order.append(c["id"])
        elif t == "user":
            for c in e["message"].get("content", []):
                if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                    continue
                text = result_text(c.get("content"))
                events.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": c["tool_use_id"],
                        "is_error": bool(c.get("is_error")),
                        "content": text,
                    }
                )
                call = calls.setdefault(c["tool_use_id"], {"tool": None, "input": {}})
                body = parse_json(text)
                call["is_error"] = bool(c.get("is_error"))
                call["status"] = body.get("status") if isinstance(body, dict) else "error"
                call["error"] = (
                    body.get("error") if isinstance(body, dict) else {"message": text[:500]}
                )
                call["partial_errors"] = body.get("errors") if isinstance(body, dict) else None
                call["measurements"] = measurements(call["tool"], call["input"], body)
                call["result_bytes"] = len(text.encode())
        elif t == "result":
            final = e
            events.append(
                {
                    "type": "result",
                    "subtype": e.get("subtype"),
                    "is_error": e.get("is_error"),
                    "num_turns": e.get("num_turns"),
                    "duration_ms": e.get("duration_ms"),
                    "result": e.get("result"),
                }
            )

    report = (final or {}).get("result") or ""
    parsed = final_json(report)
    unanswered = [cid for cid in order if "status" not in calls[cid]]
    if final is None:
        completion = "interrupted: no final result event"
    elif final.get("subtype") != "success" or final.get("is_error"):
        completion = f"failed: {final.get('subtype')}"
    elif not isinstance(parsed, dict) or not parsed.get("files"):
        completion = "incomplete: final message has no usable JSON block"
    else:
        completion = "complete"
    tool_calls = [{"n": i + 1, "id": cid, **calls[cid]} for i, cid in enumerate(order)]
    usage = (final or {}).get("usage") or {}
    summary = {
        "completion": completion,
        "session_id": launch.get("session_id"),
        "model_requested": launch.get("model_requested"),
        "models_used": models,
        "model_fallbacks": [ev for ev in events if ev["type"] == "model_fallback"],
        "claude_code_version": init.get("claude_code_version"),
        "mcp_servers": init.get("mcp_servers"),
        "started_utc": launch.get("started_utc"),
        "finished_utc": launch.get("finished_utc"),
        "elapsed_seconds": launch.get("elapsed_seconds"),
        "exit_code": launch.get("exit_code"),
        "num_turns": (final or {}).get("num_turns"),
        "usage_tokens": {
            k: usage.get(k)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        },
        "mcp_calls": len(tool_calls),
        "mcp_calls_without_result": len(unanswered),
        "mcp_calls_failed": sum(1 for c in tool_calls if c.get("status") == "error"),
        "mcp_calls_partial": sum(1 for c in tool_calls if c.get("status") == "partial"),
        "calls_by_tool": {
            t: sum(1 for c in tool_calls if c["tool"] == t)
            for t in sorted({c["tool"] for c in tool_calls if c["tool"]})
        },
        "signal_measurements": sum(len(c.get("measurements") or []) for c in tool_calls),
        "work_dir_bytes_after": launch.get("work_dir_bytes_after"),
        "raw_log_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_log_bytes": len(raw_bytes),
        "raw_log_note": "unmodified local stream-json log; not published (contains hidden "
        "thinking-token events, request IDs and local paths)",
    }
    if completion != "complete":
        report = (
            f"# No final report\n\nThis run is not complete ({completion}). "
            f"{len(tool_calls)} MCP calls were made, {len(unanswered)} without a result. "
            "Nothing here is a finding.\n"
        )
        parsed = {"complete": False, "completion": completion}
    outputs = {
        "transcript.redacted.jsonl": "".join(json.dumps(ev) + "\n" for ev in events),
        "tool_calls.json": json.dumps(tool_calls, indent=1) + "\n",
        "agent_report.md": report.rstrip("\n") + "\n",
        "agent_report.json": json.dumps(parsed, indent=1) + "\n",
        "run.json": json.dumps(summary, indent=2) + "\n",
    }
    for name, text in outputs.items():
        text = redact(text)
        check_clean(text, name)
        (run / name).write_text(text)
    print(json.dumps({k: summary[k] for k in ("completion", "models_used", "mcp_calls")}))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
