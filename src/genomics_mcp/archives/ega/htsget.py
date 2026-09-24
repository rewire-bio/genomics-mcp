"""Bounded htsget retrieval for EGA (GA4GH htsget 1.x tickets).

Protocol notes established against the live EGA service (2026-09-24):

* EGA tickets name the block class `urlClass` (the spec says `class`); both are read.
* EGA data blocks use `data:base64,<payload>` with no media type or `;` before `base64`.
  Strict RFC 2397 would treat that as a non-base64 media type; it is base64. Standard
  `data:<type>;base64,<payload>` and percent-encoded (non-base64) data URLs are also
  handled. Each block is decoded exactly once, then blocks are concatenated in order.
* The concatenated BGZF blocks contain whole records that merely share BGZF blocks with
  the interval, so records outside the interval are present. Results are post-filtered
  by alignment overlap (reference_start/reference_end from CIGAR) or variant span.

htsget query coordinates are 0-based half-open, the same as `Interval`, so `start`/`end`
are passed through unchanged (no +1).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote_to_bytes, urlsplit

from pydantic import Field

from genomics_mcp.archives._common.errors import (
    BudgetExceededError,
    InvalidInputError,
    UnsupportedError,
    UpstreamError,
)
from genomics_mcp.archives._common.http import SourceHttp, _origin
from genomics_mcp.archives._common.models import (
    Artifact,
    Checksum,
    FileRef,
    Interval,
    Provenance,
    _Model,
)
from genomics_mcp.archives._common.redact import redact_url
from genomics_mcp.archives._common.workspace import write_atomic

MAX_BLOCKS = 4096
DEFAULT_REGION_BUDGET = 16 * 1024 * 1024
DEFAULT_MAX_REGION_BP = 1_000_000
DEFAULT_MAX_RECORDS = 10_000
HTSGET_ACCEPT = "application/vnd.ga4gh.htsget.v1.0.0+json, application/json;q=0.9"
# Ticket-supplied headers forwarded to a block URL. Anything else is dropped.
_FORWARDABLE_HEADERS = {"range", "authorization", "accept", "accept-encoding"}


@dataclass
class Block:
    kind: Literal["data", "remote"]
    url_class: str | None
    url: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass
class Ticket:
    format: str | None
    blocks: list[Block]
    md5: str | None = None


def ticket_headers(bearer: str) -> dict[str, str]:
    return {"Accept": HTSGET_ACCEPT, "Authorization": f"Bearer {bearer}"}


def parse_ticket(payload: Any) -> Ticket:
    if not isinstance(payload, dict) or not isinstance(payload.get("htsget"), dict):
        raise UpstreamError("htsget response is not a ticket", source="ega")
    body = payload["htsget"]
    urls = body.get("urls")
    if not isinstance(urls, list) or not urls:
        raise UpstreamError("htsget ticket contains no URLs", source="ega")
    if len(urls) > MAX_BLOCKS:
        raise BudgetExceededError(
            f"htsget ticket has {len(urls)} blocks (limit {MAX_BLOCKS})", source="ega",
            hint="Request a smaller interval.",
        )
    blocks = []
    for item in urls:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise UpstreamError("htsget ticket URL entry is malformed", source="ega")
        url = item["url"]
        klass = item.get("class", item.get("urlClass"))
        headers = item.get("headers") or {}
        if not isinstance(headers, dict):
            raise UpstreamError("htsget ticket headers are malformed", source="ega")
        kind: Literal["data", "remote"] = "data" if url.startswith("data:") else "remote"
        blocks.append(Block(kind, klass if isinstance(klass, str) else None, url,
                            {str(k): str(v) for k, v in headers.items()}))
    fmt = body.get("format")
    return Ticket(fmt if isinstance(fmt, str) else None, blocks, body.get("md5"))


def decode_data_url(url: str, *, max_bytes: int) -> bytes:
    """Decode one RFC 2397 data URL, accepting EGA's `data:base64,` variant. Decodes once."""
    if not url.startswith("data:") or "," not in url:
        raise UpstreamError("malformed data URL in htsget ticket", source="ega")
    header, payload = url[5:].split(",", 1)
    params = [p.strip().lower() for p in header.split(";")] if header else []
    is_b64 = bool(params) and params[-1] == "base64"
    estimate = (len(payload) * 3) // 4 if is_b64 else len(payload)
    if estimate > max_bytes + 3:
        raise BudgetExceededError(
            f"htsget data block (~{estimate} bytes) exceeds the remaining budget ({max_bytes} bytes)",
            source="ega", hint="Pass a larger explicit budget or a smaller interval.",
        )
    if is_b64:
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UpstreamError("htsget data block is not valid base64", source="ega") from exc
    else:
        data = unquote_to_bytes(payload)
    if len(data) > max_bytes:
        raise BudgetExceededError(
            f"htsget data exceeds the remaining budget ({max_bytes} bytes)", source="ega",
            hint="Pass a larger explicit budget or a smaller interval.",
        )
    return data


