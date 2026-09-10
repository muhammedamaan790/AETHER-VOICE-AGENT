"""Start the telephony worker and write a fresh, self-naming log every time.

    python scripts/run_call.py              # same as `python -m aether.telephony.agent dev`
    python scripts/run_call.py start        # any LiveKit CLI arguments are forwarded

WHY THIS EXISTS. The worker was being run as

    cmd /c "python -m aether.telephony.agent dev 2>&1" | Tee-Object -FilePath call8.log

which means remembering to bump the number by hand before every call. Forget once and the new call
overwrites the old one, or worse, appends to it -- and a log holding two calls is not evidence of
either. Naming the file after the clock removes the decision: every run gets its own log and no run
can ever overwrite another.

IT ALSO PAIRS THE LOG TO ITS TRACES. A worker log and a trace are two halves of the same evidence:
the trace proves what the pipeline did, the worker log proves it came over a telephone. Last time
that pairing had to be argued by matching sixteen latencies across two files, because nothing
connected them. This writes a footer naming every trace the session produced, so the pair is stated
in the file rather than reconstructed later.

Everything the worker prints still appears on your screen; it is written to the log as well, not
instead. `logs/` is gitignored (`*.log`), so nothing here is committed by accident -- a log you
want as evidence goes to `evidence/` deliberately, after a secrets audit.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _log_path(log_dir: Path) -> Path:
    """`logs/worker-20260910T142530Z.log`, never colliding.

    The stamp format matches `Trace.new_run`, so a log and the traces beside it sort together and
    read as the same run. The `-2` suffix is for the case two workers start inside one second --
    unlikely, and silently overwriting a log would be a bad way to find out it happened.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = log_dir / f"worker-{stamp}.log"
    n = 2
    while path.exists():
        path = log_dir / f"worker-{stamp}-{n}.log"
        n += 1
    return path


def _traces_since(trace_dir: Path, started: float) -> list[Path]:
    """Trace files written after this worker started -- one per call it took."""
    if not trace_dir.is_dir():
        return []
    return sorted(
        (p for p in trace_dir.glob("run-*.jsonl") if p.stat().st_mtime >= started),
        key=lambda p: p.stat().st_mtime,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("livekit_args", nargs="*", default=None,
                    help="forwarded to the LiveKit CLI; defaults to 'dev'")
    ap.add_argument("--log-dir", default=os.environ.get("AETHER_LOG_DIR", "logs"))
    ap.add_argument("--trace-dir", default=os.environ.get("AETHER_TRACE_DIR", "traces"))
    args = ap.parse_args()

    forwarded = args.livekit_args or ["dev"]
    log_dir = (ROOT / args.log_dir) if not Path(args.log_dir).is_absolute() else Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = _log_path(log_dir)
    trace_dir = (ROOT / args.trace_dir) if not Path(args.trace_dir).is_absolute() \
        else Path(args.trace_dir)

    started_wall = datetime.now(timezone.utc)
    started = time.time()

    command = [sys.executable, "-m", "aether.telephony.agent", *forwarded]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

    header = [
        "# AETHER telephony worker",
        f"# started : {started_wall.isoformat()}",
        f"# command : {' '.join(forwarded)}",
        f"# log     : {log}",
        "",
    ]
    print(f"log        : {log}")
    print("(this file is new for this run; nothing to rename, nothing to overwrite)\n")

    code = 1
    # `newline=""` so the child's own line endings survive verbatim: a log is a record, and
    # rewriting bytes in it -- even line endings -- makes it a worse one.
    with open(log, "w", encoding="utf-8", errors="replace", newline="") as fh:
        fh.write("\n".join(header))
        fh.flush()
        try:
            process = subprocess.Popen(
                command, cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            fh.write(f"# FAILED TO START: {exc}\n")
            raise SystemExit(f"could not start the worker: {exc}") from exc

        try:
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                fh.write(line)
                fh.flush()          # flushed per line: a crash must not cost the last thing said
            code = process.wait()
        except KeyboardInterrupt:
            # Ctrl+C reached the child too. Let it shut down on its own -- killing it here would
            # cut the trace off mid-write and lose the "[7/7] call ended" line that names it.
            fh.write("\n# interrupted from the keyboard; waiting for the worker to stop\n")
            print("\nstopping the worker...")
            try:
                code = process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait()
                fh.write("# the worker did not stop in 20s and was killed\n")

        produced = _traces_since(trace_dir, started)
        footer = [
            "",
            "# ---------------------------------------------------------------------------",
            f"# ended    : {datetime.now(timezone.utc).isoformat()}",
            f"# exit     : {code}",
            f"# traces   : {len(produced)} produced by this worker session",
        ]
        footer += [f"#            {p}" for p in produced] or ["#            (none -- no call was taken)"]
        footer += [
            "# Each trace carries input_path=telephony on its first event, so it says for itself",
            "# that its audio arrived over a phone line rather than a laptop microphone.",
            "",
        ]
        fh.write("\n".join(footer))

    print(f"\nlog written: {log}")
    for path in produced:
        print(f"  trace     : {path}")
    if not produced:
        print("  trace     : none -- no call was taken")
    sys.exit(code)


if __name__ == "__main__":
    main()
