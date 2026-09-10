"""The documents must not contradict the system, or each other.

This exists because they twice did. `DEMO.md` scripted a price the database contradicted. Three
separate files quoted three different test counts (653, 734, 883) while the suite ran 889. And the
claim "no successful phone call has been completed" survived in `README.md`, `MEMORY.md` and
`PHASES.md` after it had been corrected in `RIME_EVIDENCE.md` and `DEMO.md` — so a judge opening the
README first would have read that the phone path does not work.

Every one of those was written by somebody who believed it at the time. Prose has no compiler, so
the only thing that keeps it true is a test that reads it.

`tests/test_demo_script.py` does this for the demo script. This does it for the claims that appear in
several files at once and drift apart.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "DEMO.md", "MEMORY.md", "PHASES.md", "RIME_EVIDENCE.md", "JUDGING.md",
        "PRD.md", "DEMO_SCRIPT.md", "evidence/README.md"]


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def collected() -> int:
    """How many tests actually exist, asked of pytest rather than assumed."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    assert match, f"could not read a collection count from pytest:\n{proc.stdout[-500:]}"
    return int(match.group(1))


def test_every_quoted_test_count_matches_the_suite(collected):
    """A stale count is small, but it is the first thing a reader can check and find wrong.

    Claims are written as "N tests pass" or "expect N passed", and the suite reports passed and
    skipped separately -- so a document may quote either the number that pass or the number that
    exist, and this accepts both rather than forcing one house style.
    """
    passing = collected - 2      # the two deliberate, documented skips
    wrong: list[str] = []
    for name in DOCS:
        for pattern in (r"(\d{3,}) tests? pass", r"expect (\d{3,}) passed",
                        r"\*\*(\d{3,}) (?:automated )?tests?", r"(\d{3,}) passed, \d+ skipped"):
            for found in re.findall(pattern, _text(name)):
                if int(found) not in (collected, passing):
                    wrong.append(f"{name}: says {found}, suite has {passing} passing "
                                 f"of {collected} collected")
    assert not wrong, "stale test counts:\n  " + "\n  ".join(wrong)


def test_no_document_still_says_the_phone_path_is_unverified():
    """This claim was corrected in two files and left standing in three.

    It is checked as a phrase rather than a sentiment because that is what can be checked: these are
    the exact forms the claim took. The point is not that the phone path is perfect -- Part 6 is
    explicit that STT word accuracy on narrowband audio is unmeasured -- but that no document may
    state the flat negative that a call has never succeeded.
    """
    dead = [
        "no successful phone call has been completed",
        "no successful phone call has been made",
        "no successful call has been completed",
        "the real phone path has never been validated",
        # Phrasings that say the same thing without the words above. These three survived the first
        # correction pass precisely because a search for "never been validated" did not find them.
        "it has not been validated",
        "that a real call has been measured. it has not",
        "telephony is unvalidated",
        "the call itself is unverified",
    ]
    found: list[str] = []
    for name in DOCS:
        low = _text(name).lower()
        for claim in dead:
            if claim in low:
                found.append(f"{name}: {claim!r}")
    assert not found, ("these calls have since succeeded; the claim is stale:\n  "
                       + "\n  ".join(found))


def test_no_document_describes_the_menu_as_a_python_fixture():
    """`data/aether_hotel.db` replaced the fixture. Docs that still say "fixture" send a reader
    looking for a file that no longer exists, and understate the read-only guarantee."""
    # Word-boundaried. A bare substring match reported the correct "19 read-only tools" as the
    # stale "9 read-only tools" -- the guard crying wolf about a document that was right.
    stale = [r"hotel menu fixture", r"menu fixture,", r"29 dishes",
             r"\b8 read-only tools", r"\b9 read-only tools"]
    found = [f"{name}: {pattern!r}"
             for name in DOCS for pattern in stale
             if re.search(pattern, _text(name).lower())]
    assert not found, "the menu is a SQLite database now:\n  " + "\n  ".join(found)


