"""NCBI GEO client: E-utilities (db=gds) search/summary and GEO SOFT records via acc.cgi.

Checked 2026-09-24:

* `esearch`/`esummary` (https://eutils.ncbi.nlm.nih.gov) page natively with retstart/retmax.
  Without an API key NCBI allows 3 requests/second; requests are spaced accordingly.
* `acc.cgi?acc=X&targ=self&form=text&view=brief` returns SOFT. Unknown accessions answer
  200 with an HTML page containing "Could not find a public or private accession".
* Supplementary files are listed as ftp://ftp.ncbi.nlm.nih.gov/geo/... and are served over
  HTTPS from the same host with byte ranges; they are converted to https://.
* GSE (series), GSM (sample) and GPL (platform) are distinct: describe_dataset takes GSE,
  sample operations take GSM, platforms go through describe_platform.
* Sample characteristics are free text `key: value` lines, heterogeneous across series; they
  are copied verbatim. Assemblies in data-processing text are not parsed or trusted.
* Contact fields (names, emails, addresses) are omitted from returned native metadata.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import SecretStr

from genomics_mcp.archives._common.deadline import DEFAULT_OPERATION_TIMEOUT_S, operation
from genomics_mcp.archives._common.errors import (
    InvalidInputError,
    NotFoundError,
    UnsupportedError,
    UpstreamError,
)
from genomics_mcp.archives._common.http import SourceHttp, SourcePolicy
from genomics_mcp.archives._common.models import (
    AccessStatus,
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
    compression_from_name,
    infer_format,
)
from genomics_mcp.archives._common.paging import next_offset_cursor, offset_from, page_size
from genomics_mcp.archives._common.redact import register_secret
from genomics_mcp.catalogs._remote import verify_remote

SOURCE = "geo"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ACC_CGI = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"
TERMS_URL = "https://www.ncbi.nlm.nih.gov/home/about/policies/"
API_HOSTS = frozenset({"eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"})
FILE_HOSTS = frozenset({"ftp.ncbi.nlm.nih.gov"})
MAX_SAMPLES_PER_PAGE = 50

_ACC = re.compile(r"^(GSE|GSM|GPL|GDS)\d+$")
_CONTACT = re.compile(r"_contact_|_contributor", re.I)


def geo_kind(acc: str) -> str:
    m = _ACC.match(acc.strip())
    if not m:
        raise InvalidInputError(
            f"not a GEO accession: {acc!r}",
            source=SOURCE,
            hint="Expected GSE, GSM, GPL or GDS followed by digits.",
        )
    return m.group(1)


def parse_soft(text: str) -> list[tuple[str, str, dict[str, list[str]]]]:
    """SOFT text -> [(entity_type, accession, {attribute: [values...]})]. Table sections skipped."""
    entities: list[tuple[str, str, dict[str, list[str]]]] = []
    current: dict[str, list[str]] | None = None
    in_table = False
    for line in text.splitlines():
        if line.startswith("^"):
            kind, _, acc = line[1:].partition("=")
            current = {}
            entities.append((kind.strip().upper(), acc.strip(), current))
            in_table = False
        elif line.startswith("!") and current is not None:
            key, _, value = line[1:].partition("=")
            key = key.strip()
            if key.endswith("_table_begin"):
                in_table = True
            elif key.endswith("_table_end"):
                in_table = False
            elif not in_table:
                current.setdefault(key, []).append(value.strip())
    return entities


def to_https(url: str) -> tuple[str, bool]:
    u = url.strip()
    for prefix in ("ftp://ftp.ncbi.nlm.nih.gov/", "http://ftp.ncbi.nlm.nih.gov/"):
        if u.startswith(prefix):
            return "https://ftp.ncbi.nlm.nih.gov/" + u[len(prefix) :], True
    return u, u.startswith("https://")


def _first(attrs: dict[str, list[str]], key: str) -> str | None:
    v = attrs.get(key)
    return v[0] if v else None


def _public(attrs: dict[str, list[str]]) -> dict[str, Any]:
    return {k: (v[0] if len(v) == 1 else v) for k, v in attrs.items() if not _CONTACT.search(k)}


class GeoClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: SecretStr | None = None,
        timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S,
    ) -> None:
        self.source = SOURCE
        self.operation_timeout_s = timeout_s
        self.api_key = api_key
        if api_key is not None:
            register_secret(api_key.get_secret_value())
        interval = 0.11 if api_key else 0.34
        self.http = SourceHttp(
            client,
            SourcePolicy(
                SOURCE, API_HOSTS, min_interval_s=interval, timeout_s=timeout_s, terms_url=TERMS_URL
            ),
        )
        self.files = SourceHttp(
            client,
            SourcePolicy(
                SOURCE, FILE_HOSTS, min_interval_s=0.1, timeout_s=timeout_s, terms_url=TERMS_URL
            ),
        )

    def _eparams(self, **kw: Any) -> dict[str, Any]:
        p = {"db": "gds", "retmode": "json", "tool": "rewire-genomics-mcp", **kw}
        if self.api_key is not None:
            p["api_key"] = self.api_key.get_secret_value()
        return p

    async def _soft(self, acc: str) -> tuple[dict[str, list[str]], Provenance]:
        url = ACC_CGI
        params = {"acc": acc, "targ": "self", "form": "text", "view": "brief"}
        text, res = await self.http.get_text(url, params=params)
        if not text.lstrip().startswith("^"):
            if "Could not find a public or private accession" in text:
                raise NotFoundError(f"GEO has no public record {acc}", source=SOURCE)
            raise UpstreamError(
                "GEO returned a non-SOFT response",
                source=SOURCE,
                details={"http_status": res.status},
            )
        entities = parse_soft(text)
        match = next((e for e in entities if e[1] == acc), None)
        if match is None:
            raise UpstreamError(f"GEO SOFT response did not contain {acc}", source=SOURCE)
        prov = Provenance(
            source=SOURCE,
            source_record_id=acc,
            url=res.url,
            method="GEO acc.cgi SOFT (brief)",
            terms_url=TERMS_URL,
        )
        return match[2], prov

    # -- conversions --------------------------------------------------------------
    def _file(self, url: str, owner_kind: EntityKind, owner: str) -> FileRef:
        uri, https = to_https(url)
        name = uri.rsplit("/", 1)[-1]
        fmt = infer_format(name)
        if not https:
            readiness = Readiness(
                state=ReadinessState.DOWNLOAD_REQUIRED,
                reasons=["not served over HTTPS; explicit preparation required"],
            )
        elif name.endswith("_RAW.tar"):
            readiness = Readiness(
                state=ReadinessState.DOWNLOAD_REQUIRED,
                reasons=[
                    "tar bundle of the per-sample supplementary files, which are "
                    "listed individually"
                ],
            )
        elif fmt in (FileFormat.BIGWIG, FileFormat.BIGBED):
            readiness = Readiness(
                state=ReadinessState.UNKNOWN,
                reasons=["remote range reads not yet verified; call check_file"],
            )
        elif fmt == FileFormat.FASTQ:
            readiness = Readiness(
                state=ReadinessState.NOT_LOCUS_READY, reasons=["FASTQ is not locus-queryable"]
            )
        elif fmt in (
            FileFormat.BAM,
            FileFormat.CRAM,
            FileFormat.VCF,
            FileFormat.BED,
            FileFormat.GFF3,
            FileFormat.GTF,
        ):
            readiness = Readiness(
                state=ReadinessState.INDEX_REQUIRED,
                reasons=["GEO lists no index; download, (re)compress with BGZF and index"],
            )
        else:
            readiness = Readiness(
                state=ReadinessState.DOWNLOAD_REQUIRED,
                reasons=["supplementary file; not region-queryable as listed"],
            )
        return FileRef(
            uri=uri,
            format=fmt or FileFormat.OTHER,
            compression=compression_from_name(name),
            source=SOURCE,
            accession=owner,
            access_status=AccessStatus.OPEN,
            visibility=Visibility.PUBLIC,
            relationships=[
                EntityLink(
                    relation="supplementary_file_of",
                    kind=owner_kind,
                    accession=owner,
                    source=SOURCE,
                )
            ],
            readiness=readiness,
            native={"geo_url": url, "file_name": name},
        )

    def _sample(self, acc: str, a: dict[str, list[str]], prov: Provenance) -> Sample:
        channels = int(_first(a, "Sample_channel_count") or "1")
        phen: list[PhenotypeValue] = []
        for ch in range(1, channels + 1):
            for line in a.get(f"Sample_characteristics_ch{ch}", []):
                key, sep, value = line.partition(":")
                name = key.strip() if sep else f"characteristics_ch{ch}"
                if channels > 1:
                    name = f"{name} (ch{ch})"
                phen.append(
                    PhenotypeValue(name=name, value=value.strip() if sep else line, source=SOURCE)
                )
        if channels > 1:
            prov.transformations.append("characteristic names suffixed with their channel (chN)")
        links = [
            EntityLink(relation="part_of", kind=EntityKind.STUDY, accession=s, source=SOURCE)
            for s in a.get("Sample_series_id", [])
        ]
        for rel in a.get("Sample_relation", []):
            kind, _, target = rel.partition(":")
            tail = target.strip().rstrip("/").rsplit("/", 1)[-1].split("=")[-1]
            if kind.strip() == "BioSample" and tail:
                links.append(
                    EntityLink(
                        relation="biosample", kind=EntityKind.SAMPLE, accession=tail, source=SOURCE
                    )
                )
            elif kind.strip() == "SRA" and tail:
                links.append(
                    EntityLink(
                        relation="sra_experiment",
                        kind=EntityKind.EXPERIMENT,
                        accession=tail,
                        source=SOURCE,
                    )
                )
        taxid = _first(a, "Sample_taxid_ch1")
        files = [
            self._file(u, EntityKind.SAMPLE, acc)
            for k, v in a.items()
            if k.startswith("Sample_supplementary_file")
            for u in v
            if u and u.upper() != "NONE"
        ]
        native = _public(a)
        native["supplementary_files"] = [f.uri for f in files]
        native["platforms"] = a.get("Sample_platform_id", [])
        return Sample(
            accession=acc,
            source=SOURCE,
            title=_first(a, "Sample_title"),
            description=_first(a, "Sample_description"),
            organism=_first(a, "Sample_organism_ch1"),
            taxon_id=int(taxid) if taxid and taxid.isdigit() else None,
            phenotypes=phen,
            links=links,
            native=native,
            provenance=[prov],
        )

    # -- discovery ----------------------------------------------------------------
    @operation()
    async def search_datasets(
        self,
        query: str,
        *,
        organism: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> SourcePage[Dataset]:
        q = query.strip()
        if not q:
            raise InvalidInputError("GEO search needs a query", source=SOURCE)
        n = page_size(limit, maximum=100)
        term = f"({q}) AND gse[Entry Type]"
        if organism:
            safe = re.sub(r'["\[\]()]', " ", organism).strip()
            term += f' AND "{safe}"[Organism]'
        scope = f"search:{term}"
        off = offset_from(cursor, SOURCE, scope)
        data, res = await self.http.get_json(
            f"{EUTILS}/esearch.fcgi", params=self._eparams(term=term, retstart=off, retmax=n)
        )
        r = (data or {}).get("esearchresult")
        if not isinstance(r, dict):
            raise UpstreamError("GEO esearch returned no result block", source=SOURCE)
        if r.get("ERROR"):
            raise InvalidInputError(f"GEO esearch rejected the query: {r['ERROR']}", source=SOURCE)
        total = int(r.get("count", 0))
        ids = r.get("idlist", [])
        prov = Provenance(
            source=SOURCE,
            url=res.url,
            method="NCBI E-utilities esearch db=gds",
            terms_url=TERMS_URL,
        )
        items: list[Dataset] = []
        if ids:
            summ, sres = await self.http.get_json(
                f"{EUTILS}/esummary.fcgi", params=self._eparams(id=",".join(ids))
            )
            result = (summ or {}).get("result", {})
            sprov = Provenance(
                source=SOURCE,
                url=sres.url,
                method="NCBI E-utilities esummary db=gds",
                terms_url=TERMS_URL,
            )
            for uid in result.get("uids", []):
                s = result.get(uid, {})
                acc = s.get("accession")
                if not acc:
                    continue
                items.append(
                    Dataset(
                        accession=acc,
                        source=SOURCE,
                        title=s.get("title"),
                        description=s.get("summary"),
                        access_status=AccessStatus.OPEN,
                        native={
                            k: s.get(k)
                            for k in (
                                "entrytype",
                                "gdstype",
                                "taxon",
                                "n_samples",
                                "gpl",
                                "suppfile",
                                "pdat",
                                "bioproject",
                                "pubmedids",
                            )
                        },
                        provenance=[sprov],
                    )
                )
        return SourcePage(
            items=items,
            total=total,
            provenance=[prov],
            next_cursor=next_offset_cursor(SOURCE, scope, off, len(ids), total),
        )

    @operation()
    async def describe_dataset(self, accession: str) -> DatasetDetail:
        acc = accession.strip()
        kind = geo_kind(acc)
        if kind == "GSM":
            raise InvalidInputError(
                f"{acc} is a GEO sample; use get_sample_metadata", source=SOURCE
            )
        if kind == "GPL":
            raise InvalidInputError(
                f"{acc} is a GEO platform; use describe_platform", source=SOURCE
            )
        if kind == "GDS":
            raise UnsupportedError(
                "GDS curated datasets are not described here; use the parent GSE", source=SOURCE
            )
        a, prov = await self._soft(acc)
        # GPL platforms are measurement platforms, not genomic references: kept in native/related
        # (`platforms`) rather than as EntityLinks, whose kinds have no platform type.
        links: list[EntityLink] = []
        for rel in a.get("Series_relation", []):
            kind_, _, target = rel.partition(":")
            tail = target.strip().rstrip("/").rsplit("/", 1)[-1].split("=")[-1]
            if kind_.strip() == "BioProject" and tail:
                links.append(
                    EntityLink(
                        relation="bioproject", kind=EntityKind.STUDY, accession=tail, source=SOURCE
                    )
                )
            elif kind_.strip() in ("SuperSeries of", "SubSeries of") and tail:
                links.append(
                    EntityLink(
                        relation=kind_.strip().lower().replace(" ", "_"),
                        kind=EntityKind.STUDY,
                        accession=tail,
                        source=SOURCE,
                    )
                )
        samples = a.get("Series_sample_id", [])
        ds = Dataset(
            accession=acc,
            source=SOURCE,
            title=_first(a, "Series_title"),
            description=_first(a, "Series_summary"),
            access_status=AccessStatus.OPEN,
            links=links,
            native=_public(a),
            provenance=[prov],
        )
        related = {
            "sample_count": len(samples),
            "series_supplementary_files": [
                to_https(u)[0] for u in a.get("Series_supplementary_file", [])
            ],
            "platforms": a.get("Series_platform_id", []),
        }
        return DatasetDetail(dataset=ds, related=related, provenance=[prov])

    @operation()
    async def describe_platform(self, accession: str) -> dict[str, Any]:
        """Platform summary from esummary (GPL SOFT lists every sample and can exceed 10 MB).

        GEO's db=gds UIDs encode GPL n as 100000000+n; the returned accession is checked."""
        acc = accession.strip()
        if geo_kind(acc) != "GPL":
            raise InvalidInputError(f"{acc} is not a GEO platform (GPL)", source=SOURCE)
        uid = str(100_000_000 + int(acc[3:]))
        data, res = await self.http.get_json(
            f"{EUTILS}/esummary.fcgi", params=self._eparams(id=uid)
        )
        rec = ((data or {}).get("result") or {}).get(uid)
        if not isinstance(rec, dict) or rec.get("accession") != acc:
            raise NotFoundError(f"GEO has no public platform {acc}", source=SOURCE)
        native = {
            k: rec.get(k)
            for k in ("title", "taxon", "gdstype", "ptechtype", "entrytype", "pdat", "n_samples")
        }
        native["series_count"] = len([x for x in str(rec.get("gse") or "").split(";") if x])
        prov = Provenance(
            source=SOURCE,
            source_record_id=acc,
            url=res.url,
            method="NCBI E-utilities esummary db=gds",
            terms_url=TERMS_URL,
        )
        return {"accession": acc, "kind": "platform", "native": native, "provenance": [prov]}

    async def _series_samples(self, acc: str) -> tuple[list[str], dict[str, list[str]], Provenance]:
        a, prov = await self._soft(acc)
        return a.get("Series_sample_id", []), a, prov

    @operation()
    async def list_files(
        self, accession: str, *, limit: int | None = None, cursor: str | None = None
    ) -> SourcePage[FileRef]:
        """Series supplementary files, then each sample's files; paged by sample."""
        acc = accession.strip()
        kind = geo_kind(acc)
        if kind == "GSM":
            a, prov = await self._soft(acc)
            files = self._sample(acc, a, prov).native["supplementary_files"]
            return SourcePage(
                items=[self._file(u, EntityKind.SAMPLE, acc) for u in files],
                total=len(files),
                provenance=[prov],
            )
        if kind != "GSE":
            raise InvalidInputError("list_files takes a GSE or GSM accession", source=SOURCE)
        n = page_size(limit, default=20, maximum=MAX_SAMPLES_PER_PAGE)
        sample_ids, a, prov = await self._series_samples(acc)
        scope = f"files:{acc}"
        off = offset_from(cursor, SOURCE, scope)
        items: list[FileRef] = []
        provs = [prov]
        if off == 0:
            items += [
                self._file(u, EntityKind.STUDY, acc)
                for u in a.get("Series_supplementary_file", [])
                if u.upper() != "NONE"
            ]
        page = sample_ids[off : off + n]
        for gsm in page:
            sa, sprov = await self._soft(gsm)
            provs.append(sprov)
            items += [
                self._file(u, EntityKind.SAMPLE, gsm)
                for k, v in sa.items()
                if k.startswith("Sample_supplementary_file")
                for u in v
                if u and u.upper() != "NONE"
            ]
        return SourcePage(
            items=items,
            provenance=provs,
            next_cursor=next_offset_cursor(SOURCE, scope, off, len(page), len(sample_ids)),
            warnings=[
                f"paged by sample ({len(sample_ids)} samples); series-level files "
                "are on the first page"
            ],
        )

    @operation()
    async def list_samples(
        self, accession: str, *, limit: int | None = None, cursor: str | None = None
    ) -> SourcePage[Sample]:
        acc = accession.strip()
        if geo_kind(acc) != "GSE":
            raise InvalidInputError("list_samples takes a GSE accession", source=SOURCE)
        n = page_size(limit, default=20, maximum=MAX_SAMPLES_PER_PAGE)
        sample_ids, _, prov = await self._series_samples(acc)
        scope = f"samples:{acc}"
        off = offset_from(cursor, SOURCE, scope)
        page = sample_ids[off : off + n]
        items = []
        for gsm in page:
            sa, sprov = await self._soft(gsm)
            items.append(self._sample(gsm, sa, sprov))
        return SourcePage(
            items=items,
            total=len(sample_ids),
            provenance=[prov],
            next_cursor=next_offset_cursor(SOURCE, scope, off, len(page), len(sample_ids)),
        )

    @operation()
    async def get_sample_metadata(self, accession: str) -> Sample:
        acc = accession.strip()
        if geo_kind(acc) != "GSM":
            raise InvalidInputError(f"{acc} is not a GEO sample (GSM)", source=SOURCE)
        a, prov = await self._soft(acc)
        return self._sample(acc, a, prov)

    @operation()
    async def check_file(self, file: FileRef) -> FileRef:
        if not file.uri.startswith("https://"):
            raise UnsupportedError("only HTTPS GEO files can be range-checked", source=SOURCE)
        return await verify_remote(self.files, file)
