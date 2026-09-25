"""`list_files` for configured local roots (source `local`) and S3 buckets (source `s3`).

Local listings open each file's first bytes and observe real sidecars, so every entry has a
distinct state: available, missing (broken link), denied (permission or outside roots),
unsupported (unknown format) or corrupt (content does not match the format). S3 listings
pair objects with index objects listed on the same page; content and range support are
checked when a file is opened, not in the listing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from genomics_mcp.errors import (
    GenomicsError,
    InvalidInputError,
    NotFoundError,
    UnauthorizedError,
)
from genomics_mcp.models import FileFormat, FileRef, Provenance, infer_format
from genomics_mcp.requests import ListFilesRequest
from genomics_mcp.result import OperationOutput, Truncation
from genomics_mcp.storage.formats import needs_index, readiness, sidecar_names
from genomics_mcp.storage.local import resolve_local, resolve_path
from genomics_mcp.storage.s3 import S3Clients, check_bucket, map_client_error, target_for
from genomics_mcp.storage.uris import local_path_from_uri, parse_s3_uri

if TYPE_CHECKING:
    from genomics_mcp.context import OperationContext

INDEX_SUFFIXES = (".bai", ".csi", ".crai", ".tbi", ".fai", ".gzi")


def _is_index_name(name: str) -> bool:
    return name.lower().endswith(INDEX_SUFFIXES)


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not cursor.isdigit():
        raise InvalidInputError("invalid cursor for a local listing")
    return int(cursor)


async def list_local(req: ListFilesRequest, ctx: OperationContext) -> OperationOutput:
    raw = local_path_from_uri(req.accession)
    directory = resolve_path(ctx.settings, raw)
    if not directory.is_dir():
        raise InvalidInputError("accession must be a directory under an allowed root")
    try:
        names = sorted(os.listdir(directory))
    except PermissionError:
        raise UnauthorizedError("permission denied listing the directory", source="local") from None
    wanted = set(req.formats) if req.formats else None
    subdirs: list[str] = []
    data_names: list[str] = []
    for name in names:
        p = Path(raw) / name
        if p.is_dir() and not p.is_symlink():
            subdirs.append(str(p))
        elif _is_index_name(name):
            continue
        else:
            fmt = infer_format(name)
            if wanted is None or (fmt in wanted):
                data_names.append(name)
    paired: set[str] = set()
    start = _offset(req.cursor)
    page = data_names[start : start + ctx.limits.max_records]
    records: list[dict[str, Any]] = []
    for name in (n for n in names if not _is_index_name(n)):
        for cand, _kind in sidecar_names(name, infer_format(name)):
            paired.add(cand)
        paired.add(name + ".gzi")
    for name in page:
        records.append(await _local_entry(ctx, str(Path(raw) / name)))
    unpaired = [str(Path(raw) / n) for n in names if _is_index_name(n) and n not in paired]
    more = start + len(page) < len(data_names)
    truncation = None
    if more:
        truncation = Truncation(
            reason="max_records",
            limit=ctx.limits.max_records,
            returned=len(page),
            available=len(data_names),
            next_cursor=str(start + len(page)),
        )
    return OperationOutput(
        data={
            "records": records,
            "directory": raw,
            "subdirectories": subdirs[:1000],
            "unpaired_indexes": unpaired[:1000],
            "next_cursor": truncation.next_cursor if truncation else None,
        },
        truncation=truncation,
        provenance=[
            Provenance(source="local", url=f"file://{raw}", method="local directory listing")
        ],
    )


async def _local_entry(ctx: OperationContext, path: str) -> dict[str, Any]:
    ref = FileRef(uri=path, source="local")
    fmt = ref.effective_format()
    try:
        resolved = await resolve_local(ctx.settings, ref, strict=False)
    except NotFoundError:
        return {"file": ref, "state": "missing", "reasons": ["file or link target does not exist"]}
    except UnauthorizedError as exc:
        return {"file": ref, "state": "denied", "reasons": [exc.info.message]}
    except GenomicsError as exc:
        return {"file": ref, "state": "corrupt", "reasons": [exc.info.message]}
    reasons = list(resolved.readiness.reasons)
    if fmt is None or fmt is FileFormat.OTHER:
        state = "unsupported"
    elif any("file content looks like" in r for r in reasons):
        state = "corrupt"
    else:
        state = "available"
    return {
        "file": resolved.file,
        "state": state,
        "content": resolved.content_kind,
        "index": {
            "state": resolved.index_state,
            "kind": resolved.index_kind,
            "uri": resolved.index_display,
            "companions": resolved.companion_display,
        },
        "reasons": reasons,
    }


async def list_s3(
    req: ListFilesRequest, ctx: OperationContext, clients: S3Clients
) -> OperationOutput:
    loc = parse_s3_uri(req.accession, allow_prefix=True)
    target = target_for(ctx.settings, req.storage_profile)
    check_bucket(target, loc.bucket)
    prefix = loc.key
    kwargs: dict[str, Any] = {
        "Bucket": loc.bucket,
        "Prefix": prefix,
        "Delimiter": "/",
        "MaxKeys": min(1000, ctx.limits.max_records),
    }
    if req.cursor:
        kwargs["ContinuationToken"] = req.cursor
    if target.requester_pays:
        kwargs["RequestPayer"] = "requester"

    def call() -> dict[str, Any]:
        return clients.client(target).list_objects_v2(**kwargs)

    try:
        resp = await ctx.run_blocking(call)
    except GenomicsError:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a typed storage error
        raise map_client_error(exc, what="bucket listing") from None
    objects = {o["Key"]: o for o in resp.get("Contents", [])}
    wanted = set(req.formats) if req.formats else None
    records: list[dict[str, Any]] = []
    paired: set[str] = set()
    for key, obj in objects.items():
        if _is_index_name(key):
            continue
        fmt = infer_format(key)
        if wanted is not None and fmt not in wanted:
            continue
        index_key = None
        index_kind = None
        for cand, kind in sidecar_names(key, fmt):
            if cand in objects:
                index_key, index_kind = cand, kind
                paired.add(cand)
                break
        gzi = key + ".gzi" if (key + ".gzi") in objects else None
        if gzi:
            paired.add(gzi)
        index_state = "present" if index_key else ("missing" if needs_index(fmt) else "not_needed")
        reasons = ["content, compression and byte-range support are checked when opened"]
        ready = readiness(
            fmt,
            None,
            index_state=index_state,
            range_capable=None,
            local=False,
            extra_reasons=reasons,
        )
        etag = str(obj.get("ETag", "")).strip('"') or None
        ref = FileRef(
            uri=f"s3://{loc.bucket}/{key}",
            index_uri=f"s3://{loc.bucket}/{index_key}" if index_key else None,
            source="s3",
            storage_profile=req.storage_profile,
            size_bytes=int(obj.get("Size", 0)),
            readiness=ready,
            native={"etag": etag, "last_modified": str(obj.get("LastModified", ""))},
        )
        records.append(
            {
                "file": ref,
                "state": "unsupported" if fmt is None else "available",
                "index": {
                    "state": index_state,
                    "kind": index_kind,
                    "uri": ref.index_uri,
                    "companions": {"gzi": f"s3://{loc.bucket}/{gzi}"} if gzi else {},
                },
                "reasons": ready.reasons,
            }
        )
    unpaired = [f"s3://{loc.bucket}/{k}" for k in objects if _is_index_name(k) and k not in paired]
    token = resp.get("NextContinuationToken") if resp.get("IsTruncated") else None
    truncation = (
        Truncation(
            reason="source_page",
            limit=kwargs["MaxKeys"],
            returned=len(records),
            next_cursor=token,
        )
        if token
        else None
    )
    return OperationOutput(
        data={
            "records": records,
            "bucket": loc.bucket,
            "prefix": prefix,
            "subdirectories": [
                f"s3://{loc.bucket}/{p['Prefix']}" for p in resp.get("CommonPrefixes", [])
            ],
            "unpaired_indexes": unpaired,
            "next_cursor": token,
            "notes": ["index pairing only considers objects on the same listing page"],
        },
        truncation=truncation,
        provenance=[
            Provenance(
                source="s3",
                url=f"s3://{loc.bucket}/{prefix}",
                method="S3 ListObjectsV2"
                + (" (anonymous)" if target.anonymous else f" (profile {target.profile_name})"),
            )
        ],
    )
