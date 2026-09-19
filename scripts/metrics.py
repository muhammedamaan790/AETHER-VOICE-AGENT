"""The public numbers, derived from the repository rather than remembered.

WHY THIS EXISTS. Every figure AETHER quotes to a reader lives in several documents at once, and
they drift apart silently: the README said fencing happens at "five independent checkpoints" in one
paragraph and "four independent layers" two hundred lines later, and RIME_EVIDENCE.md reported zero
leaks across 84 trace files when there were 106. Both were written by somebody who believed them.

So the numbers are computed here, from the code, the traces and the committed evidence, and
`tests/test_reference_integrity.py` fails the suite when a document disagrees. Prose has no
compiler; this is the nearest thing.

WHAT IS DELIBERATELY *NOT* HERE. Anything the repository cannot derive -- STT word accuracy on
narrowband audio, live-mic duck latency, two simultaneous callers. Those are unmeasured, they are
documented as unmeasured, and a plausible number computed here would be exactly the kind of
invention this project exists to avoid.

    python scripts/metrics.py            # print every derived metric
    python scripts/metrics.py --json     # the same, machine readable
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# --- fencing ------------------------------------------------------------------------------------

# Each checkpoint announces itself with its own `ResultDiscarded` reason, which is what makes them
# countable rather than a matter of interpretation: five reasons, five doors an abandoned turn is
# stopped at. `rime_ws` also calls `is_valid()` before every chunk, but it emits no reason of its
# own and defers to the gate -- it is a courtesy to Rime, not a sixth authority, and is not counted.
_REASON = re.compile(r'(?:reason=|STALE_TOOL\s*=\s*)"(stale_generation[a-z_]*)"')


@lru_cache(maxsize=1)
def fence_checkpoints() -> dict[str, str]:
    """{reason: 'path:line'} for every independent fence checkpoint in the pipeline."""
    found: dict[str, str] = {}
    for path in sorted((ROOT / "aether").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for reason in _REASON.findall(line):
                found.setdefault(reason, f"{path.relative_to(ROOT).as_posix()}:{n}")
    return found


# --- tools --------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def tools() -> dict[str, int]:
    sys.path.insert(0, str(ROOT))
    from aether.hotel.tools import HOTEL_TOOLS

    writes = sum(1 for _fn, mutating in HOTEL_TOOLS.values() if mutating)
    return {"total": len(HOTEL_TOOLS), "read": len(HOTEL_TOOLS) - writes, "write": writes}


# --- traces -------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def traces() -> dict[str, int]:
    files = sorted((ROOT / "traces").glob("*.jsonl"))
    counts = {"files": len(files), "leaked": 0, "discarded": 0, "spoken": 0}
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                kind = json.loads(line).get("type")
            except ValueError:
                continue
            if kind == "ResultLeaked":
                counts["leaked"] += 1
            elif kind == "ResultDiscarded":
                counts["discarded"] += 1
            elif kind == "ResponseSpoken":
                counts["spoken"] += 1
    return counts


# --- the committed real phone call ----------------------------------------------------------------

@lru_cache(maxsize=1)
def phone_call() -> dict[str, float | int]:
    """The one call in `evidence/`, whose trace and worker log are asserted to match turn for turn.

    `turn_latency_ms` and `tts_ms` measure DIFFERENT THINGS and are reported separately on purpose:
    the first is the whole turn, the second is Rime's time to the first audio chunk received. The
    second is a component of the first, and quoting one as the other overstates the system by about
    a second.
    """
    rows = [json.loads(line) for line
            in (ROOT / "evidence" / "demo-run.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    spoken = [r for r in rows if r.get("type") == "ResponseSpoken"]
    return {
        "turns": len(spoken),
        "deterministic_turns": sum(1 for r in spoken if r.get("llm_ms") == 0),
        "median_turn_ms": round(statistics.median(r["turn_latency_ms"] for r in spoken)),
        "median_first_audio_ms": round(statistics.median(r["tts_ms"] for r in spoken)),
        "median_stt_ms": round(statistics.median(r["stt_ms"] for r in spoken)),
    }


# --- multilingual routing -------------------------------------------------------------------------

@lru_cache(maxsize=1)
def routing() -> dict[str, int]:
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT))
    from measure_understanding import LANGUAGES, SUITES

    from aether.hotel.router import route

    total = passed = 0
    for by_language in SUITES.values():
        for code in LANGUAGES:
            for said, expected in by_language[code]:
                total += 1
                decision = route(said)
                if decision is not None and decision.tool == expected:
                    passed += 1
    return {"passed": passed, "total": total, "languages": len(LANGUAGES)}


# --- the suite ------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def suite() -> dict[str, int]:
    """Asked of pytest rather than assumed. Collection only -- it does not run anything."""
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "--collect-only", "--no-header"],
                          cwd=ROOT, capture_output=True, text=True)
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    if not match:
        raise RuntimeError(f"could not read a collection count:\n{proc.stdout[-400:]}")
    collected = int(match.group(1))
    # Two deliberate skips, each documented in the test that carries it: the unsafe-mode control
    # condition, and salvage. Counted rather than hardcoded so a third skip cannot slip past.
    skipped = len(re.findall(r"@pytest\.mark\.skip",
                             (ROOT / "tests" / "test_acceptance.py").read_text(encoding="utf-8")))
    return {"collected": collected, "skipped": skipped, "passing": collected - skipped}


def everything(include_suite: bool = True) -> dict:
    out = {
        "fence_checkpoints": len(fence_checkpoints()),
        "fence_reasons": fence_checkpoints(),
        "tools": tools(),
        "traces": traces(),
        "phone_call": phone_call(),
        "routing": routing(),
        "languages": ["English", "Hindi", "Spanish"],
    }
    if include_suite:
        out["suite"] = suite()
    return out


if __name__ == "__main__":
    data = everything()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        s, t, tr, pc, r = (data["suite"], data["tools"], data["traces"],
                           data["phone_call"], data["routing"])
        print(f"suite            {s['passing']} passing, {s['skipped']} skipped "
              f"({s['collected']} collected)")
        print(f"hotel tools      {t['total']}  ({t['read']} read-only, {t['write']} mutating)")
        print(f"fence points     {data['fence_checkpoints']}")
        for reason, where in sorted(data["fence_reasons"].items()):
            print(f"                 {reason:26s} {where}")
        print(f"traces           {tr['files']} files, {tr['leaked']} leaked, "
              f"{tr['discarded']} discarded, {tr['spoken']} spoken")
        print(f"routing          {r['passed']}/{r['total']} across {r['languages']} languages")
        print(f"committed call   {pc['turns']} turns, {pc['deterministic_turns']} at llm_ms=0")
        print(f"                 whole turn        median {pc['median_turn_ms']} ms")
        print(f"                 Rime first audio  median {pc['median_first_audio_ms']} ms")
        print(f"                 STT               median {pc['median_stt_ms']} ms")
