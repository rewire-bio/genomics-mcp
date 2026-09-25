"""NCBI Datasets v2 client: versioned genome assembly reports and genome packages.

Checked 2026-09-24 (`X-Datasets-Version: 18.37.0`, `X-RateLimit-Limit: 5` without a key):

* `/genome/accession/{acc}/dataset_report` and `/genome/taxon/{taxon}/dataset_report` (native
  `page_size`/`page_token` paging, `total_count`). Unknown accessions or taxa answer 200 with
  `{}`, reported as not_found.
* `/genome/accession/{acc}/download_summary` lists package contents with size estimates;
  `/genome/accession/{acc}/download?include_annotation_type=GENOME_FASTA` streams a ZIP that
  holds `ncbi_dataset/data/{acc}/*.fna` plus `md5sum.txt`. A ZIP is never reported as a
  ready FASTA: `prepare_genome_fasta` is the explicit local preparation step (bounded
  download, safe extraction, MD5 check against md5sum.txt, faidx).
* `/biosample/accession/{acc}/biosample_report` gives BioSample attributes.

Ensembl keys assemblies by INSDC (GCA_) accession. Reports expose the paired GCA/GCF
accession so a separate Ensembl adapter can match exactly; no Ensembl call is made here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import threading
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from genomics_mcp.archives._common.deadline import (
    DEFAULT_OPERATION_TIMEOUT_S,
    DEFAULT_TRANSFER_TIMEOUT_S,
    operation,
)
from genomics_mcp.archives._common.errors import (
    BudgetExceededError,
    InvalidInputError,
    NotFoundError,
    UpstreamError,
)
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy, file_digests
from genomics_mcp.archives._common.models import (
    AccessStatus,
    Artifact,
    Checksum,
    Dataset,
    DatasetDetail,
    EntityKind,
    EntityLink,
    FileFormat,
    FileRef,
    PhenotypeValue,
    Provenance,
    Readiness,
    ReadinessState,
    Reference,
    Sample,
    SourcePage,
    Visibility,
)
from genomics_mcp.archives._common.paging import decode_cursor, encode_cursor, page_size
from genomics_mcp.archives._common.redact import register_secret
from genomics_mcp.archives._common.workspace import artifact_path, open_temp, remove_quietly

SOURCE = "ncbi_datasets"
BASE = "https://api.ncbi.nlm.nih.gov/datasets/v2"
TERMS_URL = "https://www.ncbi.nlm.nih.gov/home/about/policies/"
HOSTS = frozenset({"api.ncbi.nlm.nih.gov"})
DEFAULT_PACKAGE_BUDGET = 100 * 1024 * 1024

_ASM = re.compile(r"^GC[AF]_\d{9}(\.\d+)?$")
_BIOSAMPLE = re.compile(r"^SAM[NED][A-Z]?\d+$")
_PACKAGE_KINDS = {
    "all_genomic_fasta": ("GENOME_FASTA", FileFormat.FASTA),
    "genome_gff": ("GENOME_GFF", FileFormat.GFF3),
    "genome_gtf": ("GENOME_GTF", FileFormat.GTF),
    "cds_fasta": ("CDS_FASTA", FileFormat.FASTA),
    "rna_fasta": ("RNA_FASTA", FileFormat.FASTA),
    "prot_fasta": ("PROT_FASTA", FileFormat.FASTA),
    "sequence_report": ("SEQUENCE_REPORT", FileFormat.OTHER),
}


def _asm(acc: str) -> str:
    a = acc.strip()
    if not _ASM.match(a):
        raise InvalidInputError(
            f"not a GenBank/RefSeq assembly accession: {acc!r}",
            source=SOURCE,
            hint="Expected e.g. GCF_000001405.40 (include the version).",
        )
    return a


class NcbiDatasetsClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: SecretStr | None = None,
        timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S,
        transfer_timeout_s: float = DEFAULT_TRANSFER_TIMEOUT_S,
    ) -> None:
        self.source = SOURCE
        self.operation_timeout_s = timeout_s
        self.transfer_timeout_s = transfer_timeout_s
        self.api_key = api_key
        if api_key is not None:
            register_secret(api_key.get_secret_value())
        self.http = SourceHttp(
            client,
            SourcePolicy(
                SOURCE,
                HOSTS,
                min_interval_s=0.11 if api_key else 0.21,
                timeout_s=timeout_s,
                terms_url=TERMS_URL,
            ),
        )

    def _headers(self) -> dict[str, str]:
        return {"api-key": self.api_key.get_secret_value()} if self.api_key else {}

    async def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[dict, Provenance]:
        url = f"{BASE}{path}"
        data, res = await self.http.get_json(url, params=params, headers=self._headers())
        if not isinstance(data, dict) or not data:
            raise NotFoundError(
                f"NCBI Datasets has no record for {path}",
                source=SOURCE,
                details={"http_status": res.status, "empty_body": True},
            )
        version = res.headers.get("x-datasets-version")
        prov = Provenance(
            source=SOURCE,
            url=res.url,
            method="NCBI Datasets v2 REST",
            terms_url=TERMS_URL,
            source_version=f"datasets {version}" if version else None,
        )
        return data, prov

    # -- conversions --------------------------------------------------------------
    def _dataset(self, r: dict, prov: Provenance) -> Dataset:
        info = r.get("assembly_info") or {}
        links = []
        if r.get("paired_accession"):
            links.append(
                EntityLink(
                    relation="paired_assembly",
                    kind=EntityKind.REFERENCE,
                    accession=r["paired_accession"],
                    source=SOURCE,
                )
            )
        bs = (info.get("biosample") or {}).get("accession")
        if bs:
            links.append(
                EntityLink(
                    relation="biosample", kind=EntityKind.SAMPLE, accession=bs, source=SOURCE
                )
            )
        if info.get("bioproject_accession"):
            links.append(
                EntityLink(
                    relation="bioproject",
                    kind=EntityKind.STUDY,
                    accession=info["bioproject_accession"],
                    source=SOURCE,
                )
            )
        p = prov.model_copy(update={"source_record_id": r.get("accession")})
        native = {
            k: r.get(k)
            for k in (
                "accession",
                "current_accession",
                "paired_accession",
                "source_database",
                "organism",
                "assembly_stats",
                "annotation_info",
            )
        }
        native["assembly_info"] = {k: v for k, v in info.items() if k != "biosample"}
        return Dataset(
            accession=r["accession"],
            source=SOURCE,
            title=info.get("assembly_name"),
            description=info.get("description"),
            access_status=AccessStatus.OPEN,
            assemblies=[r["accession"]]
            + ([info["assembly_name"]] if info.get("assembly_name") else []),
            links=links,
            native=native,
            provenance=[p],
        )

    def _biosample(self, b: dict, prov: Provenance) -> Sample:
        desc = b.get("description") or {}
        org = desc.get("organism") or {}
        phen = [
            PhenotypeValue(name=a["name"], value=a.get("value"), source=SOURCE)
            for a in b.get("attributes") or []
            if isinstance(a, dict) and a.get("name")
        ]
        return Sample(
            accession=b["accession"],
            source=SOURCE,
            title=desc.get("title"),
            description=desc.get("comment"),
            organism=org.get("organism_name"),
            taxon_id=org.get("tax_id"),
            phenotypes=phen,
            native={k: v for k, v in b.items() if k not in ("attributes", "owner", "contacts")},
            provenance=[prov.model_copy(update={"source_record_id": b["accession"]})],
        )

    # -- discovery ----------------------------------------------------------------
    @operation()
    async def search_datasets(
        self, query: str, *, limit: int | None = None, cursor: str | None = None
    ) -> SourcePage[Dataset]:
        """Assembly accession lookup, or genome reports for a taxon name/id (native page tokens)."""
        q = query.strip()
        if not q:
            raise InvalidInputError(
                "NCBI Datasets search needs a taxon or assembly accession", source=SOURCE
            )
        if _ASM.match(q):
            d = await self.describe_dataset(q)
            return SourcePage(items=[d.dataset], total=1, provenance=d.provenance)
        n = page_size(limit, maximum=100)
        state = decode_cursor(cursor, SOURCE)
        if state and state.get("q") != q:
            raise InvalidInputError("cursor belongs to a different query", source=SOURCE)
        params: dict[str, Any] = {"page_size": n}
        if state.get("t"):
            params["page_token"] = state["t"]
        data, prov = await self._get(f"/genome/taxon/{quote(q, safe='')}/dataset_report", params)
        items = [self._dataset(r, prov) for r in data.get("reports", [])]
        token = data.get("next_page_token")
        return SourcePage(
            items=items,
            total=data.get("total_count"),
            provenance=[prov],
            next_cursor=encode_cursor(SOURCE, q=q, t=token) if token else None,
        )

    @operation()
    async def describe_dataset(self, accession: str) -> DatasetDetail:
        acc = _asm(accession)
        data, prov = await self._get(f"/genome/accession/{acc}/dataset_report")
        reports = data.get("reports") or []
        if not reports:
            raise NotFoundError(f"NCBI Datasets has no assembly {acc}", source=SOURCE)
        r = reports[0]
        ds = self._dataset(r, prov)
        info = r.get("assembly_info") or {}
        insdc = r["accession"] if r["accession"].startswith("GCA_") else r.get("paired_accession")
        ref = Reference(
            accession=r["accession"],
            source=SOURCE,
            assembly=r["accession"],
            title=info.get("assembly_name"),
            description=info.get("description"),
            links=list(ds.links),
            provenance=ds.provenance,
        )
        warnings = []
        if r["accession"] != acc:
            warnings.append(f"{acc} resolved to {r['accession']} by NCBI Datasets")
        if info.get("assembly_status") and info["assembly_status"] != "current":
            warnings.append(f"assembly status is {info['assembly_status']!r}")
        related = {
            "reference": ref.model_dump(mode="json"),
            "assembly_status": info.get("assembly_status"),
            "release_date": info.get("release_date"),
            "annotation_release": (r.get("annotation_info") or {}).get("name"),
            "insdc_accession": insdc,
            "ensembl_linkage": (
                "Ensembl identifies assemblies by INSDC accession; match "
                f"{insdc} exactly (no name-based matching)"
            )
            if insdc
            else None,
        }
        return DatasetDetail(dataset=ds, related=related, provenance=[prov], warnings=warnings)

    @operation()
    async def list_files(
        self, accession: str, *, limit: int | None = None, cursor: str | None = None
    ) -> SourcePage[FileRef]:
        """Genome package contents. Each entry is a ZIP package endpoint, not a region-ready file."""
        acc = _asm(accession)
        data, prov = await self._get(
            f"/genome/accession/{acc}/download_summary",
            {"include_annotation_type": [v[0] for v in _PACKAGE_KINDS.values()]},
        )
        items = []
        for key, info in (data.get("available_files") or {}).items():
            if key not in _PACKAGE_KINDS or not isinstance(info, dict):
                continue
            ann, fmt = _PACKAGE_KINDS[key]
            url = f"{BASE}/genome/accession/{acc}/download?include_annotation_type={ann}"
            reason = (
                "ZIP package; prepare_genome_fasta downloads, verifies and extracts the FASTA and "
                "builds a .fai"
                if ann == "GENOME_FASTA"
                else "ZIP package; extract locally before use (not prepared by this client)"
            )
            items.append(
                FileRef(
                    uri=url,
                    format=FileFormat.OTHER,
                    source=SOURCE,
                    accession=acc,
                    access_status=AccessStatus.OPEN,
                    visibility=Visibility.PUBLIC,
                    relationships=[
                        EntityLink(
                            relation="package_of",
                            kind=EntityKind.REFERENCE,
                            accession=acc,
                            source=SOURCE,
                        )
                    ],
                    readiness=Readiness(state=ReadinessState.DOWNLOAD_REQUIRED, reasons=[reason]),
                    native={
                        "container": "zip",
                        "content": key,
                        "content_format": fmt.value,
                        "annotation_type": ann,
                        "file_count": info.get("file_count"),
                        "size_mb_estimate": info.get("size_mb"),
                        "resource_updated_on": data.get("resource_updated_on"),
                    },
                )
            )
        return SourcePage(items=items, total=len(items), provenance=[prov])

    @operation()
    async def list_samples(
        self, accession: str, *, limit: int | None = None, cursor: str | None = None
    ) -> SourcePage[Sample]:
        """The BioSample an assembly report names, if any. Reference assemblies often have none."""
        acc = _asm(accession)
        data, prov = await self._get(f"/genome/accession/{acc}/dataset_report")
        reports = data.get("reports") or []
        if not reports:
            raise NotFoundError(f"NCBI Datasets has no assembly {acc}", source=SOURCE)
        bs = (reports[0].get("assembly_info") or {}).get("biosample")
        if not bs or not bs.get("accession"):
            return SourcePage(
                items=[],
                total=0,
                provenance=[prov],
                warnings=["the assembly report names no BioSample"],
            )
        sample = self._biosample(bs, prov)
        sample.links.append(
            EntityLink(
                relation="sample_of_assembly",
                kind=EntityKind.REFERENCE,
                accession=reports[0]["accession"],
                source=SOURCE,
            )
        )
        return SourcePage(items=[sample], total=1, provenance=[prov])

    @operation()
    async def get_sample_metadata(self, accession: str) -> Sample:
        acc = accession.strip()
        if not _BIOSAMPLE.match(acc):
            raise InvalidInputError(f"not a BioSample accession: {acc!r}", source=SOURCE)
        data, prov = await self._get(f"/biosample/accession/{acc}/biosample_report")
        reports = data.get("reports") or []
        if not reports:
            raise NotFoundError(f"NCBI has no BioSample {acc}", source=SOURCE)
        return self._biosample(reports[0], prov)

    # -- preparation --------------------------------------------------------------
    @operation("transfer_timeout_s")
    async def prepare_genome_fasta(
        self, accession: str, *, workspace: Path, budget_bytes: int = DEFAULT_PACKAGE_BUDGET
    ) -> Artifact:
        """Explicit preparation: download the GENOME_FASTA package, verify, extract and faidx.

        `budget_bytes` bounds both the ZIP download and the extracted FASTA size."""

        acc = _asm(accession)
        if "." not in acc:
            raise InvalidInputError(
                "preparation needs a versioned assembly accession", source=SOURCE
            )
        if budget_bytes <= 0:
            raise InvalidInputError("budget_bytes must be positive", source=SOURCE)
        summary, _ = await self._get(
            f"/genome/accession/{acc}/download_summary", {"include_annotation_type": "GENOME_FASTA"}
        )
        est = ((summary.get("available_files") or {}).get("all_genomic_fasta") or {}).get("size_mb")
        if isinstance(est, int | float) and est * 1024 * 1024 > budget_bytes:
            raise BudgetExceededError(
                f"genome FASTA is about {est:g} MB, over the {budget_bytes}-byte budget",
                source=SOURCE,
                hint="Pass an explicit larger budget.",
            )
        url = f"{BASE}/genome/accession/{acc}/download"
        zpath = artifact_path(workspace, f"{acc}.genome_fasta.zip")
        dl = await self.http.download(
            url,
            zpath,
            budget_bytes=budget_bytes,
            headers=self._headers(),
            params={"include_annotation_type": "GENOME_FASTA"},
            timeout_s=self.transfer_timeout_s,
        )
        dest = artifact_path(workspace, f"{acc}.fna")
        stop = threading.Event()
        try:
            # ZIP and extracted FASTA share one budget (both exist on disk during extraction).
            members, verified = await asyncio.to_thread(
                _extract_fasta, zpath, acc, dest, budget_bytes - dl.size_bytes, stop
            )
            # The .fai shares the budget and is bounded before anything is written.
            fai_bound = await asyncio.to_thread(_fai_upper_bound, dest, stop)
            left = budget_bytes - dl.size_bytes - dest.stat().st_size
            if fai_bound > left:
                raise BudgetExceededError(
                    f"the FASTA index would need up to {fai_bound} bytes; {max(left, 0)} bytes "
                    "of the budget remain",
                    source=SOURCE,
                    hint="Pass an explicit larger budget.",
                    details={"budget_bytes": budget_bytes, "fai_upper_bound": fai_bound},
                )
            # Join the indexing thread even if this call is cancelled, so cleanup below never
            # races a late .fai write.
            indexing = asyncio.ensure_future(asyncio.to_thread(_faidx_bounded, dest, fai_bound))
            try:
                await asyncio.shield(indexing)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    await indexing
                raise
        except BaseException:
            stop.set()  # an interrupted extraction thread stops at its next chunk and removes its temp
            remove_quietly(dest, f"{dest}.fai")
            raise
        finally:
            zpath.unlink(missing_ok=True)
        md5, sha = file_digests(dest)
        origin = FileRef(
            uri=f"{url}?include_annotation_type=GENOME_FASTA",
            format=FileFormat.OTHER,
            source=SOURCE,
            accession=acc,
            access_status=AccessStatus.OPEN,
            visibility=Visibility.PUBLIC,
            size_bytes=dl.size_bytes,
            checksums=[Checksum(algorithm="sha256", value=dl.sha256)],
            native={"container": "zip", "members": members},
        )
        prov = Provenance(
            source=SOURCE,
            source_record_id=acc,
            url=f"{url}?include_annotation_type=GENOME_FASTA",
            method="NCBI Datasets v2 genome package",
            terms_url=TERMS_URL,
            transformations=[
                f"downloaded {dl.size_bytes}-byte ZIP within budget",
                f"extracted {len(members)} FASTA member(s) in name order",
                "member MD5s verified against md5sum.txt"
                if verified
                else "package had no md5sum.txt entries for the FASTA members",
                "ZIP removed",
                "indexed locally with pysam.faidx",
            ],
        )
        return Artifact(
            path=str(dest),
            size_bytes=dest.stat().st_size,
            checksums=[
                Checksum(algorithm="md5", value=md5),
                Checksum(algorithm="sha256", value=sha),
            ],
            checksum_verified=verified,
            format=FileFormat.FASTA,
            index_path=f"{dest}.fai",
            origin=origin,
            provenance=prov,
        )


MAX_MANIFEST_BYTES = 1024 * 1024
_SCAN_CHUNK = 8 * 1024 * 1024


def _faidx_bounded(dest: Path, bound: int) -> None:
    import pysam

    pysam.faidx(str(dest))
    if os.path.getsize(f"{dest}.fai") > bound:
        raise UpstreamError("FASTA index larger than its computed bound", source=SOURCE)


def _fai_upper_bound(path: Path, stop: threading.Event | None = None) -> int:
    """Upper bound on the samtools .fai size for `path`, from its header lines only.

    Each .fai line is `name\tlength\toffset\tlinebases\tlinewidth\n`; every number is at most
    the file size, so a record needs at most len(name) + 4 * digits(file size) + 5 bytes.
    Chunked C-speed scanning; checks `stop` between chunks."""
    digits = len(str(max(1, path.stat().st_size)))
    total = 0

    def add(line: bytes) -> None:
        nonlocal total
        name = line[1:].split(None, 1)
        total += (len(name[0]) if name else 0) + 4 * digits + 5

    carry = b""
    with path.open("rb") as fh:
        while chunk := fh.read(_SCAN_CHUNK):
            if stop is not None and stop.is_set():
                raise UpstreamError("preparation was cancelled", source=SOURCE)
            buf = carry + chunk
            cut = buf.rfind(b"\n") + 1
            complete, carry = buf[:cut], buf[cut:]
            # `complete` starts at a line start and ends with a newline.
            pos = 0 if complete.startswith(b">") else -1
            nxt = complete.find(b"\n>")
            while pos >= 0 or nxt >= 0:
                if pos < 0:
                    pos, nxt = nxt + 1, complete.find(b"\n>", nxt + 1)
                    continue
                add(complete[pos : complete.find(b"\n", pos)])
                pos = -1
    if carry.startswith(b">"):
        add(carry)
    return total


def _read_member_bounded(zf: zipfile.ZipFile, name: str, cap: int) -> bytes:
    """Read a small ZIP member, stopping at `cap` decompressed bytes whatever the header claims."""
    if zf.getinfo(name).file_size > cap:
        raise BudgetExceededError(f"package {name} is larger than {cap} bytes", source=SOURCE)
    with zf.open(name) as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise BudgetExceededError(f"package {name} decompresses past {cap} bytes", source=SOURCE)
    return data


def _extract_fasta(
    zpath: Path, acc: str, dest: Path, budget: int, stop: threading.Event | None = None
) -> tuple[list[str], bool]:
    """Concatenate `ncbi_dataset/data/<acc>/*.fna` members into `dest` (bounded, no path use)."""
    import os

    pattern = re.compile(rf"^ncbi_dataset/data/{re.escape(acc)}/[^/]+\.fna$")
    try:
        zf = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile as exc:
        raise UpstreamError("NCBI Datasets package is not a valid ZIP", source=SOURCE) from exc
    with zf:
        names = sorted(n for n in zf.namelist() if pattern.match(n))
        if not names:
            raise UpstreamError(f"package has no genomic FASTA for {acc}", source=SOURCE)
        declared = sum(zf.getinfo(n).file_size for n in names)
        if declared > budget:
            raise BudgetExceededError(
                f"extracted FASTA would be {declared} bytes, over the budget",
                source=SOURCE,
                details={"budget_bytes": budget},
            )
        sums: dict[str, str] = {}
        if "md5sum.txt" in zf.namelist():
            for line in (
                _read_member_bounded(zf, "md5sum.txt", MAX_MANIFEST_BYTES)
                .decode("utf-8", "replace")
                .splitlines()
            ):
                parts = line.split()
                if len(parts) == 2:
                    sums[parts[1].lstrip("*")] = parts[0].lower()
        verified = all(n in sums for n in names)
        fd, tmp = open_temp(dest)
        written = 0
        try:
            with os.fdopen(fd, "wb") as out:
                for n in names:
                    h = hashlib.md5(usedforsecurity=False)
                    with zf.open(n) as src:
                        for chunk in iter(lambda: src.read(1 << 20), b""):
                            if stop is not None and stop.is_set():
                                raise UpstreamError("preparation cancelled", source=SOURCE)
                            written += len(chunk)
                            if written > budget:
                                raise BudgetExceededError(
                                    "extracted FASTA exceeded the budget", source=SOURCE
                                )
                            h.update(chunk)
                            out.write(chunk)
                    if n in sums and h.hexdigest() != sums[n]:
                        raise UpstreamError(f"{n} failed its md5sum.txt check", source=SOURCE)
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return names, verified
