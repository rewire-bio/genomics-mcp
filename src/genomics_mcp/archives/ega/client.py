"""EGA client: public metadata API, authorised download metadata, htsget regions and bounded files.

Endpoints (checked 2026-09-24):

* Public metadata, anonymous: https://metadata.ega-archive.org (OpenAPI /spec/specs.yaml,
  `EGA-API-Version: 3`). Lists page with `limit`/`offset` and answer 206 for partial pages;
  `HEAD` returns `EGA-API-Total-Count`. Unknown accessions answer **200 with an empty body**,
  which is reported as not_found. `/datasets/{id}/mappings/*` need a token.
* Download API with explicit auth: https://ega.ebi.ac.uk:8443/v2 — `/metadata/...` (display
  names, index file ids), `/htsget/{reads,variants}/{EGAF}` and `/files/{EGAF}?destinationFormat=plain`.
  401 = invalid/missing authentication, 403 = permission denied, 404 = unknown accession.

The public API has no free-text search; `search_datasets` accepts accessions only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from genomics_mcp.archives._common.deadline import (
    DEFAULT_OPERATION_TIMEOUT_S,
    DEFAULT_TRANSFER_TIMEOUT_S,
    operation,
)
from genomics_mcp.archives._common.errors import (
    InvalidInputError,
    NotFoundError,
    PreparationRequiredError,
    UnauthorizedError,
    UnsupportedError,
    UpstreamError,
)
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy
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
    Interval,
    PhenotypeValue,
    Provenance,
    Readiness,
    ReadinessState,
    Sample,
    SourcePage,
    Study,
    Visibility,
    infer_format,
    utcnow,
)
from genomics_mcp.archives._common.paging import (
    MAX_RECORDS,
    next_offset_cursor,
    offset_from,
    page_size,
)
from genomics_mcp.archives._common.workspace import artifact_path
from genomics_mcp.archives.ega import htsget as hg
from genomics_mcp.archives.ega.auth import EgaAuth

SOURCE = "ega"
METADATA_BASE = "https://metadata.ega-archive.org"
DATA_BASE = "https://ega.ebi.ac.uk:8443/v2"
TERMS_URL = "https://ega-archive.org/legal-notice/"
METADATA_HOSTS = frozenset({"metadata.ega-archive.org"})
DATA_HOSTS = frozenset({"ega.ebi.ac.uk"})
DEFAULT_FILE_BUDGET = 100 * 1024 * 1024

_ACC = re.compile(r"^EGA([SDNFXRZPC])\d{11}$")
_KIND = {"S": "study", "D": "dataset", "N": "sample", "F": "file", "X": "experiment", "R": "run",
         "Z": "analysis", "P": "policy", "C": "dac"}
_INDEX_EXT = {"bai", "crai", "tbi", "csi"}
_REGION_FORMATS = {FileFormat.BAM: "reads", FileFormat.CRAM: "reads", FileFormat.VCF: "variants",
                   FileFormat.BCF: "variants"}


def accession_kind(acc: str) -> str:
    m = _ACC.match(acc.strip())
    if not m:
        raise InvalidInputError(f"not an EGA accession: {acc!r}", source=SOURCE,
                                hint="Expected EGAD/EGAS/EGAN/EGAF... followed by 11 digits.")
    return _KIND[m.group(1)]


def _require(acc: str, kind: str) -> str:
    acc = acc.strip()
    got = accession_kind(acc)
    if got != kind:
        raise InvalidInputError(f"{acc} is an EGA {got} accession; this operation needs a {kind}",
                                source=SOURCE)
    return acc


def _access(access_type: Any) -> AccessStatus:
    v = str(access_type or "").lower()
    if v == "controlled":
        return AccessStatus.CONTROLLED
    if v in ("open", "public"):
        return AccessStatus.OPEN
    return AccessStatus.UNKNOWN


class EgaClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        auth: EgaAuth | None = None,
        metadata_base: str = METADATA_BASE,
        data_base: str = DATA_BASE,
        extra_block_hosts: frozenset[str] = frozenset(),
        timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S,
        transfer_timeout_s: float = DEFAULT_TRANSFER_TIMEOUT_S,
    ) -> None:
        self.auth = auth
        self.source = SOURCE
        self.operation_timeout_s = timeout_s
        self.transfer_timeout_s = transfer_timeout_s
        self.metadata_base = metadata_base.rstrip("/")
        self.data_base = data_base.rstrip("/")
        self.meta = SourceHttp(client, SourcePolicy(
            SOURCE, METADATA_HOSTS, min_interval_s=0.2, timeout_s=timeout_s, terms_url=TERMS_URL))
        self.data = SourceHttp(client, SourcePolicy(
            SOURCE, DATA_HOSTS, min_interval_s=0.2, timeout_s=timeout_s, terms_url=TERMS_URL))
        self.block_hosts = DATA_HOSTS | extra_block_hosts

    # -- plumbing -----------------------------------------------------------------
    def _prov(self, url: str, record: str | None, method: str, res=None, **kw: Any) -> Provenance:
        version = None
        if res is not None and "ega-api-version" in res.headers:
            version = f"metadata-api v{res.headers['ega-api-version']}"
        return Provenance(source=SOURCE, source_record_id=record, url=url, method=method,
                          source_version=version, terms_url=TERMS_URL, **kw)

    async def _one(self, path: str) -> tuple[dict, Provenance]:
        url = f"{self.metadata_base}{path}"
        data, res = await self.meta.get_json(url)
        if not data or not isinstance(data, dict):
            # EGA answers 200 with an empty body for accessions it does not know.
            raise NotFoundError(f"EGA has no record at {path}", source=SOURCE,
                                details={"http_status": res.status, "empty_body": True})
        return data, self._prov(url, data.get("accession_id"), "EGA public metadata API", res)

    async def _list(self, path: str, limit: int, offset: int) -> tuple[list[dict], int | None, Provenance]:
        url = f"{self.metadata_base}{path}"
        data, res = await self.meta.get_json(url, params={"limit": limit, "offset": offset}, ok=(200, 206))
        if data is None:
            data = []
        if not isinstance(data, list):
            raise UpstreamError("EGA list endpoint did not return a list", source=SOURCE)
        total = await self._count(path)
        return data, total, self._prov(url, None, "EGA public metadata API (paged)", res)

    async def _count(self, path: str) -> int | None:
        res = await self.meta.request("HEAD", f"{self.metadata_base}{path}", ok=(200, 206))
        raw = res.headers.get("ega-api-total-count")
        return int(raw) if raw and raw.isdigit() else None

    async def _bearer(self) -> str:
        if self.auth is None:
            raise UnauthorizedError(
                "EGA file access needs explicit credentials", source=SOURCE,
                hint="Configure an EGA token or password grant (e.g. the documented public test "
                     "account loaded from pyega3 config files). No ambient credentials are used.",
                details={"http_status": None},
            )
        return await self.auth.bearer(self.data)

    async def _authed_json(self, path: str, params: dict | None = None) -> tuple[Any, Any]:
        bearer = await self._bearer()
        return await self.data.get_json(f"{self.data_base}{path}", params=params,
                                        headers={"Authorization": f"Bearer {bearer}"})

    # -- conversions --------------------------------------------------------------
    def _dataset(self, d: dict, prov: Provenance) -> Dataset:
        acc = d["accession_id"]
        links = []
        if d.get("policy_accession_id"):
            links.append(EntityLink(relation="governed_by", kind=EntityKind.POLICY,
                                    accession=d["policy_accession_id"], source=SOURCE))
        return Dataset(
            accession=acc, source=SOURCE, title=d.get("title"), description=d.get("description"),
            access_status=_access(d.get("access_type")), policy_accession=d.get("policy_accession_id"),
            links=links, native=d, provenance=[prov],
        )

    def _study(self, s: dict, prov: Provenance) -> Study:
        return Study(accession=s["accession_id"], source=SOURCE, title=s.get("title"),
                     description=s.get("description"), native=s, provenance=[prov])

    def _sample(self, s: dict, prov: Provenance, links: list[EntityLink] | None = None) -> Sample:
        phen = [PhenotypeValue(name=k, value=None if s.get(k) is None else str(s[k]), source=SOURCE)
                for k in ("phenotype", "biological_sex") if k in s]
        return Sample(accession=s["accession_id"], source=SOURCE, title=s.get("title"),
                      description=s.get("description"), phenotypes=phen, links=links or [],
                      native=s, provenance=[prov])

    def _public_file(self, f: dict, dataset: Dataset | None, prov: Provenance) -> FileRef:
        acc = f["accession_id"]
        ext = (f.get("extension") or "").lower()
        fmt = infer_format(f"x.{ext}") if ext else None
        rel = []
        ds_acc = dataset.accession if dataset else f.get("dataset_accession_id")
        if ds_acc:
            rel.append(EntityLink(relation="part_of", kind=EntityKind.DATASET, accession=ds_acc,
                                  source=SOURCE))
        access = dataset.access_status if dataset else AccessStatus.UNKNOWN
        checks = []
        if f.get("unencrypted_checksum") and str(f.get("unencrypted_checksum_type", "")).upper() == "MD5":
            checks.append(Checksum(algorithm="md5", value=f["unencrypted_checksum"].lower()))
        native = {k: v for k, v in f.items()}
        native["size_note"] = ("archive filesize; the decrypted plain file size can differ, "
                               "so size_bytes is left unset")
        return FileRef(
            uri=f"ega://{acc}", format=fmt, source=SOURCE, accession=acc, access_status=access,
            visibility=Visibility.PRIVATE if access != AccessStatus.OPEN else Visibility.PUBLIC,
            checksums=checks, relationships=rel, readiness=self._readiness(fmt, ext, authed=False),
            native=native,
        )

    def _authed_file(self, f: dict, access: AccessStatus) -> FileRef:
        acc = f["fileId"]
        name = f.get("displayFileName") or ""
        fmt = infer_format(name)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        rel = [EntityLink(relation="part_of", kind=EntityKind.DATASET, accession=d, source=SOURCE)
               for d in f.get("datasetId") or []]
        idx = f.get("indexFileId") or None
        if idx:
            rel.append(EntityLink(relation="has_index", kind=EntityKind.FILE, accession=idx, source=SOURCE))
        checks = []
        if f.get("plainChecksum") and str(f.get("plainChecksumType", "")).upper() == "MD5":
            checks.append(Checksum(algorithm="md5", value=f["plainChecksum"].lower()))
        native = {k: f[k] for k in ("fileId", "datasetId", "indexFileId", "displayFileName",
                                    "fileSize", "plainChecksum", "plainChecksumType", "fileStatus")
                  if k in f}
        status = AccessStatus.AUTHORIZED if f.get("fileStatus") == "available" else access
        return FileRef(
            uri=f"ega://{acc}", index_uri=f"ega://{idx}" if idx else None, format=fmt, source=SOURCE,
            accession=acc, access_status=status, visibility=Visibility.PRIVATE, checksums=checks,
            relationships=rel, readiness=self._readiness(fmt, ext, authed=True, has_index=bool(idx)),
            native=native,
        )

    @staticmethod
    def _readiness(fmt: FileFormat | None, ext: str, *, authed: bool, has_index: bool = False) -> Readiness:
        if ext in _INDEX_EXT:
            return Readiness(state=ReadinessState.UNSUPPORTED,
                             reasons=["index file; used with its alignment/variant file"])
        if fmt == FileFormat.FASTQ:
            return Readiness(state=ReadinessState.NOT_LOCUS_READY,
                             reasons=["FASTQ is downloadable but not queryable by locus"])
        if fmt in _REGION_FORMATS:
            why = ("EGA htsget access not yet verified for this file; call check_file"
                   if authed else "controlled access: region queries need an authorised EGA htsget ticket")
            reasons = [why]
            if authed and not has_index:
                reasons.append("EGA reports no index file id")
            return Readiness(state=ReadinessState.UNKNOWN, reasons=reasons)
        return Readiness(state=ReadinessState.UNKNOWN,
                         reasons=["file format not reported by EGA public metadata" if not fmt
                                  else f"{fmt.value} is not region-queryable through EGA htsget"])

    # -- discovery ----------------------------------------------------------------
    @operation()
    async def search_datasets(self, query: str = "", *, limit: int | None = None,
                              cursor: str | None = None) -> SourcePage[Dataset]:
        q = query.strip()
        n = page_size(limit)
        if not q:
            path = "/datasets"
        else:
            if not q.upper().startswith("EGA"):
                raise UnsupportedError(
                    "the EGA public metadata API offers no free-text search", source=SOURCE,
                    hint="Search by EGA accession (EGAD/EGAS/EGAN/EGAF/EGAC) or list all datasets.")
            kind = accession_kind(q)
            if kind == "dataset":
                d, prov = await self._one(f"/datasets/{q}")
                return SourcePage(items=[self._dataset(d, prov)], total=1, provenance=[prov])
            path = {"study": f"/studies/{q}/datasets", "sample": f"/samples/{q}/datasets",
                    "file": f"/files/{q}/datasets", "dac": f"/dacs/{q}/datasets"}.get(kind)
            if path is None:
                raise UnsupportedError(f"EGA dataset search by {kind} accession is not offered", source=SOURCE)
            await self._one(path.rsplit("/", 1)[0])  # distinguish unknown accession from no datasets
        scope = f"datasets:{q}"
        off = offset_from(cursor, SOURCE, scope)
        rows, total, prov = await self._list(path, n, off)
        items = [self._dataset(r, prov) for r in rows]
        return SourcePage(items=items, total=total, provenance=[prov],
                          next_cursor=next_offset_cursor(SOURCE, scope, off, len(items), total))

    @operation()
    async def describe_dataset(self, accession: str) -> DatasetDetail:
        acc = _require(accession, "dataset")
        d, prov = await self._one(f"/datasets/{acc}")
        ds = self._dataset(d, prov)
        studies_raw, _, sprov = await self._list(f"/datasets/{acc}/studies", 100, 0)
        studies = [self._study(s, sprov) for s in studies_raw]
        for s in studies:
            ds.links.append(EntityLink(relation="part_of", kind=EntityKind.STUDY, accession=s.accession,
                                       source=SOURCE))
        related: dict[str, Any] = {}
        provs = [prov, sprov]
        ds.file_count = await self._count(f"/datasets/{acc}/files")
        related["sample_endpoint_count"] = await self._count(f"/datasets/{acc}/samples")
        related["num_samples_reported"] = d.get("num_samples")
        warnings = []
        if related["sample_endpoint_count"] is not None and d.get("num_samples") is not None \
                and related["sample_endpoint_count"] != d.get("num_samples"):
            warnings.append(f"EGA reports num_samples={d.get('num_samples')} but its samples endpoint "
                            f"lists {related['sample_endpoint_count']}; both are shown unreconciled")
        if ds.policy_accession:
            pol, pprov = await self._one(f"/policies/{ds.policy_accession}")
            related["policy"] = pol
            provs.append(pprov)
            if pol.get("dac_accession_id"):
                dac, dprov = await self._one(f"/dacs/{pol['dac_accession_id']}")
                # DAC contact names/emails are not needed to request access through EGA; omitted.
                related["dac"] = {k: v for k, v in dac.items() if k != "contacts"}
                provs.append(dprov)
        return DatasetDetail(dataset=ds, studies=studies, related=related, provenance=provs,
                             warnings=warnings)

    @operation()
    async def list_files(self, accession: str, *, limit: int | None = None,
                         cursor: str | None = None) -> SourcePage[FileRef]:
        acc = _require(accession, "dataset")
        n = page_size(limit)
        d, dprov = await self._one(f"/datasets/{acc}")
        dataset = self._dataset(d, dprov)
        scope = f"files:{acc}:{'auth' if self.auth else 'public'}"
        off = offset_from(cursor, SOURCE, scope)
        if self.auth is not None:
            # The authorised listing honours `limit` but ignores `offset` (observed 2026-09-24), so
            # fetch offset+n rows and slice; never scan past MAX_RECORDS.
            want = off + n
            if want > MAX_RECORDS:
                raise InvalidInputError(
                    f"authorised EGA file listing cannot page past {MAX_RECORDS} records",
                    source=SOURCE, details={"offset": off})
            data, res = await self._authed_json(f"/metadata/datasets/{acc}/files",
                                                params={"limit": want})
            rows = data if isinstance(data, list) else []
            prov = Provenance(source=SOURCE, url=res.url, method="EGA download API metadata (authorised)",
                              terms_url=TERMS_URL,
                              transformations=[f"fetched {len(rows)} rows, returned rows [{off},{want})"])
            items = [self._authed_file(r, dataset.access_status) for r in rows[off:want]]
            total = await self._count(f"/datasets/{acc}/files")
            more = len(rows) >= want and want < MAX_RECORDS
            nxt = next_offset_cursor(SOURCE, scope, off, len(items), None, more=more)
            return SourcePage(items=items, next_cursor=nxt, total=total, provenance=[dprov, prov])
        rows, total, prov = await self._list(f"/datasets/{acc}/files", n, off)
        items = [self._public_file(r, dataset, prov) for r in rows]
        return SourcePage(
            items=items, total=total, provenance=[dprov, prov],
            next_cursor=next_offset_cursor(SOURCE, scope, off, len(items), total),
            warnings=["public EGA metadata has no file names or index links; configure EGA "
                      "credentials to see them"],
        )

    @operation()
    async def list_samples(self, accession: str, *, limit: int | None = None,
                           cursor: str | None = None) -> SourcePage[Sample]:
        acc = accession.strip()
        kind = accession_kind(acc)
        if kind not in ("dataset", "study", "file"):
            raise InvalidInputError(f"list_samples needs an EGA dataset, study or file, not a {kind}",
                                    source=SOURCE)
        n = page_size(limit)
        prefix = {"dataset": "datasets", "study": "studies", "file": "files"}[kind]
        await self._one(f"/{prefix}/{acc}")
        scope = f"samples:{acc}"
        off = offset_from(cursor, SOURCE, scope)
        rows, total, prov = await self._list(f"/{prefix}/{acc}/samples", n, off)
        rel = {"dataset": ("part_of", EntityKind.DATASET), "study": ("part_of", EntityKind.STUDY),
               "file": ("sample_of_file", EntityKind.FILE)}[kind]
        items = [self._sample(r, prov, [EntityLink(relation=rel[0], kind=rel[1], accession=acc,
                                                   source=SOURCE)]) for r in rows]
        return SourcePage(items=items, total=total, provenance=[prov],
                          next_cursor=next_offset_cursor(SOURCE, scope, off, len(items), total))

    @operation()
    async def get_sample_metadata(self, accession: str) -> Sample:
        acc = _require(accession, "sample")
        s, prov = await self._one(f"/samples/{acc}")
        links = []
        provs = [prov]
        for rel_path, kind in (("datasets", EntityKind.DATASET), ("studies", EntityKind.STUDY)):
            rows, _, p = await self._list(f"/samples/{acc}/{rel_path}", 100, 0)
            provs.append(p)
            links += [EntityLink(relation="part_of", kind=kind, accession=r["accession_id"], source=SOURCE)
                      for r in rows]
        sample = self._sample(s, prov, links)
        sample.provenance = provs
        return sample

    @operation()
    async def get_file(self, accession: str) -> FileRef:
        """File metadata; with credentials, enriched from the authorised download metadata."""
        acc = _require(accession, "file")
        f, prov = await self._one(f"/files/{acc}")
        datasets, _, dprov = await self._list(f"/files/{acc}/datasets", 100, 0)
        ds = self._dataset(datasets[0], dprov) if len(datasets) == 1 else None
        ref = self._public_file(f, ds, prov)
        if ds is None:
            ref.relationships += [EntityLink(relation="part_of", kind=EntityKind.DATASET,
                                             accession=r["accession_id"], source=SOURCE) for r in datasets]
        if self.auth is not None:
            data, _ = await self._authed_json(f"/metadata/files/{acc}")
            if not isinstance(data, dict) or not data.get("fileId"):
                raise NotFoundError(f"EGA download metadata has no file {acc}", source=SOURCE)
            ref = self._authed_file(data, ref.access_status)
        return ref

    # -- retrieval ----------------------------------------------------------------
    @operation()
    async def check_file(self, accession: str, *, endpoint: str = "reads") -> FileRef:
        """Verify region readiness with a header-only htsget ticket (no data blocks fetched)."""
        ref = await self.get_file(accession)
        fmt = "BAM" if endpoint == "reads" else "VCF"
        bearer = await self._bearer()
        url = f"{self.data_base}/htsget/{endpoint}/{ref.accession}"
        data, _ = await self.data.get_json(url, params={"format": fmt, "class": "header"},
                                           headers=hg.ticket_headers(bearer))
        ticket = hg.parse_ticket(data)
        ref.readiness = Readiness(
            state=ReadinessState.READY, checked_at=utcnow(),
            reasons=[f"EGA htsget {endpoint} ticket issued for {fmt} ({len(ticket.blocks)} header block(s))",
                     "region retrieval returns a bounded local artifact; records are post-filtered"],
        )
        ref.access_status = AccessStatus.AUTHORIZED
        return ref

    @operation()
    async def get_region(
        self,
        accession: str,
        interval: Interval,
        *,
        workspace: Path,
        endpoint: str = "reads",
        fmt: str | None = None,
        budget_bytes: int = hg.DEFAULT_REGION_BUDGET,
        max_records: int = hg.DEFAULT_MAX_RECORDS,
        max_region_bp: int = hg.DEFAULT_MAX_REGION_BP,
        assembly_policy: str = "reject",
    ) -> hg.RegionResult:
        """Retrieve the caller's explicit interval through EGA htsget; never the whole file.

        `budget_bytes` caps the decoded genomic bytes (all blocks together); the ticket JSON, which
        embeds base64 data, is capped at 4/3 of that plus 64 KiB. With `assembly_policy="reject"`
        (default) a header whose @SQ AS tags do not include `interval.assembly`, or that lacks the
        contig, is an error and the artifact is removed; `"warn"` returns records with a warning.
        """
        acc = _require(accession, "file")
        if endpoint not in ("reads", "variants"):
            raise InvalidInputError("endpoint must be 'reads' or 'variants'", source=SOURCE)
        if assembly_policy not in ("reject", "warn"):
            raise InvalidInputError("assembly_policy must be 'reject' or 'warn'", source=SOURCE)
        if budget_bytes <= 0 or max_records < 0:
            raise InvalidInputError("budget_bytes must be positive and max_records non-negative",
                                    source=SOURCE)
        fmt_u = hg.validate_region(interval, fmt or ("BAM" if endpoint == "reads" else "VCF"),
                                   endpoint, max_region_bp)
        ext = "bam" if endpoint == "reads" else "vcf.gz"
        path = artifact_path(workspace, hg.region_artifact_name(acc, interval, ext))
        bearer = await self._bearer()
        url = f"{self.data_base}/htsget/{endpoint}/{acc}"
        params = {"referenceName": interval.contig, "start": interval.start, "end": interval.end,
                  "format": fmt_u}
        data, res = await self.data.get_json(url, params=params, headers=hg.ticket_headers(bearer),
                                             max_body=budget_bytes * 4 // 3 + 64 * 1024)
        ticket = hg.parse_ticket(data)
        payload, blocks = await hg.fetch_blocks(self.data, ticket, ticket_url=url, bearer=bearer,
                                                budget_bytes=budget_bytes, allowed_hosts=self.block_hosts)
        if not payload:
            raise UpstreamError("EGA htsget returned no data", source=SOURCE)
        ticket_md5 = hg.verify_ticket_md5(ticket, payload)
        sha256, md5 = hg.write_private(path, payload)
        transformations = [
            f"htsget {endpoint} ticket for the requested 0-based half-open interval "
            f"[{interval.start},{interval.end}) (start/end passed unchanged)",
            f"decoded {sum(b.kind == 'data' for b in blocks)} data-URL block(s) once each and fetched "
            f"{sum(b.kind == 'remote' for b in blocks)} remote block(s); concatenated in ticket order",
            "ticket MD5 verified" if ticket_md5 else "ticket supplied no MD5; bytes not checksum-verified",
        ]
        prov = Provenance(source=SOURCE, source_record_id=acc, url=res.url,
                          method=f"EGA htsget /{endpoint}", terms_url=TERMS_URL,
                          transformations=list(transformations))
        artifact = hg.artifact_for(path, len(payload), sha256, md5, fmt_u, hg.origin_file(acc, fmt_u), prov,
                                   verified=ticket_md5 is not None)
        try:
            parsed = await hg.postfilter(path, endpoint, interval, max_records)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        header: hg.HeaderSummary = parsed["header"]
        problems = []
        if not header.contig_present:
            problems.append(f"contig {interval.contig!r} is not in the file header")
        if header.sq_assembly_tags and interval.assembly not in header.sq_assembly_tags:
            problems.append(f"caller assembly {interval.assembly!r} differs from header assembly tags "
                            f"{header.sq_assembly_tags}; no liftover is applied")
        if problems and assembly_policy == "reject":
            path.unlink(missing_ok=True)
            raise InvalidInputError("; ".join(problems), source=SOURCE,
                                    hint="Use the file's own contig names and assembly, or "
                                         "assembly_policy='warn' to inspect the records anyway.",
                                    details={"header": header.model_dump()})
        filt = ["overlap: reference_start < end and reference_end > start (reference_end from CIGAR)"
                if endpoint == "reads" else "overlap: record start < end and stop > start"]
        if endpoint == "reads":
            filt.append("unmapped/unplaced records excluded; no flag or MAPQ filters applied")
        result_prov = prov.model_copy(update={"transformations": transformations + [
            f"post-filtered {parsed['total']} decoded records to {parsed['overlapping']} overlapping"]})
        return hg.RegionResult(
            accession=acc, interval=interval, endpoint=endpoint, format=fmt_u, artifact=artifact,
            blocks=blocks, header=header, records=parsed["records"],
            records_in_blocks=parsed["total"], records_overlapping=parsed["overlapping"],
            records_skipped_unplaced=parsed["unplaced"],
            truncated=parsed["overlapping"] > len(parsed["records"]), filters=filt,
            warnings=problems, provenance=[result_prov],
        )

    @operation("transfer_timeout_s")
    async def fetch_file(self, accession: str, *, workspace: Path,
                         budget_bytes: int = DEFAULT_FILE_BUDGET) -> Artifact:
        """Download a whole decrypted file within an explicit budget and verify its MD5."""
        if self.auth is None:
            await self._bearer()  # raises unauthorized with a hint
        ref = await self.get_file(accession)
        stored = ref.native.get("fileSize")
        if not isinstance(stored, int) or stored <= 16:
            raise PreparationRequiredError("EGA reports no usable size for this file; refusing an "
                                           "unbounded download", source=SOURCE)
        # pyega3 (libs/data_file.py): plain size = stored size - 16-byte IV; fetched by explicit Range.
        plain = stored - 16
        md5 = next((c.value for c in ref.checksums if c.algorithm == "md5"), None)
        bearer = await self._bearer()
        # displayFileName is source data: reduced to a safe basename inside the workspace.
        dest = artifact_path(workspace, str(ref.native.get("displayFileName") or f"{ref.accession}.bin"))
        dl = await self.data.download(
            f"{self.data_base}/files/{ref.accession}", dest, budget_bytes=budget_bytes,
            params={"destinationFormat": "plain"},
            headers={"Authorization": f"Bearer {bearer}", "Range": f"bytes=0-{plain - 1}"},
            expected_size=plain,
        )
        if dl.size_bytes != plain:
            dest.unlink(missing_ok=True)
            raise UpstreamError("EGA download size differs from the expected plain size; file removed",
                                source=SOURCE, details={"expected": plain, "observed": dl.size_bytes})
        verified = md5 is not None and dl.md5 == md5
        if md5 is not None and not verified:
            dest.unlink(missing_ok=True)
            raise UpstreamError("downloaded EGA file failed its MD5 check; file removed", source=SOURCE,
                                details={"expected_md5": md5, "observed_md5": dl.md5})
        prov = Provenance(source=SOURCE, source_record_id=ref.accession,
                          url=f"{self.data_base}/files/{ref.accession}",
                          method="EGA download API (destinationFormat=plain)", terms_url=TERMS_URL,
                          transformations=[f"streamed bytes 0-{plain - 1} within budget",
                                           "md5 verified" if verified else "no source MD5 available"])
        return Artifact(path=str(dl.path), size_bytes=dl.size_bytes,
                        checksums=[Checksum(algorithm="md5", value=dl.md5),
                                   Checksum(algorithm="sha256", value=dl.sha256)],
                        checksum_verified=verified, format=ref.format, origin=ref, provenance=prov)
