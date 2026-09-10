"""The worker launcher must never let one call's log overwrite another's.

`scripts/run_call.py` exists because the worker was being run through a hand-numbered
`Tee-Object -FilePath call8.log`. The failure mode is quiet and total: forget to bump the number and
the previous call's log is gone, or two calls end up in one file, and a file holding two calls is
evidence of neither.

These tests pin the two properties that make the replacement trustworthy -- a name that cannot
collide, and a trace pairing that reflects only this session -- plus the one that keeps the log
readable as evidence: its stamp sorts alongside the traces it belongs to.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aether.trace import Trace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_call.py"


@pytest.fixture(scope="module")
def launcher():
    """Import the script as a module. Importing it must have no side effect of its own."""
    spec = importlib.util.spec_from_file_location("run_call", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_fresh_run_never_reuses_an_existing_log_name(launcher, tmp_path: Path) -> None:
    """The whole point. Two starts inside the same second must still get two files."""
    first = launcher._log_path(tmp_path)
    first.write_text("call one", encoding="utf-8")

    second = launcher._log_path(tmp_path)
    assert second != first
    assert not second.exists()

    second.write_text("call two", encoding="utf-8")
    third = launcher._log_path(tmp_path)
    assert third not in (first, second)

    # And the earlier call is still there, unchanged. This is the assertion the old workflow failed.
    assert first.read_text(encoding="utf-8") == "call one"


def test_the_suffix_counts_up_rather_than_wrapping_round(launcher, tmp_path: Path) -> None:
    names = []
    for _ in range(4):
        path = launcher._log_path(tmp_path)
        path.write_text("x", encoding="utf-8")
        names.append(path.name)
    assert len(set(names)) == 4


def test_the_log_stamp_matches_the_trace_stamp_format(launcher, tmp_path: Path) -> None:
    """So a log and the traces from the same call sort together and read as one run.

    Compared against `Trace.new_run`'s real output rather than against a copy of its format string,
    which would keep passing if the trace naming changed.
    """
    trace = Trace.new_run(tmp_path, echo=False)
    trace.close()
    trace_stamp = re.fullmatch(r"run-(.+)\.jsonl", trace.path.name).group(1)
    log_stamp = re.fullmatch(r"worker-(.+)\.log", launcher._log_path(tmp_path).name).group(1)

    pattern = r"\d{8}T\d{6}Z"
    assert re.fullmatch(pattern, trace_stamp), trace_stamp
    assert re.fullmatch(pattern, log_stamp), log_stamp
    # Same second, or one apart if the clock ticked between the two calls.
    fmt = "%Y%m%dT%H%M%SZ"
    delta = abs((datetime.strptime(log_stamp, fmt) - datetime.strptime(trace_stamp, fmt)).total_seconds())
    assert delta <= 2


def test_only_traces_from_this_session_are_paired_to_the_log(launcher, tmp_path: Path) -> None:
    """A footer that claimed an older call's trace would be worse than no footer at all."""
    import os
    import time

    older = tmp_path / "run-20200101T000000Z.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    old_time = time.time() - 3600
    os.utime(older, (old_time, old_time))

    started = time.time()
    time.sleep(0.01)
    mine = tmp_path / "run-20260910T000000Z.jsonl"
    mine.write_text("{}\n", encoding="utf-8")

    paired = launcher._traces_since(tmp_path, started)
    assert paired == [mine], "the footer would have claimed a trace from another call"


def test_a_missing_trace_directory_is_not_an_error(launcher, tmp_path: Path) -> None:
    """A worker that took no call still has to finish writing its log."""
    assert launcher._traces_since(tmp_path / "nope", 0.0) == []


def test_the_launcher_starts_the_real_agent_module(launcher) -> None:
    """Read as text, deliberately: the command is built inside `main()`, which cannot be called
    here without starting a worker. A wrapper that launched something else would still pass every
    other test in this file."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"-m", "aether.telephony.agent"' in source
    assert "stderr=subprocess.STDOUT" in source, "stderr must reach the log, as 2>&1 did"


def test_logs_are_gitignored_so_none_is_committed_by_accident() -> None:
    """`logs/` fills up with every call. Evidence goes to `evidence/` deliberately, never by
    forgetting to ignore something."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.log" in ignore
    # And the one deliberate exception is still below it, or it would do nothing.
    assert ignore.index("!evidence/*.log") > ignore.index("*.log")
