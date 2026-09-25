"""ENCODE portal client (JSON REST, https://www.encodeproject.org/help/rest-api/).

Checked 2026-09-24:

* `/search/?type=...&format=json` reports `total`; a `from=` offset was refused (HTTP 403),
  so pages are built from a sorted accession list (`field=accession`, bounded by MAX_RECORDS)
  and the page's objects are fetched by accession.
* File `href` (`/files/X/@@download/X.ext`) answers 307 to a short-lived signed S3 URL.
  `cloud_metadata.url` is the unsigned object in the public `encode-public` bucket and answers
  anonymous `Range` requests with 206; `check_file` verifies that (plus bigWig/bigBed magic)
  before reporting a file ready. Signed URLs are never returned. `azure_uri` values carry SAS
  signatures and are dropped from native metadata.
* Assemblies are per file (an experiment can list mm10 while an archived file is mm9).
"""

from __future__ import annotations

from typing import Any

import httpx

from genomics_mcp.archives._common.deadline import DEFAULT_OPERATION_TIMEOUT_S, operation
from genomics_mcp.archives._common.errors import InvalidInputError, NotFoundError, UpstreamError
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy
from genomics_mcp.archives._common.models import (
    AccessStatus,
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
    Visibility,
)
from genomics_mcp.archives._common.paging import (
    MAX_RECORDS,
    next_offset_cursor,
    offset_from,
    page_size,
)
from genomics_mcp.catalogs._remote import verify_remote

SOURCE = "encode"
BASE = "https://www.encodeproject.org"
TERMS_URL = "https://www.encodeproject.org/help/citing-encode/"
API_HOSTS = frozenset({"www.encodeproject.org"})
DATA_HOSTS = frozenset({"encode-public.s3.amazonaws.com", "www.encodeproject.org"})
MAX_OBJECT_BYTES = 8 * 1024 * 1024

_FORMATS = {
    "bigwig": FileFormat.BIGWIG,
    "bigbed": FileFormat.BIGBED,
    "bam": FileFormat.BAM,
    "fastq": FileFormat.FASTQ,
    "bed": FileFormat.BED,
    "vcf": FileFormat.VCF,
    "tsv": FileFormat.TSV,
    "gtf": FileFormat.GTF,
    "gff": FileFormat.GFF3,
    "fasta": FileFormat.FASTA,
    "cram": FileFormat.CRAM,
}
_DATASET_TYPES = (
    "Experiment",
    "Annotation",
    "FunctionalCharacterizationExperiment",
    "Reference",
    "Project",
    "PublicationData",
    "ComputationalModel",
    "SingleCellUnit",
    "TransgenicEnhancerExperiment",
    "Series",
)
_DROP_NATIVE = {
    "azure_uri",
    "internal_tags",
    "submitted_by",
    "lab",
    "award",
    "documents",
    "analysis_step_version",
    "quality_metrics",
    "audit",
}
_BIOSAMPLE_FIELDS = (
    "sex",
    "age",
    "age_units",
    "age_display",
    "life_stage",
    "health_status",
    "disease_term_name",
    "disease_term_id",
    "treatments_phrase",
    "summary",
)


def _check_acc(acc: str, prefixes: tuple[str, ...]) -> str:
    a = acc.strip()
    if not a.startswith(prefixes) or len(a) < 11:
        raise InvalidInputError(
            f"expected an ENCODE accession starting with {'/'.join(prefixes)}: {acc!r}",
            source=SOURCE,
        )
    return a


def _native(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k not in _DROP_NATIVE}


FILE_FIELDS = (
    "@id",
    "accession",
    "file_format",
    "file_format_type",
    "file_type",
    "output_type",
    "output_category",
    "assembly",
    "genome_annotation",
    "file_size",
    "md5sum",
    "href",
    "status",
    "dataset",
    "replicate",
    "biological_replicates",
    "technical_replicates",
    "derived_from",
    "cloud_metadata",
    "no_file_available",
    "restricted",
    "date_created",
)
DATASET_FIELDS = (
    "@id",
    "@type",
    "accession",
    "assay_title",
    "assay_term_name",
    "description",
    "biosample_summary",
    "assembly",
    "status",
    "date_released",
    "target.label",
    "replicates.@id",
    "annotation_type",
)