class BlockInfo(_Model):
    index: int
    kind: Literal["data", "remote"]
    url_class: str | None = None
    bytes: int
    host: str | None = None


class HeaderSummary(_Model):
    n_references: int
    contig_present: bool
    contig_length: int | None = None
    sq_assembly_tags: list[str] = Field(default_factory=list, description="Distinct @SQ AS values.")
    sq_md5: str | None = Field(default=None, description="@SQ M5 of the queried contig, if present.")


class RegionResult(_Model):
    """Bounded htsget retrieval: the assembled local artifact plus post-filtered records."""

    accession: str
    interval: Interval
    endpoint: Literal["reads", "variants"]
    format: str
    artifact: Artifact
    blocks: list[BlockInfo]
    header: HeaderSummary
    records: list[dict[str, Any]]
    records_in_blocks: int = Field(description="All records decoded from the returned blocks.")
    records_overlapping: int = Field(description="Records overlapping the interval after post-filtering.")
    records_skipped_unplaced: int = 0
    truncated: bool = False
    filters: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)


async def fetch_blocks(
    http: SourceHttp,
    ticket: Ticket,
    *,
    ticket_url: str,
    bearer: str | None,
    budget_bytes: int,
    allowed_hosts: frozenset[str],
) -> tuple[bytes, list[BlockInfo]]:
    """Resolve every block within `budget_bytes`. Our bearer token is only sent to the ticket origin."""
    parts: list[bytes] = []
    infos: list[BlockInfo] = []
    used = 0
    ticket_origin = _origin(ticket_url)
    if budget_bytes <= 0:
        raise InvalidInputError("budget_bytes must be positive", source="ega")
    for i, block in enumerate(ticket.blocks):
        remaining = budget_bytes - used
        if remaining <= 0:
            raise BudgetExceededError(
                f"htsget ticket needs more than the {budget_bytes}-byte budget "
                f"({len(ticket.blocks) - i} block(s) not fetched)", source="ega",
                hint="Pass a larger explicit budget or a smaller interval.",
            )
        if block.kind == "data":
            data = decode_data_url(block.url, max_bytes=remaining)
            host = None
        else:
            scheme = urlsplit(block.url).scheme
            if scheme != "https":
                raise UpstreamError("htsget block URL is not https", source="ega",
                                    details={"url": redact_url(block.url)})
            headers = {k: v for k, v in block.headers.items() if k.lower() in _FORWARDABLE_HEADERS}
            has_auth = any(k.lower() == "authorization" for k in headers)
            if bearer and not has_auth and _origin(block.url) == ticket_origin:
                headers["Authorization"] = f"Bearer {bearer}"
            res = await http.request("GET", block.url, headers=headers, ok=(200, 206),
                                     max_body=remaining, allowed_hosts=allowed_hosts)
            data, host = res.body, urlsplit(block.url).hostname
        used += len(data)
        if used > budget_bytes:
            raise BudgetExceededError(f"htsget blocks exceed the {budget_bytes}-byte budget",
                                      source="ega")
        parts.append(data)
        infos.append(BlockInfo(index=i, kind=block.kind, url_class=block.url_class, bytes=len(data),
                               host=host))
    return b"".join(parts), infos


def verify_ticket_md5(ticket: Ticket, payload: bytes) -> str | None:
    """Check an MD5 supplied in the ticket against the reconstructed bytes. Returns the MD5 if checked."""
    if not ticket.md5:
        return None
    observed = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    if observed != str(ticket.md5).lower():
        raise UpstreamError("htsget payload does not match the ticket MD5", source="ega",
                            details={"ticket_md5": str(ticket.md5), "observed_md5": observed})
    return observed


def region_artifact_name(accession: str, interval: Interval, ext: str) -> str:
    """Opaque filename: validated accession + hash of the interval. Contigs never become paths."""
    key = f"{interval.assembly}\t{interval.contig}\t{interval.start}\t{interval.end}".encode()
    return f"{accession}.region-{hashlib.sha256(key).hexdigest()[:16]}.{ext}"


def write_private(path: Path, data: bytes) -> tuple[str, str]:
    write_atomic(path, data)
    return hashlib.sha256(data).hexdigest(), hashlib.md5(data, usedforsecurity=False).hexdigest()


def _read_record(r: Any) -> dict[str, Any]:
    return {
        "query_name": r.query_name,
        "flag": r.flag,
        "contig": r.reference_name,
        "start": r.reference_start,
        "end": r.reference_end,
        "mapping_quality": r.mapping_quality,
        "cigar": r.cigarstring,
        "is_reverse": r.is_reverse,
        "is_secondary": r.is_secondary,
        "is_supplementary": r.is_supplementary,
        "is_duplicate": r.is_duplicate,
        "is_qcfail": r.is_qcfail,
        "is_paired": r.is_paired,
        "mate_contig": r.next_reference_name if r.is_paired else None,
        "mate_start": r.next_reference_start if r.is_paired else None,
    }


