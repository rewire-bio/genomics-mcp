# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["mcp==2.2.0"]
# ///
"""Real public SRA runs to FASTQ over MCP stdio with the optional SRA Toolkit (convert_sra_run).

    uv run --script scripts/sra_demo.py --out demos/results/$(date -u +%F)/sra-native.json \\
        --work-dir /tmp/sra-work --env GENOMICS_MCP_SRA_TOOLKIT_DIR=/path/to/sratoolkit/bin \\
        -- genomics-mcp
    uv run --script scripts/sra_demo.py --out sra-container.json --work-dir "$WORK" \\
        --server-work-dir /work --image-ref IMAGE@DIGEST -- \\
        docker run --rm -i --mount type=bind,source="$WORK",target=/work IMAGE

Checks, all through MCP tools and resources:
  status      genomics://status reports the toolkit and its version
  invalid     a non-run accession is refused before the toolkit runs
  budget      a 4 KiB budget fails with budget_exceeded and leaves no FASTQ
  paired      SRR13450355 (744 spots, Illumina paired) converts and verifies
  single      SRR24157174 (1,080 spots, single-end with a technical read) converts and verifies
  interrupt   SRR10063098 (~38 MB): the server is stopped mid-download; a new server resumes it
  cancel      SRR10063100 (~37 MB): cancelled mid-job; its files are removed

Every FASTQ artifact is re-read here, independently of the server: record structure, spot
numbering, mate pairing, sha256/md5 against the reported checksums, and read and base counts
against ENA's and NCBI's own run metadata. File ownership (uid) is recorded. The harness never
imports genomics_mcp. A source failure is recorded as a failed check, never as success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from _mcpclient import call, connect, parse_env, scrub, server_env

PAIRED, SINGLE = "SRR13450355", "SRR24157174"
INTERRUPT, CANCEL = "SRR10063098", "SRR10063100"
SMALL_BUDGET = 16 * 1024 * 1024
MID_BUDGET = 400 * 1024 * 1024


class Failed(Exception):
    pass


def check(cond: bool, what: str) -> None:
    if not cond:
        raise Failed(what)


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed public https URLs
        return resp.read().decode()


def archive_counts(acc: str) -> dict[str, Any]:
    """Spots and bases from ENA's file report and NCBI's EUtils runinfo (public HTTPS)."""
    q = urllib.parse.urlencode(
        {"accession": acc, "result": "read_run", "fields": "read_count,base_count,library_layout"}
    )
    ena = fetch_text(f"https://www.ebi.ac.uk/ena/portal/api/filereport?{q}").splitlines()
    ena_row = dict(zip(ena[0].split("\t"), ena[1].split("\t"), strict=True))
    q = urllib.parse.urlencode({"db": "sra", "id": acc, "rettype": "runinfo", "retmode": "text"})
    rows = fetch_text(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?{q}")
    lines = [line for line in rows.splitlines() if line.strip()]
    header = lines[0].split(",")
    ncbi = next(
        dict(zip(header, r.split(","), strict=False)) for r in lines[1:] if r.startswith(acc)
    )
    return {
        "ena": {k: ena_row[k] for k in ("read_count", "base_count", "library_layout")},
        "ncbi_runinfo": {k: ncbi[k] for k in ("spots", "bases", "LibraryLayout", "size_MB")},
    }


def read_fastq(path: Path, acc: str) -> tuple[list[int], int, dict[str, str]]:
    """Spot numbers, base count and checksums of one FASTQ file (independent parser)."""
    spots, bases = [], 0
    md5, sha = hashlib.md5(), hashlib.sha256()  # noqa: S324 - file checksum
    with path.open("rb") as fh:
        while head := fh.readline():
            seq, plus, qual = fh.readline(), fh.readline(), fh.readline()
            for line in (head, seq, plus, qual):
                md5.update(line)
                sha.update(line)
            check(
                head[:1] == b"@" and plus[:1] == b"+" and qual.endswith(b"\n"),
                f"{path.name}: record",
            )
            check(len(seq.rstrip()) == len(qual.rstrip()), f"{path.name}: seq/qual length")
            name = head[1:].split()[0].decode()
            check(name.startswith(acc + "."), f"{path.name}: read name {name}")
            spots.append(int(name.split(".")[1]))
            bases += len(seq.rstrip())
    return spots, bases, {"md5": md5.hexdigest(), "sha256": sha.hexdigest()}


def independent_check(data: dict, acc: str, host_path) -> dict[str, Any]:
    arts = {Path(a["path"]).name: a for a in data["artifacts"]}
    files: dict[str, Any] = {}
    all_spots: list[int] = []
    bases = 0
    for name, art in sorted(arts.items()):
        path = host_path(art["path"])
        spots, n_bases, sums = read_fastq(path, acc)
        reported = {c["algorithm"]: c["value"] for c in art["checksums"]}
        check(
            reported["sha256"] == sums["sha256"] and reported["md5"] == sums["md5"],
            f"{name} checksum",
        )
        files[name] = {"reads": len(spots), "bases": n_bases, "uid": path.stat().st_uid, **sums}
        bases += n_bases
        if not name.endswith(("_2.fastq",)):
            all_spots += spots
    mates = [n for n in arts if n.endswith(("_1.fastq", "_2.fastq"))]
    if mates:
        one, two = (read_fastq(host_path(arts[f"{acc}_{i}.fastq"]["path"]), acc)[0] for i in (1, 2))
        check(one == two, "mate files list the same spots in the same order")
    check(len(all_spots) == len(set(all_spots)), "no spot repeated across files")
    archive = archive_counts(acc)
    ena_spots, ena_bases = int(archive["ena"]["read_count"]), int(archive["ena"]["base_count"])
    check(
        len(set(all_spots)) == ena_spots,
        f"spots {len(set(all_spots))} == ENA read_count {ena_spots}",
    )
    check(bases == ena_bases, f"bases {bases} == ENA base_count {ena_bases}")
    check(int(archive["ncbi_runinfo"]["spots"]) == ena_spots, "NCBI runinfo spots == ENA")
    check(int(archive["ncbi_runinfo"]["bases"]) == ena_bases, "NCBI runinfo bases == ENA")
    return {"files": files, "spots": len(set(all_spots)), "bases": bases, "archive": archive}


async def wait_final(client, tid: str, seconds: float = 900) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        res = await call(client, "get_transfer_status", {"transfer_id": tid})
        if res["data"]["transfer"]["state"] in ("completed", "failed", "cancelled"):
            return res
        await anyio.sleep(1)
    raise Failed(f"{tid} did not finish in {seconds}s")


async def wait_downloading(client, tid: str, seconds: float = 120) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        res = await call(client, "get_transfer_status", {"transfer_id": tid})
        state = res["data"]["transfer"]["state"]
        if state != "running" and state != "queued":
            raise Failed(f"{tid} ended ({state}) before it could be interrupted")
        if res["data"]["transfer"]["bytes_done"] > 1_000_000:
            return res
        await anyio.sleep(0.2)
    raise Failed(f"{tid} did not start downloading")


def summary(res: dict) -> dict:
    data = res["data"]
    prep = data.get("preparation") or {}
    return {
        "state": data["transfer"]["state"],
        "transfer_id": data["transfer"]["transfer_id"],
        "error": data["transfer"].get("error"),
        "notes": data.get("notes"),
        "toolkit": prep.get("toolkit"),
        "run_file": prep.get("run_file"),
        "dependencies": prep.get("dependencies"),
        "sequence_table": prep.get("sequence_table"),
        "spot_count": prep.get("spot_count"),
        "conversion": prep.get("conversion"),
        "estimates": prep.get("estimates"),
        "verification": prep.get("verification"),
        "resources": prep.get("resources"),
        "fastq": [
            {k: f[k] for k in ("role", "reads", "bases")} | {"readiness": f["file"]["readiness"]}
            for f in prep.get("fastq", [])
        ],
        "artifacts": [
            {"path": a["path"], "size_bytes": a["size_bytes"], "checksums": a["checksums"]}
            for a in data.get("artifacts", [])
        ],
        "provenance": res.get("provenance"),
    }


async def run(args: argparse.Namespace) -> dict:
    env = server_env(parse_env(args.env))
    work = Path(args.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    server_work = args.server_work_dir or str(work)
    if not args.server_work_dir:
        env["GENOMICS_MCP_WORK_DIR"] = str(work)

    def host_path(p: str) -> Path:
        return work / Path(p).relative_to(server_work)

    errlog = work.parent / f"{work.name}-server.log"
    checks: dict[str, Any] = {}

    async def step(name: str, fn) -> None:
        started = time.monotonic()
        try:
            checks[name] = {"ok": True, **(await fn())}
        except Exception as exc:  # noqa: BLE001 - recorded, never hidden
            checks[name] = {"ok": False, "failure": f"{type(exc).__name__}: {exc}"}
        checks[name]["seconds"] = round(time.monotonic() - started, 1)

    async with connect(args.command, env, errlog) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        status = json.loads((await client.read_resource("genomics://status")).contents[0].text)

        async def s_status():
            sra = status["sra_toolkit"]
            check("convert_sra_run" in tools, "convert_sra_run listed")
            check(sra["available"], f"toolkit available: {sra['detail']}")
            return {"sra_toolkit": sra, "server_version": status["server_version"]}

        async def s_invalid():
            res = await call(client, "convert_sra_run", {"accession": "SRP000001"})
            check(res["error"]["code"] == "invalid_input", "invalid accession refused")
            return {"code": res["error"]["code"]}

        async def s_budget():
            res = await call(client, "convert_sra_run", {"accession": PAIRED, "budget_bytes": 4096})
            final = await wait_final(client, res["data"]["transfer"]["transfer_id"])
            err = final["data"]["transfer"]["error"] or {}
            check(err.get("code") == "budget_exceeded", f"budget_exceeded, got {err}")
            check(final["data"]["artifacts"] == [], "no artifacts")
            return {"error": {k: err.get(k) for k in ("code", "message")}}

        async def convert(acc: str, budget: int) -> dict:
            res = await call(client, "convert_sra_run", {"accession": acc, "budget_bytes": budget})
            check(res["status"] == "ok", f"started: {res.get('error')}")
            final = await wait_final(client, res["data"]["transfer"]["transfer_id"])
            check(
                final["data"]["transfer"]["state"] == "completed",
                f"completed: {summary(final)['error']}",
            )
            return {
                "result": summary(final),
                "independent": independent_check(final["data"], acc, host_path),
            }

        await step("status", s_status)
        await step("invalid", s_invalid)
        await step("budget", s_budget)
        await step("paired", lambda: convert(PAIRED, SMALL_BUDGET))
        await step("single", lambda: convert(SINGLE, SMALL_BUDGET))

        async def s_cancel():
            res = await call(
                client, "convert_sra_run", {"accession": CANCEL, "budget_bytes": MID_BUDGET}
            )
            tid = res["data"]["transfer"]["transfer_id"]
            await wait_downloading(client, tid)
            cancelled = await call(client, "cancel_transfer", {"transfer_id": tid})
            check(cancelled["data"]["transfer"]["state"] == "cancelled", "cancelled")
            await anyio.sleep(1)
            left = list((work / "transfers" / tid).rglob("*"))
            check([p for p in left if p.name != "job.json"] == [], f"job files removed: {left}")
            check(not (work / "artifacts" / tid).exists(), "no artifacts")
            return {"transfer_id": tid, "remaining": [p.name for p in left]}

        if not args.skip_mid_size:
            await step("cancel", s_cancel)
            res = await call(
                client, "convert_sra_run", {"accession": INTERRUPT, "budget_bytes": MID_BUDGET}
            )
            interrupted = res["data"]["transfer"]["transfer_id"]
            try:
                before = await wait_downloading(client, interrupted)
                checks["interrupt"] = {
                    "bytes_done_when_stopped": before["data"]["transfer"]["bytes_done"]
                }
            except Exception as exc:  # noqa: BLE001 - recorded, never hidden
                checks["interrupt"] = {"ok": False, "failure": f"{type(exc).__name__}: {exc}"}
    # The server has exited here; its shutdown marks the interrupted job failed and resumable.

    if not args.skip_mid_size and "failure" not in checks.get("interrupt", {}):
        async with connect(args.command, env, errlog.with_name(errlog.stem + "-2.log")) as client:

            async def s_resume():
                stopped = await call(client, "get_transfer_status", {"transfer_id": interrupted})
                t = stopped["data"]["transfer"]
                check(t["state"] == "failed" and t["resumable"], f"resumable after restart: {t}")
                partial = sorted(p.name for p in (work / "transfers" / interrupted).rglob("*.tmp"))
                res = await call(
                    client, "convert_sra_run", {"accession": INTERRUPT, "budget_bytes": MID_BUDGET}
                )
                check(res["data"]["transfer"]["transfer_id"] == interrupted, "same job resumed")
                final = await wait_final(client, interrupted)
                check(
                    final["data"]["transfer"]["state"] == "completed", f"{summary(final)['error']}"
                )
                return {
                    "after_restart": {
                        "state": t["state"],
                        "resumable": t["resumable"],
                        "error": t["error"],
                    },
                    "partial_files_before_resume": partial,
                    "result": summary(final),
                    "independent": independent_check(final["data"], INTERRUPT, host_path),
                }

            await step("interrupt_resume", s_resume)

    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": f"{platform.system()} {platform.machine()}",
        "command": args.command[:3] + (["..."] if len(args.command) > 3 else []),
        "image": args.image_ref,
        "harness_uid": os.getuid(),
        "child_rusage": {
            "max_rss": usage.ru_maxrss,
            "max_rss_unit": "bytes" if sys.platform == "darwin" else "KiB",
            "cpu_s": round(usage.ru_utime + usage.ru_stime, 1),
            "note": "server and toolkit processes it waited for (for docker run: the CLI only)",
        },
        "checks": checks,
        "passed": all(c.get("ok", False) for k, c in checks.items() if k != "interrupt"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", required=True)
    p.add_argument("--work-dir", required=True, help="host path of the server work dir")
    p.add_argument("--server-work-dir", help="the same directory as the server sees it (container)")
    p.add_argument("--env", action="append", default=[], help="KEY=VALUE for the server")
    p.add_argument("--image-ref", help="container image reference with digest, for the record")
    p.add_argument("--skip-mid-size", action="store_true", help="skip interrupt and cancel runs")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        p.error("give the server command after --")
    report = anyio.run(run, args)
    work = str(Path(args.work_dir).resolve())
    report = scrub(
        report, {work: "$WORK", **({args.server_work_dir: "$WORK"} if args.server_work_dir else {})}
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v.get("ok") for k, v in report["checks"].items()}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
