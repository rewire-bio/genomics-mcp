# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["mcp==2.2.0", "boto3==1.43.102", "pysam==0.24.1"]
# ///
"""Five real-data demonstrations over MCP stdio against an installed `genomics-mcp`.

    uv run --script scripts/demos.py --out demos/results/$(date -u +%F) \\
        --install-report dist-check.json -- /path/to/clean-venv/bin/genomics-mcp

Demos (all live except the MinIO one, which uses a local MinIO with explicit dummy keys):
  ega       EGA public test account: EGAF00007243773 (EGAD00001003338) GRCh38 chr10:[10000,10050)
  ena       ENA DQ285577.1 sequence FASTA fetched as a local artifact, then read back
  encode    ENCODE ENCFF792QDS GRCh38 bigWig, exact mean over chr1:[1000000,1001000)
  reference NC_000007.14 reference base check plus ClinVar VCV000013961 (BRAF c.1799T>A)
  minio     synthetic BAM on local MinIO (explicit profile) equals the local file; no AWS

Each demo starts a fresh server with its own work directory and dummy ambient AWS
credentials. Results keep counts, identities, checksums and dates; signed URLs and secrets
are removed. A source outage is recorded as that demo's explicit error, never as success.
The harness never imports genomics_mcp.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from _mcpclient import DUMMY_AWS, call, connect, scrub, server_env

EGA_FILE = "EGAF00007243773"
EGA_DATASET = "EGAD00001003338"
EGA_INTERVAL = {"contig": "chr10", "start": 10000, "end": 10050, "assembly": "GRCh38"}
ENCODE_FILE = "ENCFF792QDS"
ENCODE_INTERVAL = {"contig": "chr1", "start": 1_000_000, "end": 1_001_000, "assembly": "GRCh38"}
ENCODE_MEAN = 26.361254017233847
BRAF = {"assembly": "GRCh38", "contig": "7", "pos": 140753336, "ref": "A", "alt": "T"}

MINIO_ENDPOINT = "http://127.0.0.1:39000"
MINIO_BUCKET = "genomics-mcp-test"
# Local MinIO test user created for development; not an AWS account. Values are never written.
MINIO_KEY_ENV, MINIO_SECRET_ENV = "GENOMICS_DEMO_MINIO_KEY", "GENOMICS_DEMO_MINIO_SECRET"


class DemoFailure(Exception):
    pass


def check(cond: bool, what: str) -> None:
    if not cond:
        raise DemoFailure(what)


def ok(env: dict[str, Any], what: str) -> dict[str, Any]:
    if env.get("status") not in ("ok", "partial"):
        raise DemoFailure(f"{what}: {json.dumps(env.get('error'))[:800]}")
    return env["data"]


class Session:
    """One server process with its own work directory; records every call."""

    def __init__(self, command: list[str], root: Path, name: str, extra_env: dict[str, str]):
        self.command, self.name = command, name
        self.work = root / name / "work"
        self.work.mkdir(parents=True)
        self.errlog = root / name / "server.log"
        self.env = server_env({"GENOMICS_MCP_WORK_DIR": str(self.work), **extra_env})
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        self._cm = connect(self.command, self.env, self.errlog)
        self.client = await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        body = await call(self.client, tool, args)
        self.calls.append(
            {
                "tool": tool,
                "arguments": args,
                "status": body.get("status"),
                "error": body.get("error"),
                "seconds": round(time.monotonic() - started, 2),
                "provenance": body.get("provenance"),
                "source_status": body.get("source_status"),
            }
        )
        return body

    def log_has_secrets(self, secrets: list[str]) -> bool:
        text = self.errlog.read_text() if self.errlog.exists() else ""
        return any(s and s in text for s in secrets)


def workdir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


async def wait_transfer(s: Session, first: dict[str, Any], timeout: float = 300) -> dict[str, Any]:
    body = first
    deadline = time.monotonic() + timeout
    while ok(body, "transfer")["transfer"]["state"] not in ("completed", "failed", "cancelled"):
        if time.monotonic() > deadline:
            raise DemoFailure("transfer did not finish in time")
        await anyio.sleep(2)
        tid = body["data"]["transfer"]["transfer_id"]
        body = await s.call("get_transfer_status", {"transfer_id": tid})
    return body


# --- demos -------------------------------------------------------------------------------


async def demo_ega(s: Session) -> dict[str, Any]:
    ds = ok(
        await s.call("describe_dataset", {"source": "ega", "accession": EGA_DATASET}), "describe"
    )
    body = await s.call(
        "get_reads",
        {"file": {"uri": f"ega://{EGA_FILE}", "format": "bam"}, "interval": EGA_INTERVAL},
    )
    data = ok(body, "get_reads")
    provider = data["region_slice"]["provider"]
    records = data["records"]
    lo, hi = EGA_INTERVAL["start"], EGA_INTERVAL["end"]
    check(provider["records_received"] == 91, f"received {provider['records_received']} != 91")
    check(
        provider["records_overlapping"] == 42,
        f"overlapping {provider['records_overlapping']} != 42",
    )
    check(len(records) == 42, f"returned {len(records)} != 42")
    check(all(r["start"] < hi and r["end"] > lo for r in records), "record outside interval")
    return {
        "dataset": {"accession": EGA_DATASET, "title": (ds.get("dataset") or {}).get("title")},
        "file": EGA_FILE,
        "interval": EGA_INTERVAL,
        "records_received_from_htsget": provider["records_received"],
        "records_overlapping": provider["records_overlapping"],
        "records_returned": len(records),
        "record_span": [min(r["start"] for r in records), max(r["end"] for r in records)],
        "assembly": data.get("assembly"),
        "region_slice": {k: v for k, v in data["region_slice"].items() if k != "provider"},
        "access": "EGA documented public test account (GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT=1)",
    }


async def demo_ena(s: Session) -> dict[str, Any]:
    files = ok(await s.call("list_files", {"source": "ena", "accession": "DQ285577"}), "list_files")
    fasta = next((f for f in files["records"] if f.get("format") == "fasta"), None)
    check(fasta is not None, "no FASTA record for DQ285577")
    first = await s.call("fetch_file", {"file": fasta, "prepare": True})
    done = ok(await wait_transfer(s, first), "fetch_file")
    transfer = done["transfer"]
    check(
        transfer["state"] == "completed", f"transfer {transfer['state']}: {transfer.get('error')}"
    )
    artifact = transfer["artifact"]
    af = done["artifact_file"]
    path = Path(artifact["path"])
    check(path.is_file(), "artifact path missing")
    size = path.stat().st_size
    contig = Path(af["index_uri"]).read_text().split("\t")[0]
    seq = ok(
        await s.call(
            "get_sequence",
            {
                "file": af,
                "interval": {"contig": contig, "start": 0, "end": 614, "assembly": "DQ285577.1"},
            },
        ),
        "get_sequence",
    )
    bases = seq["records"][0]["sequence"]
    check(size == 756, f"artifact {size} bytes != 756")
    check(len(bases) == 614, f"{len(bases)} bases != 614")
    return {
        "accession": "DQ285577.1",
        "kind": "INSDC sequence record as FASTA (not an alignment or region query)",
        "source_uri": fasta.get("uri"),
        "artifact_bytes": size,
        "artifact_checksums": artifact.get("checksums"),
        "checksum_verified": artifact.get("checksum_verified"),
        "fasta_contig": contig,
        "bases": len(bases),
        "first_30_bases": bases[:30],
    }


async def demo_encode(s: Session) -> dict[str, Any]:
    files = ok(
        await s.call("list_files", {"source": "encode", "accession": ENCODE_FILE}), "list_files"
    )
    (f,) = files["records"]
    check(f["assembly"] == "GRCh38" and f["format"] == "bigwig", "unexpected ENCODE file record")
    body = await s.call(
        "get_signal",
        {"file": {**f, "visibility": "public"}, "interval": ENCODE_INTERVAL, "max_records": 5},
    )
    data = ok(body, "get_signal")
    mean = data["summary"]["value"]
    check(abs(mean - ENCODE_MEAN) <= 1e-12, f"mean {mean!r} != {ENCODE_MEAN!r}")
    used = workdir_bytes(s.work)
    check(used < 16 * 1024 * 1024, f"work dir grew to {used} bytes")
    return {
        "file": ENCODE_FILE,
        "file_size_bytes": f.get("size_bytes"),
        "source_md5": f.get("checksums"),
        "uri": f["uri"],
        "interval": ENCODE_INTERVAL,
        "summary": data["summary"],
        "first_records": data["records"][:3],
        "work_dir_bytes_after_query": used,
        "note": "range reads only; the 1.4 GB file was not downloaded",
    }


async def demo_reference(s: Session) -> dict[str, Any]:
    norm = ok(
        await s.call("normalize_variant", {"variant": BRAF, "sources": ["ncbi_nuccore"]}),
        "normalize",
    )
    cv = norm["canonical_variant"]
    rc = cv["reference_check"]
    check(rc["status"] == "verified" and rc["source"] == "ncbi_nuccore", f"reference check {rc}")
    check(cv["refseq_accession"] == "NC_000007.14", f"accession {cv['refseq_accession']}")
    look = ok(await s.call("lookup_variant", {"variant": BRAF, "sources": ["clinvar"]}), "lookup")
    records = look["records"]
    (vcv,) = [r for r in records if r["evidence_type"] == "clinical_variant_record"]
    scvs = [r for r in records if r["evidence_type"] == "clinical_assertion"]
    agg = vcv["data"]["aggregate_classifications"]
    for kind in ("germline", "somatic_clinical_impact", "oncogenicity"):
        check(kind in agg, f"aggregate {kind} missing")
    by_type = Counter(r["data"]["classification_type"] for r in scvs)
    for kind in ("germline", "somatic_clinical_impact", "oncogenicity"):
        check(by_type[kind] > 0, f"no {kind} submissions")
    return {
        "variant": BRAF,
        "reference": {
            "accession": cv["refseq_accession"],
            "source": rc["source"],
            "observed_ref": rc["observed_ref"],
            "spdi": cv["spdi"],
            "hgvs_g": cv["hgvs_g"],
        },
        "clinvar": {
            "vcv": vcv["data"]["vcv"],
            "name": vcv["data"]["name"],
            "source_version": vcv["provenance"]["source_version"],
            "aggregate_classifications": {
                k: {
                    x: v.get(x)
                    for x in ("description", "review_status", "conflict_reported_by_clinvar")
                }
                for k, v in agg.items()
            },
            "submissions_by_classification_type": dict(sorted(by_type.items())),
            "submitted_classification_counts": vcv["data"].get("submitted_classification_counts"),
        },
    }


def make_bam(dest: Path) -> tuple[Path, int]:
    """Synthetic BAM (60 reads on chrDemo) with an index. Returns path and the oracle count."""
    import pysam

    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": "chrDemo", "LN": 5000}]}
    unsorted = dest / "unsorted.bam"
    with pysam.AlignmentFile(str(unsorted), "wb", header=header) as out:
        for i in range(60):
            a = pysam.AlignedSegment(out.header)
            a.query_name = f"read{i:03d}"
            a.reference_id = 0
            a.reference_start = 100 + i * 50
            a.mapping_quality = 60
            a.cigarstring = "40M" if i % 3 else "20M100N20M"
            a.query_sequence = "ACGT" * 10
            a.query_qualities = pysam.qualitystring_to_array("I" * 40)
            a.flag = 0 if i % 2 else 16
            out.write(a)
    bam = dest / "synthetic.bam"
    pysam.sort("-o", str(bam), str(unsorted))
    unsorted.unlink()
    pysam.index(str(bam))
    with pysam.AlignmentFile(str(bam)) as f:
        expected = sum(1 for _ in f.fetch("chrDemo", 1000, 2000))
    return bam, expected


async def demo_minio(command: list[str], root: Path) -> tuple[dict[str, Any], list[dict]]:
    import boto3
    from botocore.config import Config

    key = os.environ.get(MINIO_KEY_ENV)
    secret = os.environ.get(MINIO_SECRET_ENV)
    if not key or not secret:
        raise DemoFailure(f"set {MINIO_KEY_ENV}/{MINIO_SECRET_ENV} to the local MinIO test user")
    data = root / "minio" / "data"
    data.mkdir(parents=True)
    bam, expected = make_bam(data)
    prefix = f"demos/{uuid.uuid4().hex}"
    s3 = boto3.session.Session().client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    for f in (bam, bam.with_suffix(".bam.bai")):
        s3.upload_file(str(f), MINIO_BUCKET, f"{prefix}/{f.name}")
    config = root / "minio" / "config.toml"
    config.write_text(
        "[storage.profiles.minio]\n"
        f'endpoint_url = "{MINIO_ENDPOINT}"\nregion = "us-east-1"\naddressing_style = "path"\n'
        f'access_key_id_env = "{MINIO_KEY_ENV}"\nsecret_access_key_env = "{MINIO_SECRET_ENV}"\n'
        f'buckets = ["{MINIO_BUCKET}"]\n'
    )
    interval = {"contig": "chrDemo", "start": 1000, "end": 2000, "assembly": "synthetic"}
    s3_file = {"uri": f"s3://{MINIO_BUCKET}/{prefix}/synthetic.bam", "storage_profile": "minio"}
    calls: list[dict] = []
    try:
        base = {"GENOMICS_MCP_CONFIG": str(config), "GENOMICS_MCP_ALLOWED_ROOTS": str(data)}
        creds = {MINIO_KEY_ENV: key, MINIO_SECRET_ENV: secret}
        async with Session(command, root / "minio", "with-profile", {**base, **creds}) as s:
            remote = ok(
                await s.call("get_reads", {"file": s3_file, "interval": interval}), "s3 get_reads"
            )
            local = ok(
                await s.call("get_reads", {"file": {"uri": str(bam)}, "interval": interval}),
                "local",
            )
            check(not s.log_has_secrets([key, secret, *DUMMY_AWS.values()]), "secret in server log")
            calls += s.calls
        # Same request without the profile's keys: must fail, not fall back to ambient AWS.
        async with Session(command, root / "minio", "without-keys", base) as s:
            denied = await s.call("get_reads", {"file": s3_file, "interval": interval})
            calls += s.calls
    finally:
        for f in (bam, bam.with_suffix(".bam.bai")):
            s3.delete_object(Bucket=MINIO_BUCKET, Key=f"{prefix}/{f.name}")

    def names(d):
        return [(r["name"], r["start"], r["end"], r["flag"]) for r in d["records"]]

    check(len(remote["records"]) == expected, f"s3 {len(remote['records'])} != oracle {expected}")
    check(names(remote) == names(local), "s3 and local records differ")
    check(denied["status"] == "error", "request without profile keys did not fail")
    return {
        "endpoint": MINIO_ENDPOINT,
        "bucket": MINIO_BUCKET,
        "object": "synthetic.bam (+ .bai), uploaded for this run and deleted afterwards",
        "interval": interval,
        "oracle_count_pysam_fetch": expected,
        "s3_records": len(remote["records"]),
        "local_records": len(local["records"]),
        "identical_to_local": True,
        "credentials": f"explicit profile keys from {MINIO_KEY_ENV}/{MINIO_SECRET_ENV} (values not recorded)",
        "ambient_aws": "dummy AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN set in the server env and unused",
        "without_profile_keys": {
            "status": denied["status"],
            "error_code": (denied.get("error") or {}).get("code"),
        },
    }, calls


# --- runner ------------------------------------------------------------------------------

DEMOS = {
    "ega": ("EGA public test BAM region", demo_ega, {"GENOMICS_MCP_EGA_PUBLIC_TEST_ACCOUNT": "1"}),
    "ena": ("ENA sequence artifact", demo_ena, {}),
    "encode": ("ENCODE bigWig signal", demo_encode, {}),
    "reference": ("Reference sequence and ClinVar evidence", demo_reference, {}),
    "minio": ("Local MinIO synthetic BAM", None, {}),
}


async def run_one(name: str, command: list[str], root: Path) -> dict[str, Any]:
    title, fn, extra = DEMOS[name]
    started = datetime.now(UTC)
    result: dict[str, Any] = {"demo": name, "title": title, "started_at": started.isoformat()}
    calls: list[dict] = []
    try:
        if fn is None:
            result["result"], calls = await demo_minio(command, root)
        else:
            async with Session(command, root, name, extra) as s:
                try:
                    result["result"] = await fn(s)
                    check(not s.log_has_secrets(list(DUMMY_AWS.values())), "dummy secret in log")
                finally:
                    calls = s.calls
        result["status"] = "passed"
    except DemoFailure as exc:
        result.update(status="failed", failure=str(exc))
    except Exception as exc:  # noqa: BLE001 - recorded in the result, never hidden
        result.update(status="failed", failure=f"{type(exc).__name__}: {exc}")
    result["finished_at"] = datetime.now(UTC).isoformat()
    result["calls"] = calls
    return scrub(result, {str(root): "$RUN", str(root.resolve()): "$RUN"})


def server_identity(command: list[str]) -> dict[str, Any]:
    env = server_env(dummy_aws=False)
    out = subprocess.run(
        [*command, "--version"], capture_output=True, text=True, env=env, check=False
    )
    return {"command": command, "version": out.stdout.strip() or out.stderr.strip()}


async def main_async(args: argparse.Namespace) -> int:
    root = Path(tempfile.mkdtemp(prefix="genomics-mcp-demos-"))
    args.out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "server": server_identity(args.command),
        "host": {"platform": platform.platform(), "machine": platform.machine()},
        "install": json.loads(args.install_report.read_text()) if args.install_report else None,
        "demos": {},
    }
    try:
        for name in args.only or list(DEMOS):
            print(f"== {name}", file=sys.stderr)
            result = await run_one(name, args.command, root)
            (args.out / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
            summary["demos"][name] = {k: result[k] for k in ("status", "started_at", "finished_at")}
            if result["status"] != "passed":
                summary["demos"][name]["failure"] = result["failure"]
            print(f"   {result['status']} {result.get('failure', '')}", file=sys.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    (args.out / "summary.json").write_text(
        json.dumps(scrub(summary, {str(root): "$RUN"}), indent=2) + "\n"
    )
    print(json.dumps(summary["demos"], indent=2))
    return 0 if all(d["status"] == "passed" for d in summary["demos"].values()) else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--only", action="append", choices=list(DEMOS))
    p.add_argument("--install-report", type=Path, help="check_dist.py --output JSON")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        p.error("give the installed server command after --")
    return anyio.run(main_async, args)


if __name__ == "__main__":
    sys.exit(main())
