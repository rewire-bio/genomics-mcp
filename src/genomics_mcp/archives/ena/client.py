"""ENA client: Portal API (search, filereport, count) and Browser API (XML records, FASTA).

Behaviour checked 2026-09-24:

* Portal API `/search` and `/filereport` have **no offset parameter** (`Unsupported param
  offset`), and row order changes with `limit` (limit=6 and limit=0 returned different first
  rows for PRJEB54831). Pages are therefore built from the complete accession list
  (`limit=0`, id field only, at most `MAX_RECORDS`), sorted, sliced, and the page's details
  fetched with `POST /search includeAccessions=`. Totals come from that list.
* Unknown accessions give 200 with `[]` from filereport, so existence is checked first
  (`/search?includeAccessions=`, which also resolves SRA/DDBJ secondary accessions such as
  SRP000001 -> PRJNA33627). Browser API answers 404 for unknown records.
* Text wildcards match single tokens case-insensitively, so free-text search is an AND of
  per-word `study_title`/`study_description` wildcard clauses.
* Files are reported as `ftp.sra.ebi.ac.uk/...` paths without a scheme. ENA serves the same
  paths over HTTPS with byte ranges (ftp.sra.ebi.ac.uk, ftp.ebi.ac.uk), so those two hosts
  are converted to https://. Any other host stays ftp:// and needs explicit preparation.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from genomics_mcp.archives._common.deadline import (
    DEFAULT_OPERATION_TIMEOUT_S,
    DEFAULT_TRANSFER_TIMEOUT_S,
    operation,
)
from genomics_mcp.archives._common.errors import (
    BudgetExceededError,
    InvalidInputError,
    NotFoundError,
    UnsupportedError,
    UpstreamError,
)
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy, file_digests
from genomics_mcp.archives._common.models import (
    AccessStatus,
    Artifact,
    Checksum,
    Compression,
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
    Sample,
    SourcePage,
    Study,
    Visibility,
    compression_from_name,
    infer_format,
    is_index_name,
    sniff_compression,
    utcnow,
)
from genomics_mcp.archives._common.paging import (
    MAX_RECORDS,
    next_offset_cursor,
    offset_from,
    page_size,
)
from genomics_mcp.archives._common.workspace import artifact_path

SOURCE = "ena"
PORTAL = "https://www.ebi.ac.uk/ena/portal/api"
BROWSER = "https://www.ebi.ac.uk/ena/browser/api"
TERMS_URL = "https://www.ebi.ac.uk/ena/browser/about/policies"
API_HOSTS = frozenset({"www.ebi.ac.uk"})
HTTPS_FILE_HOSTS = frozenset({"ftp.sra.ebi.ac.uk", "ftp.ebi.ac.uk"})
DEFAULT_FILE_BUDGET = 100 * 1024 * 1024
MAX_XML_BYTES = 4 * 1024 * 1024

_STUDY = re.compile(r"^(PRJ[EDN][A-Z]\d+|[EDS]RP\d{6,})$")
_SAMPLE = re.compile(r"^(SAM[EDN][A-Z]?\d+|[EDS]RS\d{6,})$")
_RUN = re.compile(r"^[EDS]RR\d{6,}$")
_EXPERIMENT = re.compile(r"^[EDS]RX\d{6,}$")
_ANALYSIS = re.compile(r"^[EDS]RZ\d{6,}$")
_SEQUENCE = re.compile(r"^[A-Z]{1,6}_?\d{5,12}(\.\d+)?$")

STUDY_FIELDS = ("study_accession,secondary_study_accession,study_title,study_description,tax_id,"
                "scientific_name,center_name,first_public,last_updated,status")
RUN_FIELDS = ("run_accession,study_accession,secondary_study_accession,sample_accession,"
              "secondary_sample_accession,experiment_accession,library_strategy,library_source,"
              "library_layout,instrument_platform,fastq_ftp,fastq_bytes,fastq_md5,submitted_ftp,"
              "submitted_bytes,submitted_md5,submitted_format,bam_ftp,bam_bytes,bam_md5")
ANALYSIS_FIELDS = ("analysis_accession,study_accession,sample_accession,analysis_type,"
                   "submitted_ftp,submitted_bytes,submitted_md5,submitted_format,generated_ftp,"
                   "generated_bytes,generated_md5")
SAMPLE_FIELDS = ("sample_accession,secondary_sample_accession,sample_alias,sample_title,tax_id,"
                 "scientific_name,description,first_public,last_updated,checklist")
_INDEX_FORMATS = {"BAI", "CRAI", "TBI", "CSI"}


def classify(acc: str) -> str:
    a = acc.strip()
    for kind, rx in (("study", _STUDY), ("sample", _SAMPLE), ("run", _RUN),
                     ("experiment", _EXPERIMENT), ("analysis", _ANALYSIS)):
        if rx.match(a):
            return kind
    if _SEQUENCE.match(a):
        return "sequence"
    raise InvalidInputError(f"not a recognised ENA/SRA accession: {acc!r}", source=SOURCE)


def to_uri(path: str) -> tuple[str, bool]:
    """ENA file path -> (uri, https_available). Never invents hosts."""
    p = path.strip()
    if "://" in p:
        scheme, rest = p.split("://", 1)
    else:
        scheme, rest = "ftp", p
    host = rest.split("/", 1)[0].lower()
    if host in HTTPS_FILE_HOSTS and scheme in ("ftp", "http", "https"):
        return f"https://{rest}", True
    return f"{scheme}://{rest}", scheme == "https"


_DECLARED = {"BAM": FileFormat.BAM, "CRAM": FileFormat.CRAM, "FASTQ": FileFormat.FASTQ,
             "VCF": FileFormat.VCF, "FASTA": FileFormat.FASTA, "BCF": FileFormat.BCF}
_INDEX_FOR = {FileFormat.BAM: (".bai", ".csi"), FileFormat.CRAM: (".crai",),
              FileFormat.VCF: (".tbi", ".csi"), FileFormat.BCF: (".csi",)}
_DATA_EXT = (".vcf.gz", ".vcf.bgz", ".bam", ".cram", ".bcf")


def _entry_format(e: dict) -> FileFormat | None:
    return infer_format(e["name"]) or _DECLARED.get((e["declared"] or "").upper())


def companion_indexes(name: str, fmt: FileFormat | None, index_names: list[str]) -> list[str]:
    """Index file names that conventionally belong to `name` (`a.bam.bai`, else `a.bai`).

    Full-name companions win; otherwise stem companions; more than one candidate is ambiguous."""
    suffixes = _INDEX_FOR.get(fmt) if fmt else None
    if not suffixes:
        return []
    full = [n for n in index_names if any(n == name + sfx for sfx in suffixes)]
    if full:
        return full
    stem = next((name[: -len(ext)] for ext in _DATA_EXT if name.lower().endswith(ext)), None)
    if not stem:
        return []
    return [n for n in index_names if any(n == stem + sfx for sfx in suffixes)]


def fasta_accession(header: str) -> str | None:
    """`>ENA|DQ285577|DQ285577.1 desc` -> `DQ285577.1`; `>X.1 desc` -> `X.1`; else None."""
    if not header.startswith(">"):
        return None
    token = header[1:].split()[0] if header[1:].split() else ""
    parts = token.split("|")
    cand = parts[2] if len(parts) >= 3 and parts[0] == "ENA" else token
    return cand if _SEQUENCE.match(cand) else None


def _split(v: Any) -> list[str]:
    return [x for x in str(v or "").split(";")] if v not in (None, "") else []


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9]+", text)][:6]


class EnaClient:
    def __init__(self, client: httpx.AsyncClient, *, timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S,
                 transfer_timeout_s: float = DEFAULT_TRANSFER_TIMEOUT_S) -> None:
        self.source = SOURCE
        self.operation_timeout_s = timeout_s
        self.transfer_timeout_s = transfer_timeout_s
        self.http = SourceHttp(client, SourcePolicy(
            SOURCE, API_HOSTS, min_interval_s=0.1, timeout_s=timeout_s, terms_url=TERMS_URL))
        self.files = SourceHttp(client, SourcePolicy(
            SOURCE, HTTPS_FILE_HOSTS, min_interval_s=0.1, timeout_s=timeout_s, terms_url=TERMS_URL))

    def _prov(self, url: str, record: str | None, method: str, **kw: Any) -> Provenance:
        return Provenance(source=SOURCE, source_record_id=record, url=url, method=method,
                          terms_url=TERMS_URL, **kw)

    async def _portal(self, endpoint: str, params: dict[str, Any]) -> tuple[list[dict], Provenance]:
        url = f"{PORTAL}/{endpoint}"
        data, res = await self.http.get_json(url, params={**params, "format": "json"})
        if data is None:
            data = []
        if not isinstance(data, list):
            raise UpstreamError("ENA portal returned an unexpected payload", source=SOURCE)
        return data, self._prov(str(httpx.URL(url, params={**params, "format": "json"})), None,
                                f"ENA Portal API /{endpoint}")

    async def _count(self, endpoint: str, params: dict[str, Any]) -> int | None:
        data, _ = await self.http.get_json(f"{PORTAL}/{endpoint}", params={**params, "format": "json"})
        try:
            return int(data["count"])
        except (TypeError, KeyError, ValueError):
            return None

    async def _page_ids(self, endpoint: str, params: dict[str, Any], id_field: str, off: int, n: int
                        ) -> tuple[list[str], int, Provenance]:
        """Sorted accession list (bounded), sliced to one page. Deterministic across calls."""
        rows, prov = await self._portal(endpoint, {**params, "fields": id_field, "limit": MAX_RECORDS + 1})
        ids = sorted({r[id_field] for r in rows if r.get(id_field)})
        if len(rows) > MAX_RECORDS:
            raise InvalidInputError(
                f"more than {MAX_RECORDS} ENA records match; refine the query or list a narrower "
                "accession (sample or run)", source=SOURCE, details={"limit": MAX_RECORDS})
        prov.transformations.append(f"sorted {len(ids)} accessions; page [{off},{off + n})")
        return ids[off:off + n], len(ids), prov

    async def _details(self, result: str, ids: list[str], fields: str, id_field: str) -> list[dict]:
        if not ids:
            return []
        res = await self.http.request(
            "POST", f"{PORTAL}/search", ok=(200,), headers={"Accept": "application/json"},
            data={"result": result, "includeAccessions": ",".join(ids), "fields": fields,
                  "format": "json", "limit": str(len(ids))})
        rows = res.json() or []
        by_id = {r.get(id_field): r for r in rows if isinstance(r, dict)}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise UpstreamError(f"ENA returned no details for {len(missing)} listed accessions",
                                source=SOURCE, details={"missing": missing[:10]})
        return [by_id[i] for i in ids]

    async def _study_row(self, acc: str) -> tuple[dict, Provenance]:
        rows, prov = await self._portal("search", {"result": "study", "includeAccessions": acc,
                                                   "fields": STUDY_FIELDS})
        if not rows:
            raise NotFoundError(f"ENA has no study {acc}", source=SOURCE)
        prov.source_record_id = rows[0]["study_accession"]
        return rows[0], prov

    # -- conversions --------------------------------------------------------------
    def _dataset(self, r: dict, prov: Provenance) -> Dataset:
        native = {k: v for k, v in r.items() if v not in ("", None)}
        return Dataset(accession=r["study_accession"], source=SOURCE, title=r.get("study_title") or None,
                       description=r.get("study_description") or None, access_status=AccessStatus.OPEN,
                       native=native, provenance=[prov])

    def _files_from_row(self, row: dict, owner_kind: EntityKind, owner: str, prov: Provenance
                        ) -> list[FileRef]:
        links = [EntityLink(relation="file_of", kind=owner_kind, accession=owner, source=SOURCE)]
        if row.get("study_accession"):
            links.append(EntityLink(relation="part_of", kind=EntityKind.STUDY,
                                    accession=row["study_accession"], source=SOURCE))
        for s in _split(row.get("sample_accession")):
            links.append(EntityLink(relation="derived_from", kind=EntityKind.SAMPLE, accession=s,
                                    source=SOURCE))
        if row.get("experiment_accession"):
            links.append(EntityLink(relation="part_of", kind=EntityKind.EXPERIMENT,
                                    accession=row["experiment_accession"], source=SOURCE))
        entries: list[dict[str, Any]] = []
        for group in ("fastq", "submitted", "bam", "generated"):
            paths = _split(row.get(f"{group}_ftp"))
            sizes, md5s = _split(row.get(f"{group}_bytes")), _split(row.get(f"{group}_md5"))
            fmts = _split(row.get(f"{group}_format")) if group == "submitted" else []
            for i, path in enumerate(paths):
                if not path.strip():
                    continue
                uri, https = to_uri(path)
                declared = fmts[i] if i < len(fmts) else ("FASTQ" if group == "fastq" else None)
                name = uri.rsplit("/", 1)[-1]
                entries.append({
                    "uri": uri, "https": https, "declared": declared, "group": group, "name": name,
                    "size": sizes[i] if i < len(sizes) else None, "md5": md5s[i] if i < len(md5s) else None,
                    "is_index": is_index_name(name) or (declared or "").upper() in _INDEX_FORMATS,
                })
        indexes = [e for e in entries if e["is_index"]]
        data = [e for e in entries if not e["is_index"]]
        # Pair indexes with data files only within this one source record (run/analysis).
        claims: dict[str, list[int]] = {}
        chosen: dict[int, dict | None] = {}
        for i, e in enumerate(data):
            fmt = _entry_format(e)
            cands = companion_indexes(e["name"], fmt, [x["name"] for x in indexes])
            e["fmt"] = fmt
            e["index_candidates"] = cands
            if len(cands) == 1:
                claims.setdefault(cands[0], []).append(i)
        for i, e in enumerate(data):
            cands = e["index_candidates"]
            ok = len(cands) == 1 and len(claims.get(cands[0], [])) == 1
            chosen[i] = next(x for x in indexes if x["name"] == cands[0]) if ok else None
        used = {c["name"] for c in chosen.values() if c}
        out: list[FileRef] = []
        for i, e in enumerate(data):
            idx = chosen[i]
            native: dict[str, Any] = {"file_group": e["group"], "declared_format": e["declared"],
                                      "https_available": e["https"]}
            if idx:
                native["index"] = {"uri": idx["uri"], "bytes": idx["size"], "md5": idx["md5"],
                                   "declared_format": idx["declared"],
                                   "pairing": "same ENA record, conventional companion file name"}
            elif len(e["index_candidates"]) > 1 or (e["index_candidates"]
                                                    and len(claims.get(e["index_candidates"][0], [])) > 1):
                native["index_ambiguous"] = e["index_candidates"]
            out.append(self._ref(e, e["fmt"], idx["uri"] if idx else None, owner, links, native))
        for x in indexes:
            if x["name"] in used:
                continue
            native = {"file_group": x["group"], "declared_format": x["declared"], "https_available": x["https"],
                      "role": "index", "note": "no unambiguous data file for this index in the same record"}
            ref = self._ref(x, FileFormat.OTHER, None, owner, links, native)
            ref.readiness = Readiness(state=ReadinessState.UNSUPPORTED,
                                      reasons=["index file listed without an unambiguous data file"])
            out.append(ref)
        return out

    def _ref(self, e: dict, fmt: FileFormat | None, index: str | None, owner: str, links: list[EntityLink],
             native: dict[str, Any]) -> FileRef:
        size = e["size"]
        return FileRef(
            uri=e["uri"], index_uri=index, format=fmt, compression=compression_from_name(e["name"]),
            source=SOURCE, accession=owner, access_status=AccessStatus.OPEN, visibility=Visibility.PUBLIC,
            size_bytes=int(size) if size and size.isdigit() else None,
            checksums=[Checksum(algorithm="md5", value=e["md5"].lower())] if e["md5"] else [],
            relationships=list(links), readiness=self._readiness(fmt, index, e["https"], e["group"]),
            native=native,
        )

    @staticmethod
    def _readiness(fmt: FileFormat | None, index: str | None, https: bool, group: str) -> Readiness:
        reasons = []
        if not https:
            return Readiness(state=ReadinessState.DOWNLOAD_REQUIRED,
                             reasons=["FTP-only location; explicit preparation (download) required"])
        if fmt == FileFormat.FASTQ:
            return Readiness(state=ReadinessState.NOT_LOCUS_READY,
                             reasons=["FASTQ is downloadable but not queryable by locus"])
        if fmt in (FileFormat.BAM, FileFormat.CRAM, FileFormat.VCF, FileFormat.BCF):
            if index is None:
                return Readiness(state=ReadinessState.INDEX_REQUIRED,
                                 reasons=["ENA lists no index for this file; it may also be unmapped"])
            reasons.append("index listed by ENA; range support and alignment state not yet verified "
                           "(call check_file)")
            reasons.append("ENA does not report the assembly; supply it explicitly")
            return Readiness(state=ReadinessState.UNKNOWN, reasons=reasons)
        return Readiness(state=ReadinessState.UNKNOWN,
                         reasons=[f"{group} file of format {fmt.value if fmt else 'unknown'}"])

    # -- discovery ----------------------------------------------------------------
    @operation()
    async def search_datasets(self, query: str, *, limit: int | None = None,
                              cursor: str | None = None) -> SourcePage[Dataset]:
        q = query.strip()
        n = page_size(limit)
        if not q:
            raise InvalidInputError("ENA search needs a query or accession", source=SOURCE)
        try:
            kind = classify(q)
        except InvalidInputError:
            kind = None
        if kind == "study":
            row, prov = await self._study_row(q)
            return SourcePage(items=[self._dataset(row, prov)], total=1, provenance=[prov])
        if kind is not None:
            raise UnsupportedError(f"search_datasets takes a study accession or text; {q} is a {kind}",
                                   source=SOURCE, hint="Use get_sample_metadata or list_files.")
        words = _words(q)
        if not words:
            raise InvalidInputError("query has no searchable words", source=SOURCE)
        clause = " AND ".join(f'(study_title="*{w}*" OR study_description="*{w}*")' for w in words)
        scope = f"search:{clause}"
        off = offset_from(cursor, SOURCE, scope)
        ids, total, prov = await self._page_ids("search", {"result": "study", "query": clause},
                                                "study_accession", off, n)
        rows = await self._details("study", ids, STUDY_FIELDS, "study_accession")
        items = [self._dataset(r, prov) for r in rows]
        return SourcePage(items=items, total=total, provenance=[prov],
                          next_cursor=next_offset_cursor(SOURCE, scope, off, len(items), total))

    @operation()
    async def describe_dataset(self, accession: str) -> DatasetDetail:
        acc = accession.strip()
        if classify(acc) != "study":
            raise InvalidInputError(f"{acc} is not an ENA/SRA study accession", source=SOURCE)
        row, prov = await self._study_row(acc)
        ds = self._dataset(row, prov)
        primary = row["study_accession"]
        related = {
            "run_count": await self._count("filereportcount", {"accession": primary, "result": "read_run"}),
            "analysis_count": await self._count("filereportcount", {"accession": primary,
                                                                    "result": "analysis"}),
            "sample_count": await self._count("count", {"result": "sample",
                                                        "query": f"study_accession={primary}"}),
        }
        warnings = []
        if primary != acc:
            warnings.append(f"{acc} resolved to primary study accession {primary} by ENA")
        # ENA's study record is both the discoverable dataset and the study.
        study = Study(accession=ds.accession, source=SOURCE, title=ds.title, description=ds.description,
                      native=ds.native, provenance=[prov])
        return DatasetDetail(dataset=ds, studies=[study], related=related, provenance=[prov],
                             warnings=warnings)

    @operation()
    async def list_files(self, accession: str, *, limit: int | None = None,
                         cursor: str | None = None) -> SourcePage[FileRef]:
        """Run and analysis files for a study/sample/experiment/run/analysis. Paged by source row."""
        acc = accession.strip()
        kind = classify(acc)
        if kind == "sequence":
            return await self._sequence_file(acc)
        if kind == "study":
            row, _ = await self._study_row(acc)
            acc = row["study_accession"]
        n = page_size(limit)
        scope = f"files:{acc}"
        off = offset_from(cursor, SOURCE, scope)
        results = ["read_run", "analysis"] if kind in ("study", "sample") else (
            ["analysis"] if kind == "analysis" else ["read_run"])
        id_fields = {"read_run": "run_accession", "analysis": "analysis_accession"}
        all_ids: list[tuple[str, str]] = []
        provs: list[Provenance] = []
        for r in results:
            ids, _, prov = await self._page_ids("filereport", {"accession": acc, "result": r},
                                                id_fields[r], 0, MAX_RECORDS)
            provs.append(prov)
            all_ids += [(r, i) for i in ids]
        if not all_ids and kind != "study":
            await self._browser_xml(acc)  # filereport answers [] for unknown accessions
        page = all_ids[off:off + n]
        items: list[FileRef] = []
        for r in results:
            ids = [i for rr, i in page if rr == r]
            fields = RUN_FIELDS if r == "read_run" else ANALYSIS_FIELDS
            for row in await self._details(r, ids, fields, id_fields[r]):
                okind = EntityKind.RUN if r == "read_run" else EntityKind.ANALYSIS
                items.extend(self._files_from_row(row, okind, row[id_fields[r]], provs[0]))
        total_rows = len(all_ids)
        return SourcePage(items=items, total=None, provenance=provs,
                          next_cursor=next_offset_cursor(SOURCE, scope, off, len(page), total_rows),
                          warnings=[f"paged by ENA run/analysis records ({total_rows} in total); "
                                    "a record can carry several files"])

    @operation()
    async def list_samples(self, accession: str, *, limit: int | None = None,
                           cursor: str | None = None) -> SourcePage[Sample]:
        acc = accession.strip()
        if classify(acc) != "study":
            raise InvalidInputError("list_samples needs an ENA/SRA study accession", source=SOURCE)
        row, _ = await self._study_row(acc)
        primary = row["study_accession"]
        n = page_size(limit)
        scope = f"samples:{primary}"
        off = offset_from(cursor, SOURCE, scope)
        query = f"study_accession={primary}"
        ids, total, prov = await self._page_ids("search", {"result": "sample", "query": query},
                                                "sample_accession", off, n)
        rows = await self._details("sample", ids, SAMPLE_FIELDS, "sample_accession")
        items = []
        for r in rows:
            items.append(Sample(
                accession=r["sample_accession"], source=SOURCE, title=r.get("sample_title") or None,
                description=r.get("description") or None, organism=r.get("scientific_name") or None,
                taxon_id=int(r["tax_id"]) if str(r.get("tax_id", "")).isdigit() else None,
                links=[EntityLink(relation="part_of", kind=EntityKind.STUDY, accession=primary,
                                  source=SOURCE)],
                native={k: v for k, v in r.items() if v not in ("", None)}, provenance=[prov],
            ))
        return SourcePage(items=items, total=total, provenance=[prov],
                          next_cursor=next_offset_cursor(SOURCE, scope, off, len(items), total),
                          warnings=["summary rows; call get_sample_metadata for all sample attributes"])

    async def _browser_xml(self, acc: str) -> tuple[ET.Element, Provenance]:
        url = f"{BROWSER}/xml/{acc}"
        text, res = await self.http.get_text(url, headers={"Accept": "application/xml"},
                                             max_body=MAX_XML_BYTES)
        if "<!DOCTYPE" in text[:500] or "<!ENTITY" in text:
            raise UpstreamError("ENA XML contains a DTD; refusing to parse", source=SOURCE)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise UpstreamError("ENA returned malformed XML", source=SOURCE) from exc
        if len(root) == 0:
            raise NotFoundError(f"ENA has no record {acc}", source=SOURCE)
        return root, self._prov(url, acc, "ENA Browser API XML")

    @operation()
    async def get_sample_metadata(self, accession: str) -> Sample:
        """All SAMPLE_ATTRIBUTES exactly as submitted (tag, value, units) plus declared links."""
        acc = accession.strip()
        if classify(acc) != "sample":
            raise InvalidInputError(f"{acc} is not an ENA/SRA/BioSample sample accession", source=SOURCE)
        root, prov = await self._browser_xml(acc)
        s = root.find("SAMPLE")
        if s is None:
            raise NotFoundError(f"ENA record {acc} is not a sample", source=SOURCE)
        attrs = []
        for a in s.findall("./SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE"):
            tag = (a.findtext("TAG") or "").strip()
            if not tag:
                continue
            attrs.append(PhenotypeValue(name=tag, value=a.findtext("VALUE"), unit=a.findtext("UNITS"),
                                        source=SOURCE))
        ids = {"primary": s.findtext("./IDENTIFIERS/PRIMARY_ID"),
               "secondary": [e.text for e in s.findall("./IDENTIFIERS/SECONDARY_ID")],
               "external": [{"namespace": e.get("namespace"), "id": e.text}
                            for e in s.findall("./IDENTIFIERS/EXTERNAL_ID")],
               "submitter": [{"namespace": e.get("namespace"), "id": e.text}
                             for e in s.findall("./IDENTIFIERS/SUBMITTER_ID")]}
        xrefs = [{"db": x.findtext("DB"), "id": x.findtext("ID")}
                 for x in s.findall("./SAMPLE_LINKS/SAMPLE_LINK/XREF_LINK")]
        taxon = s.findtext("./SAMPLE_NAME/TAXON_ID")
        prov.transformations.append("SAMPLE_ATTRIBUTES copied verbatim as phenotype values "
                                    "(includes ENA-* housekeeping tags)")
        return Sample(
            accession=s.get("accession") or acc, source=SOURCE, title=s.findtext("TITLE"),
            description=s.findtext("DESCRIPTION"), organism=s.findtext("./SAMPLE_NAME/SCIENTIFIC_NAME"),
            taxon_id=int(taxon) if taxon and taxon.isdigit() else None, phenotypes=attrs,
            native={"alias": s.get("alias"), "center_name": s.get("center_name"), "identifiers": ids,
                    "xref_links": xrefs},
            provenance=[prov],
        )

    # -- retrieval ----------------------------------------------------------------
    async def _sequence_file(self, acc: str) -> SourcePage[FileRef]:
        url = f"{BROWSER}/fasta/{acc}"
        ref = FileRef(uri=url, format=FileFormat.FASTA, compression=Compression.NONE, source=SOURCE,
                      accession=acc, access_status=AccessStatus.OPEN, visibility=Visibility.PUBLIC,
                      readiness=Readiness(state=ReadinessState.DOWNLOAD_REQUIRED,
                                          reasons=["ENA Browser API FASTA; download then faidx-index "
                                                   "locally (fetch_sequence_fasta)"]),
                      native={"endpoint": "browser/api/fasta"})
        return SourcePage(items=[ref], total=1, provenance=[self._prov(url, acc, "ENA Browser API FASTA")])

    @operation()
    async def check_file(self, file: FileRef) -> FileRef:
        """Probe HTTPS byte-range support (and BGZF for .gz) for a listed ENA file."""
        if not file.uri.startswith("https://"):
            raise UnsupportedError("only HTTPS ENA files can be range-checked; FTP needs preparation",
                                   source=SOURCE)
        probe = await self.files.probe_range(file.uri, nbytes=64)
        out = file.model_copy(deep=True)
        reasons = []
        if probe.total_size is not None:
            out.size_bytes = probe.total_size
        if out.compression == Compression.UNKNOWN:
            out.compression = sniff_compression(probe.head)
        state = ReadinessState.UNKNOWN
        if not probe.range_supported:
            state = ReadinessState.DOWNLOAD_REQUIRED
            reasons.append(f"server answered HTTP {probe.status} to a Range request")
        elif out.format == FileFormat.FASTQ:
            state = ReadinessState.NOT_LOCUS_READY
            reasons.append("FASTQ is not queryable by locus")
        elif out.format in (FileFormat.BAM, FileFormat.CRAM, FileFormat.VCF, FileFormat.BCF):
            if out.index_uri is None:
                state = ReadinessState.INDEX_REQUIRED
                reasons.append("no index listed")
            else:
                iprobe = await self.files.probe_range(out.index_uri, nbytes=16)
                if not iprobe.range_supported and iprobe.status != 200:
                    state = ReadinessState.INDEX_REQUIRED
                    reasons.append("index not retrievable")
                elif out.assembly is None:
                    state = ReadinessState.READY
                    reasons.append("range reads verified for file and index; assembly is not reported "
                                   "by ENA and must be supplied by the caller")
                else:
                    state = ReadinessState.READY
            if out.format == FileFormat.VCF and out.compression != Compression.BGZF:
                state = ReadinessState.INDEX_REQUIRED
                reasons.append(f"VCF compression is {out.compression}; region queries need BGZF + index")
        reasons.append(f"range probe HTTP {probe.status}")
        out.readiness = Readiness(state=state, reasons=reasons, checked_at=utcnow())
        return out

    @operation("transfer_timeout_s")
    async def fetch_file(self, file: FileRef, *, workspace: Path,
                         budget_bytes: int = DEFAULT_FILE_BUDGET) -> Artifact:
        """Download a listed ENA file (and its paired index) over HTTPS within budget; verify MD5s."""
        if not file.uri.startswith("https://"):
            raise UnsupportedError("FTP-only ENA locations need explicit preparation", source=SOURCE)
        if budget_bytes <= 0:
            raise InvalidInputError("budget_bytes must be positive", source=SOURCE)
        dest = artifact_path(workspace, file.uri.rsplit("/", 1)[-1])
        dl = await self.files.download(file.uri, dest, budget_bytes=budget_bytes,
                                       expected_size=file.size_bytes)
        md5 = next((c.value for c in file.checksums if c.algorithm == "md5"), None)
        self._verify(dest, dl, md5, file.size_bytes)
        index_path = None
        index_verified = None
        if file.index_uri:
            meta = file.native.get("index") or {}
            isize = int(meta["bytes"]) if str(meta.get("bytes") or "").isdigit() else None
            remaining = budget_bytes - dl.size_bytes
            if remaining <= 0 or (isize is not None and isize > remaining):
                dest.unlink(missing_ok=True)
                raise BudgetExceededError("the file and its index exceed the transfer budget", source=SOURCE,
                                          details={"budget_bytes": budget_bytes})
            idest = artifact_path(workspace, file.index_uri.rsplit("/", 1)[-1])
            imd5 = meta.get("md5") or None
            try:
                idx = await self.files.download(file.index_uri, idest, budget_bytes=remaining,
                                                expected_size=isize)
                self._verify(idest, idx, imd5, isize)
            except BaseException:
                dest.unlink(missing_ok=True)  # never leave a data file without its promised index
                raise
            index_path, index_verified = str(idx.path), imd5 is not None
        steps = ["streamed within budget", "md5 verified against ENA" if md5 else "no ENA md5 available"]
        if index_path:
            steps.append("index md5 verified against ENA" if index_verified else "index has no ENA md5")
        prov = self._prov(file.uri, file.accession, "HTTPS download from ENA file server", transformations=steps)
        return Artifact(path=str(dl.path), size_bytes=dl.size_bytes,
                        checksums=[Checksum(algorithm="md5", value=dl.md5),
                                   Checksum(algorithm="sha256", value=dl.sha256)],
                        checksum_verified=md5 is not None, format=file.format, index_path=index_path,
                        origin=file, provenance=prov)

    @staticmethod
    def _verify(dest: Path, dl: Any, md5: str | None, size: int | None) -> None:
        if md5 is not None and dl.md5 != md5.lower():
            dest.unlink(missing_ok=True)
            raise UpstreamError("downloaded ENA file failed its MD5 check; file removed", source=SOURCE,
                                details={"expected_md5": md5, "observed_md5": dl.md5})
        if size is not None and dl.size_bytes != size:
            dest.unlink(missing_ok=True)
            raise UpstreamError("downloaded ENA file size differs from ENA's bytes; file removed",
                                source=SOURCE, details={"expected": size, "observed": dl.size_bytes})

    @operation("transfer_timeout_s")
    async def fetch_sequence_fasta(self, accession: str, *, workspace: Path,
                                   budget_bytes: int = DEFAULT_FILE_BUDGET) -> Artifact:
        """Download an INSDC sequence as FASTA (Browser API) and build a .fai (explicit preparation).

        The resolved accession.version is read from the returned FASTA header and reported as the
        artifact's accession (`native.requested_accession` keeps what was asked for)."""
        import asyncio

        import pysam

        acc = accession.strip()
        if classify(acc) != "sequence":
            raise InvalidInputError(f"{acc} is not an INSDC sequence accession", source=SOURCE)
        url = f"{BROWSER}/fasta/{acc}"
        dest = artifact_path(workspace, f"{acc}.fa")
        dl = await self.http.download(url, dest, budget_bytes=budget_bytes)
        with dest.open("rb") as fh:
            first = fh.readline(4096).decode("utf-8", errors="replace").strip()
        resolved = fasta_accession(first)
        if resolved is None:
            dest.unlink(missing_ok=True)
            raise NotFoundError(f"ENA returned no FASTA for {acc}", source=SOURCE)
        if resolved.split(".")[0] != acc.split(".")[0] or ("." in acc and resolved != acc):
            dest.unlink(missing_ok=True)
            raise UpstreamError(f"ENA FASTA for {acc} identifies {resolved}", source=SOURCE)
        await asyncio.to_thread(pysam.faidx, str(dest))
        md5, sha = file_digests(dest)
        origin = (await self._sequence_file(acc)).items[0]
        origin.accession = resolved
        origin.native.update({"requested_accession": acc, "resolved_accession": resolved,
                              "sequence_version": resolved.rsplit(".", 1)[1] if "." in resolved else None,
                              "fasta_header": first[:300]})
        prov = self._prov(url, resolved, "ENA Browser API FASTA",
                          transformations=["downloaded within budget",
                                           f"resolved {acc} -> {resolved} from the FASTA header",
                                           "indexed locally with pysam.faidx"])
        if "." in resolved:
            prov.source_version = f"sequence version {resolved.rsplit('.', 1)[1]}"
        return Artifact(path=str(dest), size_bytes=dl.size_bytes,
                        checksums=[Checksum(algorithm="md5", value=md5), Checksum(algorithm="sha256", value=sha)],
                        checksum_verified=False, format=FileFormat.FASTA, index_path=f"{dest}.fai",
                        origin=origin, provenance=prov)