def test_no_document_references_a_tool_that_no_longer_exists():
    """`spice_of` and `find_by_spice` were removed when the database turned out to record no spice
    level. MEMORY.md may still discuss them -- it is a history, and the entry that does is explicitly
    marked superseded -- so only the reader-facing documents are checked."""
    from aether.hotel.tools import HOTEL_TOOLS

    reader_facing = ["README.md", "DEMO.md", "DEMO_SCRIPT.md", "JUDGING.md", "PHASES.md"]
    found: list[str] = []
    for name in reader_facing:
        text = _text(name)
        for gone in ("spice_of", "find_by_spice"):
            assert gone not in HOTEL_TOOLS, f"{gone} is back; update this test"
            if re.search(rf"`{gone}`", text):
                found.append(f"{name}: {gone}")
    assert not found, "these tools were removed:\n  " + "\n  ".join(found)


def test_every_relative_link_in_the_reader_facing_docs_resolves():
    """A broken link in a document a judge is invited to follow is a small thing that reads badly."""
    broken: list[str] = []
    for name in ["README.md", "JUDGING.md", "DEMO.md", "DEMO_SCRIPT.md", "evidence/README.md"]:
        base = (ROOT / name).parent
        for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", _text(name)):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (base / target.split("#")[0]).resolve()
            if not path.exists():
                broken.append(f"{name}: [{label}]({target})")
    assert not broken, "broken links:\n  " + "\n  ".join(broken)


# --- the committed evidence must corroborate itself ------------------------------------------

def test_the_committed_trace_and_worker_log_describe_the_same_call():
    """This correspondence is the ONLY thing establishing that the committed run came over a phone.

    A trace records latency and fencing but not its input path, and the event vocabulary is
    identical for a telephone and a laptop microphone. The worker log is telephony-specific. If the
    two files ever stop matching turn for turn, the claim "this is a real phone call" loses its
    basis and must be withdrawn -- so it is asserted rather than remembered.
    """
    import json

    trace = ROOT / "evidence" / "demo-run.jsonl"
    log = ROOT / "evidence" / "demo-call-worker.log"
    assert trace.exists() and log.exists()

    spoken = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    from_trace = [
        (round(e["stt_ms"]), round(e["llm_ms"]), round(e["tts_ms"]), round(e["turn_latency_ms"]))
        for e in spoken if e.get("type") == "ResponseSpoken"
    ]
    from_log = [
        tuple(int(g) for g in m.groups())
        for m in re.finditer(r"latency: stt=(\d+) llm=(\d+) tts=(\d+) -> turn=(\d+) ms",
                             log.read_text(encoding="utf-8"))
    ]
    assert from_log, "the worker log has no latency lines"
    assert from_trace == from_log, "the trace and the worker log are not the same call"


def test_the_committed_worker_log_carries_no_caller_number_or_secret():
    """It is a real call log. The caller's number is personal data and is redacted; a secret would
    be worse. Checked here rather than trusted, because this file IS published."""
    log = (ROOT / "evidence" / "demo-call-worker.log").read_text(encoding="utf-8")
    assert not re.search(r"\+\d{10,15}", log), "an unredacted phone number is in the evidence"
    assert "<redacted-caller-number>" in log, "the redaction marker should show what was removed"
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
            if m and re.search(r"KEY|SECRET|TOKEN|PASSWORD", m.group(1), re.I):
                value = m.group(2).strip().strip('"').strip("'")
                if len(value) >= 8:
                    assert value not in log, f"{m.group(1)} leaked into committed evidence"


def test_the_worker_log_evidences_the_telephony_fixes_it_is_cited_for():
    """RIME_EVIDENCE Part 6 cites this file for four specific claims. If the lines are not in it,
    the citation is decoration."""
    log = (ROOT / "evidence" / "demo-call-worker.log").read_text(encoding="utf-8")
    for needle in ("aether-hotel", "inbound pump starting", "pumps=1", "frames_failed=0",
                   "observed_rate=", "inbound audio captured"):
        assert needle in log, f"the log does not actually show {needle!r}"
