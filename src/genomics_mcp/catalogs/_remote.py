"""Readiness checks for public remote files, based on an actual byte-range probe."""

from __future__ import annotations

from genomics_mcp.archives._common.http import SourceHttp
from genomics_mcp.archives._common.models import (
    Compression,
    FileFormat,
    FileRef,
    Readiness,
    ReadinessState,
    sniff_compression,
    utcnow,
)

MAGIC = {FileFormat.BIGWIG: b"\x26\xfc\x8f\x88", FileFormat.BIGBED: b"\xeb\xf2\x89\x87"}


async def verify_remote(http: SourceHttp, file: FileRef) -> FileRef:
    """Probe `bytes=0-63` at `file.uri`; READY only for bigWig/bigBed with honoured ranges and magic.

    Other formats keep their listed state, updated with the observed size and compression.
    """
    out = file.model_copy(deep=True)
    probe = await http.probe_range(out.uri, nbytes=64)
    reasons = [f"range probe HTTP {probe.status}"]
    state = out.readiness.state if out.readiness else ReadinessState.UNKNOWN
    if probe.total_size is not None and out.size_bytes is None:
        out.size_bytes = probe.total_size
    if probe.total_size is not None and out.size_bytes != probe.total_size:
        state = ReadinessState.UNKNOWN
        reasons.append(f"server size {probe.total_size} differs from catalog size {out.size_bytes}")
    elif not probe.range_supported:
        state = ReadinessState.DOWNLOAD_REQUIRED
        reasons.append("byte ranges not honoured; download before querying")
    elif out.format in MAGIC:
        if probe.head[:4] == MAGIC[out.format]:
            state = ReadinessState.READY
            reasons.append(f"{out.format.value} magic verified; anonymous range reads honoured")
            if out.assembly is None:
                reasons.append("the catalog reports no structured assembly; supply it explicitly")
        else:
            state = ReadinessState.UNSUPPORTED
            reasons.append(f"content does not start with {out.format.value} magic")
    elif out.compression in (Compression.UNKNOWN, None) and probe.head[:2] == b"\x1f\x8b":
        out.compression = sniff_compression(probe.head)
        reasons.append(f"compression observed: {out.compression.value}")
        if out.compression == Compression.GZIP:
            reasons.append(
                "ordinary gzip is not BGZF; recompress and index locally for region queries"
            )
    out.readiness = Readiness(state=state, reasons=reasons, checked_at=utcnow())
    return out
