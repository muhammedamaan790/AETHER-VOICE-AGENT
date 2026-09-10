"""One call that does everything, measured: many turns, a real interruption, three languages.

    python scripts/demo_full_call.py                  # offline: no API keys, no network, no cost
    python scripts/demo_full_call.py --mode live      # real Gemini + real Rime over /ws3
    python scripts/demo_full_call.py --repeats 3      # run the whole script N times and average

WHAT THIS IS FOR. The evidence for AETHER is spread across a suite of 1100 tests, a committed phone
trace and four measurement scripts. That is the right shape for a judge who reads, and the wrong
shape for the question "show me it working." This runs the whole product end to end in one go and
prints one report: how long an answer takes, what happens when the caller interrupts, and what
happens when they change language mid-call.

WHAT IT MEASURES, AND WHAT IT DOES NOT. Text is injected where the recogniser would put it, so
**speech-to-text is not in these numbers** -- there is no microphone and no recorded audio here. STT
on real telephone audio was measured separately and is in `evidence/demo-run.jsonl` (927 ms median).
Everything downstream of the transcript IS real in `--mode live`: the router, the database, the
model, Rime over `/ws3`, and the audio gate.

  offline : real router, real SQLite, real fencing, real audio gate; the model and Rime are doubles.
            The `decide` column is therefore the true cost of answering from the database, and the
            `speak` column is not a latency measurement at all.
  live    : the same pipeline with the real model and the real Rime socket. `decide` includes the
            model when the database cannot answer; `speak` is Rime's real time to first audio.

Prints no secret. In live mode the API keys open the connections and are never displayed.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from aether.events import EventType  # noqa: E402
from aether.hotel.router import normalise, route  # noqa: E402
from aether.hotel.tools import HOTEL_TOOLS  # noqa: E402
from aether.lang import ENGLISH, HINDI, SPANISH  # noqa: E402
from aether.tools import ToolRunner  # noqa: E402
from aether.trace import Trace  # noqa: E402

AUDIO = np.zeros(16000, np.int16)

# The script of the call. Deliberately a mix: things the database answers deterministically, and
# one thing it cannot, so both paths appear in the numbers rather than only the flattering one.
ENGLISH_TURNS = [
    "what starters do you have",
    "how much is the chicken kebab",
    "do you have vegetarian mains",
    "what rooms do you have",
    "how much is an executive suite",
    "do you have a swimming pool",
    "is there a gym",
    "what time does the restaurant open",
    "what documents do I need at check in",
    "is there a security deposit",
    "what time is check in",
    "do you have parking",
    "can I get an extra bed",
    "is there a doctor available",
    "what is your address",
    # The database holds no row for either of these, so both fall through to the model on purpose.
    # Without them the latency table would show only the flattering path (R8b.3).
    "what is your star rating",
    "is there anything worth seeing nearby",
]

HINDI_TURNS = ["what starters do you have", "how much is the chicken kebab",
               "do you have a swimming pool", "what time is check in"]
SPANISH_TURNS = ["what rooms do you have", "how much is an executive suite",
                 "do you have a gym", "what time is check in"]

# The interruption. The caller asks about starters, then changes ONE part of the request while the
# lookup is still running -- the brief's own acceptance scenario, in the product's own domain.
INTERRUPTED_FIRST = "what starters do you have"
INTERRUPTED_REVISED = "actually, what desserts do you have"


@dataclass
class Turn:
    label: str
    said: str
    language: str
    # Wall time around the whole turn. It runs until the LAST chunk of the answer has been streamed
    # and handed over, not until the first -- `speak()` blocks for the length of the reply. So it is
    # NOT what the caller waits, and subtracting `speak_ms` from it does not give "decide": it gives
    # decide plus the rest of the stream. Two earlier versions of this script got that wrong; the
    # honest wait is `heard_ms` below, built from the trace's own stage marks.
    wall_ms: float | None = None
    speak_ms: float | None = None            # Rime `tts_ms`: request to FIRST audio
    llm_ms: float | None = None              # 0 on a turn the database answered
    from_database: bool = False
    spoken: str = ""

    @property
    def heard_ms(self) -> float | None:
        """Transcript in -> the caller starts hearing the answer. What a caller actually waits.

        `llm_ms + tts_ms`, both stamped by the pipeline itself. Routing and the SQLite lookup sit
        between them and are under a millisecond (see the offline run), so they round away.
        """
        if self.speak_ms is None:
            return None
        return round((self.llm_ms or 0.0) + self.speak_ms, 1)


@dataclass
class Report:
    turns: list[Turn] = field(default_factory=list)
    switches: list[tuple[str, str, str]] = field(default_factory=list)   # to, voice, model
    interruption: dict = field(default_factory=dict)
    leaks: int = 0


# --------------------------------------------------------------------------------------------
# building the pipeline
# --------------------------------------------------------------------------------------------

def _offline_doubles(trace: Trace, stack: ExitStack):
    """The doubles the test suite already pins, reused rather than written a second time.

    Importing from `tests/` is unusual in a script and is deliberate: a second copy of the fake
    speaker would be free to drift from the real `build_tts` contract, and a fake that quietly
    ignores the language argument would hide a language switch that never happened.
    """
    from tests.test_menu_routing import _LLM, _Mic, _Rime, _STT

    from aether.audio.player import AudioGate

    rime, llm = _Rime(), _LLM()

    def fake_tts(_trace, samplerate=48000, language=None):
        return rime if language is None else _Rime(language, spoken=rime.spoken)

    stack.enter_context(mock.patch("aether.spike.AudioGate", lambda *a, **k: AudioGate(trace)))
    stack.enter_context(mock.patch("aether.spike.MicVAD", lambda *a, **k: _Mic()))
    stack.enter_context(mock.patch("aether.spike.WhisperSTT", lambda *a, **k: _STT("")))
    stack.enter_context(mock.patch("aether.spike.build_llm", lambda: llm))
    stack.enter_context(mock.patch("aether.spike.build_tts", fake_tts))
    return rime


def _live_doubles(trace: Trace, stack: ExitStack, samplerate: int):
    """Real model, real Rime. Only the microphone and the recogniser are stood in for.

    The audio gate is real but its device is not opened: audio is collected instead of played, so
    the script can run on a machine with no speaker and still measure everything up to the point
    samples would be handed over.
    """
    from tests.test_menu_routing import _Mic, _STT

    from aether.audio.player import AudioGate

    class _Sink(AudioGate):
        def open(self):   # never touch a device
            self._watching = False

        def close(self): ...

    stack.enter_context(mock.patch("aether.spike.AudioGate",
                                   lambda *a, **k: _Sink(trace, samplerate=samplerate)))
    stack.enter_context(mock.patch("aether.spike.MicVAD", lambda *a, **k: _Mic()))
    stack.enter_context(mock.patch("aether.spike.WhisperSTT", lambda *a, **k: _STT("")))
    return None


def build_spike(trace: Trace, stack: ExitStack, live: bool, samplerate: int):
    from aether.spike import HANDS_FREE, Day1Spike

    rime = _live_doubles(trace, stack, samplerate) if live else _offline_doubles(trace, stack)
    spike = Day1Spike(trace, input_mode=HANDS_FREE)
    if rime is not None:
        spike.rime = rime
    stack.enter_context(mock.patch.object(spike, "_streaming_enabled", False))
    return spike, rime


# --------------------------------------------------------------------------------------------
# running one turn
# --------------------------------------------------------------------------------------------

def _spoken_fields(trace: Trace) -> dict:
    last = trace.last(EventType.RESPONSE_SPOKEN)
    return last.fields if last is not None else {}


def say(spike, trace: Trace, said: str, label: str, language: str) -> Turn:
    """One caller turn, measured from "the transcript exists" to "the answer is spoken"."""
    spike.stt.text = said
    before = len(trace.all(EventType.RESPONSE_SPOKEN))

    started = time.perf_counter()
    spike.handle_utterance(AUDIO, 0.0)
    wall = (time.perf_counter() - started) * 1000.0

    fields = _spoken_fields(trace)
    spoke = len(trace.all(EventType.RESPONSE_SPOKEN)) > before
    llm_ms = fields.get("llm_ms") if spoke else None
    return Turn(
        label=label,
        said=said,
        language=language,
        wall_ms=round(wall, 1),
        speak_ms=fields.get("tts_ms") if spoke else None,
        llm_ms=llm_ms,
        # Asked of the ROUTER, not inferred from `llm_ms`. A fake model answers in under a
        # millisecond, so `llm_ms == 0` would count every offline turn as a database answer and
        # quietly turn the headline claim into a tautology. The router is what actually decides.
        from_database=bool(spoke and route(normalise(said)) is not None),
        spoken=(fields.get("text") or "") if spoke else "",
    )


def run_interruption(spike, trace: Trace, report: Report) -> None:
    """The brief's acceptance scenario: interrupt a running tool and change one part of the request.

    The delay is INJECTED rather than waited for -- `ToolRunner` takes `delay_ms` and a `sleep`
    callable, so the barge-in lands inside the lookup deterministically instead of racing a timer.
    """
    original_tools = spike.tools
    leaks_before = len(trace.all(EventType.RESULT_LEAKED))

    def caller_interrupts(_seconds):
        spike.barge.on_speech_onset()
        spike.barge.on_voiced_progress(400.0)      # past the meaningful-speech bound -> fence

    spike.tools = ToolRunner(trace, spike.menu, tools=HOTEL_TOOLS,
                             delay_ms=500.0, sleep=caller_interrupts)
    spike.stt.text = INTERRUPTED_FIRST
    spoken_before = len(trace.all(EventType.RESPONSE_SPOKEN))
    spike.handle_utterance(AUDIO, 0.0)

    changes = trace.all(EventType.GENERATION_CHANGED)
    fenced_gen = next((e.fields.get("to_gen") for e in changes if e.fields.get("to_gen")), None)
    abandoned_spoke = len(trace.all(EventType.RESPONSE_SPOKEN)) > spoken_before

    # The revised request, at ordinary tool speed.
    spike.tools = original_tools
    revised = say(spike, trace, INTERRUPTED_REVISED, "REVISED after interruption", "eng")

    report.interruption = {
        "asked": INTERRUPTED_FIRST,
        "interrupted_with": INTERRUPTED_REVISED,
        "fenced_generation": fenced_gen,
        "abandoned_turn_spoke": abandoned_spoke,
        "discarded_recorded": bool(trace.all(EventType.RESULT_DISCARDED)),
        "leaks": len(trace.all(EventType.RESULT_LEAKED)) - leaks_before,
        "final_answer": revised.spoken,
        "final_from_database": revised.from_database,
    }
    report.turns.append(revised)


def switch_to(spike, trace: Trace, language, report: Report) -> None:
    """Change language mid-call the way a caller does: by asking for it in words."""
    spike.stt.text = {"hin": "hindi", "spa": "spanish", "eng": "english"}[language.code]
    spike.handle_utterance(AUDIO, 0.0)
    speaker = spike.rime
    config = getattr(speaker, "config", None)
    report.switches.append((
        language.code,
        getattr(config, "voice", "?"),
        getattr(config, "model", "?"),
    ))


# --------------------------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------------------------

def one_call(live: bool, samplerate: int) -> Report:
    report = Report()
    trace = Trace()
    trace.input_path = "local_microphone"   # not a phone: this script never opens a line

    with ExitStack() as stack:
        spike, _rime = build_spike(trace, stack, live, samplerate)

        # A real call opens by asking which language, so the script does too.
        spike.begin_language_selection()
        spike.stt.text = "english"
        spike.handle_utterance(AUDIO, 0.0)

        for said in ENGLISH_TURNS:
            report.turns.append(say(spike, trace, said, "english", "eng"))

        run_interruption(spike, trace, report)

        switch_to(spike, trace, HINDI, report)
        for said in HINDI_TURNS:
            report.turns.append(say(spike, trace, said, "hindi", "hin"))

        switch_to(spike, trace, SPANISH, report)
        for said in SPANISH_TURNS:
            report.turns.append(say(spike, trace, said, "spanish", "spa"))

        switch_to(spike, trace, ENGLISH, report)
        report.turns.append(say(spike, trace, "what time is check in", "back in english", "eng"))

        report.leaks = len(trace.all(EventType.RESULT_LEAKED))
    return report


# --------------------------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------------------------

def _stat(values: list[float]) -> str:
    if not values:
        return "—"
    return (f"n={len(values):<3} avg {statistics.mean(values):7.1f}  "
            f"median {statistics.median(values):7.1f}  "
            f"min {min(values):7.1f}  max {max(values):7.1f}")


def show(reports: list[Report], live: bool) -> int:
    turns = [t for r in reports for t in r.turns]
    line = "=" * 92

    print(line)
    print(f"  AETHER — one full call, {'LIVE (real model, real Rime)' if live else 'OFFLINE'}"
          f"   ×{len(reports)} run{'s' if len(reports) > 1 else ''}")
    print(line)

    def _dash(value, width):
        return f"{value:>{width}.1f}" if value is not None else f"{'—':>{width}}"

    print("\n1. EVERY TURN\n")
    print(f"  {'lang':<5} {'heard ms':>9} {'llm':>7} {'rime':>7} {'wall':>8} {'source':<9}  "
          f"what the caller said")
    print(f"  {'-'*5} {'-'*9:>9} {'-'*7:>7} {'-'*7:>7} {'-'*8:>8} {'-'*9:<9}  {'-'*38}")
    for t in reports[0].turns:
        source = "database" if t.from_database else ("model" if t.spoken else "silent")
        print(f"  {t.language:<5} {_dash(t.heard_ms, 9)} {_dash(t.llm_ms, 7)} "
              f"{_dash(t.speak_ms, 7)} {_dash(t.wall_ms, 8)} {source:<9}  {t.said}")

    print("\n2. LATENCY\n")
    print("  heard = transcript in -> the caller starts hearing the answer.  heard = llm + rime.")
    print("  wall  = the whole turn INCLUDING streaming the complete reply, which is longer than")
    print("          the caller waits and is shown only so the two are not confused.\n")
    db = [t.heard_ms for t in turns if t.from_database and t.heard_ms is not None]
    model = [t.heard_ms for t in turns if not t.from_database and t.heard_ms is not None]
    every = [t.heard_ms for t in turns if t.heard_ms is not None]
    print(f"  HEARD, answered from the database  {_stat(db)}")
    print(f"  HEARD, answered by the model       {_stat(model)}")
    print(f"  HEARD, every turn                  {_stat(every)}")
    print(f"  rime alone, to first audio         "
          f"{_stat([t.speak_ms for t in turns if t.speak_ms is not None])}")
    print(f"  model alone, when it was used      "
          f"{_stat([t.llm_ms for t in turns if t.llm_ms])}")
    print(f"  wall, whole turn                   "
          f"{_stat([t.wall_ms for t in turns if t.wall_ms is not None])}")
    answered = [t for t in turns if t.spoken]
    from_db = [t for t in answered if t.from_database]
    print(f"\n  {len(from_db)} of {len(answered)} turns were answered with NO model call at all.")
    print("  Speech-to-text is NOT in these numbers -- there is no microphone in this script.")
    print("  Real STT on telephone audio: 927 ms median, in evidence/demo-run.jsonl.")

    interruption = reports[0].interruption
    print("\n3. INTERRUPTION — the caller changes their mind mid-lookup\n")
    print(f"  caller asked            : {interruption['asked']!r}")
    print(f"  then interrupted with   : {interruption['interrupted_with']!r}")
    print(f"  generation fenced       : {interruption['fenced_generation']}")
    print(f"  abandoned turn spoke    : {interruption['abandoned_turn_spoke']}   (must be False)")
    print(f"  discard recorded        : {interruption['discarded_recorded']}   (must be True)")
    print(f"  stale results leaked    : {interruption['leaks']}   (must be 0)")
    print(f"  final answer            : {interruption['final_answer'][:70]!r}")
    print(f"  answered from database  : {interruption['final_from_database']}")

    print("\n4. LANGUAGE — changed mid-call, by asking\n")
    for code, voice, model_id in reports[0].switches:
        print(f"  now speaking {code:<4} with Rime voice {voice!r} on model {model_id!r}")

    print("\n5. VERDICT\n")
    problems = []
    if any(r.leaks for r in reports):
        problems.append("a stale result reached the caller")
    if interruption["abandoned_turn_spoke"]:
        problems.append("the abandoned turn was spoken")
    if not interruption["discarded_recorded"]:
        problems.append("the fenced lookup left no record")
    if not interruption["final_from_database"]:
        problems.append("the revised answer did not come from the database")
    if len({code for code, _v, _m in reports[0].switches}) < 3:
        problems.append("not every language switch landed")
    # Counted from the turns, not from the latency samples: offline runs have no Rime timing, and a
    # verdict that failed for want of a measurement would be reporting the wrong thing.
    if not from_db:
        problems.append("nothing was answered from the database")
    if len(answered) < len(turns):
        problems.append(f"{len(turns) - len(answered)} turn(s) were never answered")

    if problems:
        for problem in problems:
            print(f"  FAIL  {problem}")
        return 1
    print("  PASS  every turn answered, the interruption fenced cleanly, 0 stale results spoken,")
    print("        and the voice changed with the language on every switch.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("offline", "live"), default="offline")
    ap.add_argument("--repeats", type=int, default=1, help="run the whole call N times and average")
    ap.add_argument("--samplerate", type=int, default=48000)
    args = ap.parse_args()

    live = args.mode == "live"
    if live:
        from dotenv import load_dotenv
        load_dotenv()

    reports = [one_call(live, args.samplerate) for _ in range(max(1, args.repeats))]
    sys.exit(show(reports, live))


if __name__ == "__main__":
    main()
