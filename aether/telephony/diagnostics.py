"""Where did a call actually fail? Answered from measurements, never from a guess.

**No LiveKit import**, like the rest of this package's non-`agent` modules, so it runs in the main
environment and under test.

The first real call produced "AETHER never replied", which is six different faults wearing the same
coat. This module walks the pipeline in order and reports the FIRST stage that produced nothing,
using the bridge counters and the canonical trace events:

    no inbound audio       samples_received == 0
    not listening          audio arrived, every block dropped by the listening gate
    VAD rejection          frames reached the VAD, no SpeechOnset -- levels are reported alongside
    STT failure            speech was detected, TranscriptFinal carried no text
    routing / LLM          a transcript existed, nothing was produced to speak
    Rime failure           something was produced, ResultDiscarded at stage "tts"
    outbound audio         ResponseSpoken exists, no audible block ever left the pump

It states what it measured and stops. It does not tune anything, does not recommend a threshold,
and does not decide whether a number is good -- deriving a new speech floor is a deliberate act
performed against real call audio, not a side effect of reading a report (see the plan's section 6).
"""

from __future__ import annotations

from typing import Any

from ..events import EventType

# The order the pipeline runs in. The first stage to fail is the one worth reporting: everything
# after it failed *because* of it, and listing all seven would bury the answer.
STAGES = (
    "inbound_audio", "listening_gate", "vad", "stt", "reply", "tts", "outbound_audio",
)


def _texts(trace, event_type) -> list[str]:
    return [(e.fields.get("text") or "").strip() for e in trace.all(event_type)]


def diagnose(trace, inbound=None, outbound=None) -> dict[str, Any]:
    """Build the report. `inbound`/`outbound` are the bridges; either may be absent.

    Returns a dict with `stage` -- the first stage that produced nothing, or None when the call
    completed a spoken turn -- plus the counts each verdict was drawn from, so the verdict can be
    checked rather than believed.
    """
    onsets = len(trace.all(EventType.SPEECH_ONSET))
    transcripts = _texts(trace, EventType.TRANSCRIPT_FINAL)
    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    discards = trace.all(EventType.RESULT_DISCARDED)
    tts_discards = [e for e in discards if e.fields.get("stage") == "tts"]

    inbound_stats = inbound.diagnostics() if inbound is not None else {}
    outbound_stats = outbound.diagnostics() if outbound is not None else {}

    report: dict[str, Any] = {
        "stage": None,
        "detail": "a turn was spoken end to end",
        "inbound": inbound_stats,
        "outbound": outbound_stats,
        "speech_onsets": onsets,
        "transcripts": len([t for t in transcripts if t]),
        "empty_transcripts": len([t for t in transcripts if not t]),
        "responses_spoken": len(spoken),
        "results_discarded": len(discards),
        "leaks": len(trace.all(EventType.RESULT_LEAKED)),
    }

    def fail(stage: str, detail: str) -> dict[str, Any]:
        report["stage"] = stage
        report["detail"] = detail
        return report

    if inbound is not None:
        if inbound.samples_received == 0:
            return fail("inbound_audio",
                        "no audio reached the bridge at all -- the caller's track was never "
                        "subscribed, or the pump never started")
        if inbound.frames_delivered == 0:
            if inbound.frames_dropped_not_listening:
                return fail("listening_gate",
                            f"audio arrived and every block was dropped: listening was off for "
                            f"{inbound.frames_dropped_not_listening} blocks")
            return fail("listening_gate",
                        "audio arrived but produced no VAD frames -- check the frame size and the "
                        "resample path")

    if onsets == 0:
        levels = ""
        if inbound_stats:
            levels = (f" (peak rms {inbound_stats['peak_rms']}, floor {inbound_stats['floor_rms']}, "
                      f"speech_floor {inbound_stats['speech_floor']})")
        return fail("vad", "audio reached the VAD and no speech onset was detected" + levels)

    if not any(transcripts):
        return fail("stt", f"speech was detected {onsets} time(s) and every transcript was empty")

    if not spoken:
        if tts_discards:
            reasons = sorted({e.fields.get("reason") for e in tts_discards})
            return fail("tts", f"a reply was produced and synthesis failed: {', '.join(map(str, reasons))}")
        reasons = sorted({e.fields.get("reason") for e in discards}) or ["no result was produced"]
        return fail("reply", f"a transcript existed and nothing was produced to speak: "
                             f"{', '.join(map(str, reasons))}")

    if outbound is not None and outbound.blocks_with_audio == 0:
        return fail("outbound_audio",
                    "a turn was spoken and no audible block ever left the pump -- the caller heard "
                    "silence")

    return report


def format_report(report: dict[str, Any]) -> str:
    """One block of plain text for the worker log. Facts, then the verdict."""
    lines = ["--- call diagnostics ---"]
    for key in ("inbound", "outbound"):
        stats = report.get(key) or {}
        if stats:
            lines.append(f"{key:9}: " + "  ".join(f"{k}={v}" for k, v in stats.items()))
    # Transport-level facts the caller may have attached. Reported on their own line because they
    # answer a different question from the pipeline counts: not "how far did a turn get" but
    # "was the audio that arrived intact". `pumps` above 1 for a single caller means one track was
    # attached twice and two readers were interleaved into one buffer -- audio arrives, doubled and
    # scrambled, and every count below still looks healthy.
    transport = {k: report[k] for k in ("inbound_pumps", "inbound_frames_failed")
                 if report.get(k) is not None}
    if transport:
        lines.append("transport: " + "  ".join(f"{k[8:]}={v}" for k, v in transport.items()))
    lines.append(
        "pipeline : "
        f"onsets={report['speech_onsets']}  transcripts={report['transcripts']}"
        f"  empty={report['empty_transcripts']}  spoken={report['responses_spoken']}"
        f"  discarded={report['results_discarded']}  leaks={report['leaks']}"
    )
    stage = report.get("stage")
    lines.append(f"verdict  : {'OK' if stage is None else stage.upper()} -- {report['detail']}")
    return "\n".join(lines)
