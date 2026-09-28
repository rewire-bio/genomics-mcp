"""Stand-in for prefetch and fasterq-dump in offline tests (installed under both names).

Behaviour comes from control.json next to the executables, because the server gives toolkit
children a scrubbed environment. Messages, exit codes and output layout copy SRA Toolkit 3.4.1
as observed; every call is appended to calls.jsonl for assertions.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(sys.argv[0]).parent
TOOL = Path(sys.argv[0]).name
control = json.loads((HERE / "control.json").read_text())
args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


settings = os.environ.get("NCBI_SETTINGS")
with (HERE / "calls.jsonl").open("a") as fh:
    fh.write(
        json.dumps(
            {
                "tool": TOOL,
                "args": args,
                "env": {k: v for k, v in os.environ.items()},
                "settings": Path(settings).read_text() if settings else None,
                "pid": os.getpid(),
            }
        )
        + "\n"
    )

if args == ["--version"]:
    print(f"{sys.argv[0]} : {control.get('version', '3.4.1')}\n")
    sys.exit(0)


def hang():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (HERE / "grandchild.pid").write_text(str(child.pid))
    time.sleep(120)


def prefetch():
    mode = control.get("prefetch", "ok")
    acc = args[0]
    run_dir = Path(opt("--output-directory")) / acc
    size = control.get("run_bytes", 4000)
    if mode == "404":
        print(
            f"prefetch.3.4.1 err: name not found while resolving query within virtual file "
            f"system module - failed to resolve accession '{acc}' - no data ( 404 )",
            file=sys.stderr,
        )
        sys.exit(3)
    if mode == "403":
        print(f"prefetch.3.4.1 err: access denied while resolving '{acc}' ( 403 )")
        sys.exit(3)
    if mode == "too_large":
        print(f"1) '{acc}' (4 KB) is larger than maximum allowed: skipped")
        sys.exit(0)
    if mode == "hang":
        hang()
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp = run_dir / f"{acc}.sra.tmp"
    if mode == "grow":
        with tmp.open("ab") as fh:
            while True:
                fh.write(b"x" * 65536)
                fh.flush()
                time.sleep(0.01)
    have = tmp.stat().st_size if tmp.exists() else 0
    if have:
        (HERE / "resumed.txt").write_text(str(have))
    if mode == "interrupt":
        with tmp.open("ab") as fh:
            fh.write(b"s" * (size // 2 - have))
        print("prefetch.3.4.1 err: connection reset by peer while reading file", file=sys.stderr)
        sys.exit(3)
    if not (run_dir / f"{acc}.sra").exists():
        with tmp.open("ab") as fh:
            fh.write(b"s" * (size - have))
        tmp.rename(run_dir / f"{acc}.sra")
    for dep in control.get("deps", []):
        (run_dir / dep).write_bytes(b"r" * 100)
    print(f"1) '{acc}' was downloaded successfully")


def record(name, n, seq="ACGT"):
    return (
        f"@{name} {n} length={len(seq)}\n{seq}\n+{name} {n} length={len(seq)}\n{'I' * len(seq)}\n"
    )


def fasterq_dump():
    mode = control.get("fasterq", "paired")
    run_dir = Path(args[0])
    acc = run_dir.name
    outdir = Path(opt("--outdir"))
    if not (run_dir / f"{acc}.sra").is_file():
        print(f"fasterq-dump.3.4.1 err: cannot find '{run_dir}'", file=sys.stderr)
        sys.exit(3)
    pairs, single = control.get("pairs", 3), control.get("unpaired", 0)
    if "--size-check" in args:
        if mode != "no_estimate":
            print(f"est. output          : {control.get('estimate', 2000):,} bytes")
            print("... uses 'SEQUENCE' as sequence-table")
            print(f"SEQ.spot_count = {control.get('spots', pairs + single):,}")
            print(f"SEQ.bio_base_count = {control.get('bio_bases', 8 * pairs + 3 * single):,}")
        sys.exit(0)
    if mode == "hang":
        hang()
    if mode == "fail":
        print("fasterq-dump.3.4.1 err: the input data is missing the QUALITY-column")
        sys.exit(3)
    if mode == "disk_limit":
        print("disk-limit exeeded!\nfasterq-dump quit with error code 3")
        sys.exit(3)
    scratch = Path(opt("--temp")) / "fasterq.tmp.host.1"
    scratch.mkdir(parents=True)
    (scratch / "chunk").write_bytes(b"t" * control.get("scratch_bytes", 100))
    if pairs:
        with (
            (outdir / f"{acc}_1.fastq").open("w") as r1,
            (outdir / f"{acc}_2.fastq").open("w") as r2,
        ):
            for i in range(1, pairs + 1):
                r1.write(record(f"{acc}.{i}", i))
                other = f"{acc}.{i + 1}" if mode == "mismatch" and i == pairs else f"{acc}.{i}"
                r2.write(record(other, i, "TTGA"))
    if single:
        with (outdir / f"{acc}.fastq").open("w") as fh:
            for i in range(pairs + 1, pairs + single + 1):
                fh.write(record(f"{acc}.{i}", i, "GGC"))
    if mode == "truncated":
        with (outdir / f"{acc}_2.fastq").open("a") as fh:
            fh.write(f"@{acc}.99 99 length=4\nACGT\n")
    if mode == "extra_file":
        (outdir / f"{acc}_3.fastq").write_text("")
    for f in scratch.iterdir():
        f.unlink()
    scratch.rmdir()
    written = 2 * pairs + single + (1 if mode == "miscount" else 0)
    print(f"spots read      : {pairs + single:,}", file=sys.stderr)
    if mode != "no_report":
        print(f"reads written   : {written:,}", file=sys.stderr)


{"prefetch": prefetch, "fasterq-dump": fasterq_dump}[TOOL]()