def _ref_acc(path: Any) -> str | None:
    """'/files/ENCFF001JBR/' -> 'ENCFF001JBR'; embedded objects use their accession or @id."""
    if isinstance(path, dict):
        path = path.get("accession") or path.get("@id")
    if isinstance(path, str):
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else None
    return None


def dataset_kind(ref: Any) -> EntityKind:
    """ENCODE datasets are typed by collection: /experiments/ -> experiment, anything else
    (annotations, references, series, ...) -> the neutral dataset kind."""
    path = ref.get("@id", "") if isinstance(ref, dict) else str(ref)
    return EntityKind.EXPERIMENT if path.startswith("/experiments/") else EntityKind.DATASET


class EncodeClient:
    def __init__(
        self, client: httpx.AsyncClient, *, timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S
    ) -> None:
        self.source = SOURCE
        self.operation_timeout_s = timeout_s
        self.http = SourceHttp(
            client,
            SourcePolicy(
                SOURCE,
                API_HOSTS,
                min_interval_s=0.1,
                timeout_s=timeout_s,
                terms_url=TERMS_URL,
                max_body_bytes=MAX_OBJECT_BYTES,
            ),
        )
        self.data = SourceHttp(
            client,
            SourcePolicy(
                SOURCE, DATA_HOSTS, min_interval_s=0.1, timeout_s=timeout_s, terms_url=TERMS_URL
            ),
        )

    def _prov(
        self, url: str, record: str | None, method: str, obj: dict | None = None
    ) -> Provenance:
        version = (
            f"schema_version {obj['schema_version']}" if obj and obj.get("schema_version") else None
        )
        return Provenance(
            source=SOURCE,
            source_record_id=record,
            url=url,
            method=method,
            source_version=version,
            terms_url=TERMS_URL,
        )

    async def _object(self, path: str, frame: str = "object") -> tuple[dict, Provenance]:
        url = f"{BASE}{path}"
        data, res = await self.http.get_json(url, params={"format": "json", "frame": frame})
        if not isinstance(data, dict) or not data.get("@id"):
            raise NotFoundError(f"ENCODE has no object at {path}", source=SOURCE)
        return data, self._prov(res.url, data.get("accession"), "ENCODE REST object", data)

    async def _search_ids(
        self, params: dict[str, Any], off: int, n: int
    ) -> tuple[list[str], int, Provenance]:
        url = f"{BASE}/search/"
        q = {**params, "format": "json", "field": "accession", "limit": MAX_RECORDS + 1}
        data, res = await self.http.get_json(url, params=q, ok=(200, 404))
        if res.status == 404:  # ENCODE answers 404 with an empty @graph when nothing matches
            return [], 0, self._prov(res.url, None, "ENCODE REST search")
        graph = (data or {}).get("@graph", [])
        total = (data or {}).get("total")
        if isinstance(total, int) and total > MAX_RECORDS:
            raise InvalidInputError(
                f"{total} ENCODE records match; refine the query (limit {MAX_RECORDS})",
                source=SOURCE,
            )
        ids = sorted(g["accession"] for g in graph if g.get("accession"))
        prov = self._prov(res.url, None, "ENCODE REST search")
        prov.transformations.append(f"sorted {len(ids)} accessions; page [{off},{off + n})")
        return ids[off : off + n], len(ids), prov

    async def _search_rows(self, type_: str, ids: list[str], fields: tuple[str, ...]) -> list[dict]:
        """One search request returning the listed accessions' fields, in `ids` order."""
        if not ids:
            return []
        params: list[tuple[str, Any]] = [("type", type_), ("format", "json"), ("limit", len(ids))]
        params += [("accession", i) for i in ids] + [("field", f) for f in fields]
        data, _ = await self.http.get_json(f"{BASE}/search/", params=params)
        rows = {g.get("accession"): g for g in (data or {}).get("@graph", [])}
        missing = [i for i in ids if i not in rows]
        if missing:
            raise UpstreamError(
                f"ENCODE search omitted {len(missing)} listed records",
                source=SOURCE,
                details={"missing": missing[:10]},
            )
        return [rows[i] for i in ids]

    # -- conversions --------------------------------------------------------------
    def _dataset(self, obj: dict, prov: Provenance) -> Dataset:
        # Replicates are not samples; they stay in `native` (list_samples follows them to biosamples).
        links: list[EntityLink] = []
        assembly = obj.get("assembly") or []
        return Dataset(
            accession=obj["accession"],
            source=SOURCE,
            title=obj.get("description") or obj.get("assay_title"),
            description=obj.get("biosample_summary") or obj.get("description"),
            access_status=AccessStatus.OPEN,
            assemblies=list(assembly) if isinstance(assembly, list) else [assembly],
            file_count=len(obj["files"]) if isinstance(obj.get("files"), list) else None,
            links=links,
            native=_native(
                {
                    k: v
                    for k, v in obj.items()
                    if k not in ("files", "original_files", "contributing_files")
                }
            ),
            provenance=[prov],
        )

    def _file(self, f: dict, prov: Provenance) -> FileRef:
        fmt_raw = str(f.get("file_format") or "").lower()
        fmt = _FORMATS.get(fmt_raw, FileFormat.OTHER if fmt_raw else None)
        href = f.get("href")
        cloud = (f.get("cloud_metadata") or {}).get("url")
        uri = cloud or (f"{BASE}{href}" if href else None)
        if uri is None:
            raise UpstreamError(
                f"ENCODE file {f.get('accession')} has no download location", source=SOURCE
            )
        rel = []
        if f.get("dataset"):
            rel.append(
                EntityLink(
                    relation="part_of",
                    kind=dataset_kind(f["dataset"]),
                    accession=_ref_acc(f["dataset"]),
                    source=SOURCE,
                )
            )
        for d in f.get("derived_from") or []:
            rel.append(
                EntityLink(
                    relation="derived_from",
                    kind=EntityKind.FILE,
                    accession=_ref_acc(d),
                    source=SOURCE,
                )
            )
        checks = [Checksum(algorithm="md5", value=f["md5sum"])] if f.get("md5sum") else []
        restricted = bool(f.get("restricted")) or bool(f.get("no_file_available"))
        compression = None
        if fmt in (
            FileFormat.BED,
            FileFormat.VCF,
            FileFormat.TSV,
            FileFormat.FASTQ,
            FileFormat.GTF,
        ) and str(href or cloud or "").endswith(".gz"):
            compression = Compression.UNKNOWN
        native = _native(f)
        native["href"] = f"{BASE}{href}" if href else None
        if restricted:
            readiness = Readiness(
                state=ReadinessState.UNSUPPORTED,
                reasons=["ENCODE marks this file restricted or unavailable"],
            )
        elif fmt == FileFormat.FASTQ:
            readiness = Readiness(
                state=ReadinessState.NOT_LOCUS_READY, reasons=["FASTQ is not locus-queryable"]
            )
        elif fmt in (FileFormat.BIGWIG, FileFormat.BIGBED):
            readiness = Readiness(
                state=ReadinessState.UNKNOWN,
                reasons=["remote range reads not yet verified; call check_file"],
            )
        elif fmt in (FileFormat.BAM, FileFormat.VCF, FileFormat.BED):
            readiness = Readiness(
                state=ReadinessState.INDEX_REQUIRED,
                reasons=[
                    "ENCODE does not publish a companion index; download and index "
                    "locally (BED/VCF must be BGZF-compressed first)"
                ],
            )
        else:
            readiness = Readiness(
                state=ReadinessState.DOWNLOAD_REQUIRED,
                reasons=[f"{fmt_raw or 'unknown'} files are downloadable, not region-queryable"],
            )
        return FileRef(
            uri=uri,
            format=fmt,
            compression=compression,
            assembly=f.get("assembly"),
            source=SOURCE,
            accession=f.get("accession"),
            access_status=AccessStatus.DENIED if restricted else AccessStatus.OPEN,
            visibility=Visibility.PUBLIC,
            size_bytes=f.get("file_size"),
            checksums=checks,
            relationships=rel,
            readiness=readiness,
            native=native,
        )

    def _biosample(self, b: dict, prov: Provenance, links: list[EntityLink]) -> Sample:
        organism = b.get("organism")
        org_name = organism.get("scientific_name") if isinstance(organism, dict) else None
        taxon = organism.get("taxon_id") if isinstance(organism, dict) else None
        ont = b.get("biosample_ontology")
        phen = [
            PhenotypeValue(name=k, value=str(b[k]), source=SOURCE)
            for k in _BIOSAMPLE_FIELDS
            if b.get(k) not in (None, "", [])
        ]
        if isinstance(ont, dict) and ont.get("term_name"):
            phen.append(
                PhenotypeValue(
                    name="biosample_term_name",
                    value=ont["term_name"],
                    ontology_term=ont.get("term_id"),
                    source=SOURCE,
                )
            )
        donor = b.get("donor")
        if donor:
            links = [
                *links,
                EntityLink(
                    relation="donor",
                    kind=EntityKind.INDIVIDUAL,
                    accession=_ref_acc(donor),
                    source=SOURCE,
                ),
            ]
        return Sample(
            accession=b["accession"],
            source=SOURCE,
            title=b.get("summary"),
            description=b.get("description"),
            organism=org_name,
            taxon_id=int(taxon) if str(taxon or "").isdigit() else None,
            phenotypes=phen,
            links=links,
            native=_native({k: v for k, v in b.items() if k != "donor"}),
            provenance=[prov],
        )

    # -- discovery ----------------------------------------------------------------
    @operation()
    async def search_datasets(
        self,
        query: str,
        *,
        dataset_type: str = "Experiment",
        assembly: str | None = None,
        organism: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> SourcePage[Dataset]:
        if dataset_type not in _DATASET_TYPES:
            raise InvalidInputError(
                f"unsupported ENCODE dataset type {dataset_type!r}",
                source=SOURCE,
                details={"allowed": list(_DATASET_TYPES)},
            )
        n = page_size(limit, maximum=100)
        params = {"type": dataset_type, "status": "released"}
        if query.strip():
            params["searchTerm"] = query.strip()
        if assembly:
            params["assembly"] = assembly  # ENCODE facet; exact assembly name as ENCODE records it
        if organism:
            params["replicates.library.biosample.donor.organism.scientific_name"] = organism
        scope = f"search:{dataset_type}:{query.strip()}:{assembly}:{organism}"
        off = offset_from(cursor, SOURCE, scope)
        ids, total, prov = await self._search_ids(params, off, n)
        items = [
            self._dataset(r, prov)
            for r in await self._search_rows(dataset_type, ids, DATASET_FIELDS)
        ]
        return SourcePage(
            items=items,
            total=total,
            provenance=[prov],
            next_cursor=next_offset_cursor(SOURCE, scope, off, len(items), total),
        )

    @operation()
    async def describe_dataset(self, accession: str) -> DatasetDetail:
        acc = _check_acc(accession, ("ENCSR",))
        obj, prov = await self._object(f"/{acc}/")
        ds = self._dataset(obj, prov)
        related = {
            k: obj[k]
            for k in (
                "possible_controls",
                "related_series",
                "default_analysis",
                "analyses",
                "dbxrefs",
                "references",
            )
            if obj.get(k)
        }
        return DatasetDetail(dataset=ds, related=related, provenance=[prov])

    @operation()
    async def list_files(
        self, accession: str, *, limit: int | None = None, cursor: str | None = None
    ) -> SourcePage[FileRef]:
        acc = _check_acc(accession, ("ENCSR", "ENCFF"))
        if acc.startswith("ENCFF"):
            f, prov = await self._object(f"/files/{acc}/")
            return SourcePage(items=[self._file(f, prov)], total=1, provenance=[prov])
        obj, dprov = await self._object(f"/{acc}/")
        n = page_size(limit, maximum=100)
        scope = f"files:{acc}"
        off = offset_from(cursor, SOURCE, scope)
        ids, total, prov = await self._search_ids({"type": "File", "dataset": obj["@id"]}, off, n)
        items = [self._file(f, prov) for f in await self._search_rows("File", ids, FILE_FIELDS)]
        return SourcePage(
            items=items,
            total=total,
            provenance=[dprov, prov],
            next_cursor=next_offset_cursor(SOURCE, scope, off, len(items), total),
        )

    @operation()
    async def list_samples(
        self, accession: str, *, limit: int | None = None, cursor: str | None = None
    ) -> SourcePage[Sample]:
        """Biosamples reached through the experiment's replicate -> library -> biosample links."""
        acc = _check_acc(accession, ("ENCSR",))
        obj, prov = await self._object(f"/{acc}/", frame="embedded")
        seen: dict[str, Sample] = {}
        for rep in obj.get("replicates") or []:
            lib = rep.get("library") if isinstance(rep, dict) else None
            bio = lib.get("biosample") if isinstance(lib, dict) else None
            if not isinstance(bio, dict) or not bio.get("accession"):
                continue
            link = EntityLink(
                relation="replicate_of", kind=dataset_kind(obj), accession=acc, source=SOURCE
            )
            if bio["accession"] not in seen:
                sample = self._biosample(bio, prov, [link])
                sample.native["replicates"] = []
                seen[bio["accession"]] = sample
            seen[bio["accession"]].native["replicates"].append(
                {
                    "replicate": rep.get("@id"),
                    "biological_replicate_number": rep.get("biological_replicate_number"),
                    "technical_replicate_number": rep.get("technical_replicate_number"),
                    "library": lib.get("accession"),
                }
            )
        samples = sorted(seen.values(), key=lambda s: s.accession)
        n = page_size(limit)
        scope = f"samples:{acc}"
        off = offset_from(cursor, SOURCE, scope)
        page = samples[off : off + n]
        return SourcePage(
            items=page,
            total=len(samples),
            provenance=[prov],
            next_cursor=next_offset_cursor(SOURCE, scope, off, len(page), len(samples)),
        )

    @operation()
    async def get_sample_metadata(self, accession: str) -> Sample:
        acc = _check_acc(accession, ("ENCBS", "ENCDO"))
        if acc.startswith("ENCDO"):
            d, prov = await self._object(f"/{acc}/")
            phen = [
                PhenotypeValue(name=k, value=str(d[k]), source=SOURCE)
                for k in (
                    "sex",
                    "age",
                    "age_units",
                    "life_stage",
                    "health_status",
                    "ethnicity",
                    "strain_name",
                    "genotype",
                )
                if d.get(k) not in (None, "", [])
            ]
            return Sample(
                accession=acc,
                source=SOURCE,
                title=d.get("accession"),
                phenotypes=phen,
                native=_native(d),
                provenance=[prov],
            )
        b, prov = await self._object(f"/biosamples/{acc}/", frame="embedded")
        return self._biosample(b, prov, [])

    # -- retrieval readiness ------------------------------------------------------
    @operation()
    async def check_file(self, file: FileRef) -> FileRef:
        """Verify anonymous byte-range reads (and bigWig/bigBed magic) at the unsigned file URI."""
        if file.access_status == AccessStatus.DENIED:
            return file
        return await verify_remote(self.data, file)