def filter_reads(path: Path, interval: Interval, max_records: int) -> dict[str, Any]:
    """Decode an htsget BAM slice and keep alignments overlapping the 0-based half-open interval."""
    import pysam

    out: dict[str, Any] = {"records": [], "total": 0, "overlapping": 0, "unplaced": 0}
    with pysam.AlignmentFile(str(path), "rb", check_sq=False) as fh:
        header = fh.header.to_dict()
        sq = header.get("SQ", [])
        match = next((s for s in sq if s.get("SN") == interval.contig), None)
        out["header"] = HeaderSummary(
            n_references=len(sq), contig_present=match is not None,
            contig_length=match.get("LN") if match else None,
            sq_assembly_tags=sorted({s["AS"] for s in sq if s.get("AS")}),
            sq_md5=match.get("M5") if match else None,
        )
        for r in fh.fetch(until_eof=True):
            out["total"] += 1
            if r.is_unmapped or r.reference_end is None:
                out["unplaced"] += 1
                continue
            if (r.reference_name == interval.contig and r.reference_start < interval.end
                    and r.reference_end > interval.start):
                out["overlapping"] += 1
                if len(out["records"]) < max_records:
                    out["records"].append(_read_record(r))
    return out


def filter_variants(path: Path, interval: Interval, max_records: int) -> dict[str, Any]:
    """Decode an htsget VCF slice; keep records whose REF span overlaps the interval.

    Sample genotypes are not returned here (private data; E4 readers own genotype output)."""
    import pysam

    out: dict[str, Any] = {"records": [], "total": 0, "overlapping": 0, "unplaced": 0}
    with pysam.VariantFile(str(path)) as fh:
        contigs = fh.header.contigs
        c = contigs.get(interval.contig)
        assemblies = set(re.findall(r"^##contig=<[^\n]*?assembly=([^,>]+)", str(fh.header), re.M))
        out["header"] = HeaderSummary(
            n_references=len(contigs), contig_present=c is not None,
            contig_length=c.length if c is not None else None,
            sq_assembly_tags=sorted(assemblies),
        )
        out["n_samples"] = len(fh.header.samples)
        for rec in fh:
            out["total"] += 1
            if rec.contig == interval.contig and rec.start < interval.end and rec.stop > interval.start:
                out["overlapping"] += 1
                if len(out["records"]) < max_records:
                    out["records"].append({
                        "contig": rec.contig, "pos": rec.pos, "start": rec.start, "end": rec.stop,
                        "id": rec.id, "ref": rec.ref, "alts": list(rec.alts or ()),
                        "qual": rec.qual, "filter": list(rec.filter.keys()),
                    })
    return out


async def postfilter(path: Path, endpoint: str, interval: Interval, max_records: int) -> dict[str, Any]:
    fn = filter_reads if endpoint == "reads" else filter_variants
    try:
        return await asyncio.to_thread(fn, path, interval, max_records)
    except (OSError, ValueError) as exc:
        raise UpstreamError(
            f"htsget payload could not be decoded as {'BAM' if endpoint == 'reads' else 'VCF'}",
            source="ega", details={"error": type(exc).__name__},
        ) from exc


def validate_region(interval: Interval, fmt: str, endpoint: str, max_region_bp: int) -> str:
    fmt_u = fmt.upper()
    if endpoint == "reads" and fmt_u != "BAM":
        raise UnsupportedError(
            "EGA htsget reads are retrieved as BAM; CRAM output needs an explicit, checksum-matched "
            "reference and is not requested here", source="ega", hint="Use format='BAM'.",
        )
    if endpoint == "variants" and fmt_u != "VCF":
        raise UnsupportedError("EGA htsget variants are retrieved as VCF", source="ega")
    if interval.length > max_region_bp:
        raise InvalidInputError(
            f"interval is {interval.length} bp, over the {max_region_bp} bp region limit",
            source="ega", hint="Narrow the interval or pass an explicit max_region_bp.",
            details={"length": interval.length, "max_region_bp": max_region_bp},
        )
    return fmt_u


def origin_file(accession: str, fmt: str) -> FileRef:
    return FileRef(uri=f"ega://{accession}", source="ega", accession=accession,
                   access_status="authorized", visibility="private", format=fmt.lower())


def artifact_for(path: Path, size: int, sha256: str, md5: str, fmt: str, origin: FileRef,
                 prov: Provenance, *, verified: bool = False) -> Artifact:
    return Artifact(
        path=str(path), size_bytes=size,
        checksums=[Checksum(algorithm="sha256", value=sha256), Checksum(algorithm="md5", value=md5)],
        checksum_verified=verified, format=fmt.lower(), origin=origin, provenance=prov,
    )
