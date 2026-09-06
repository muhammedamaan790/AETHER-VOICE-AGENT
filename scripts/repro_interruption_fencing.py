"""Real-audio reproduction: a fenced generation's audio is CUT, not drained.

Synthesises a long utterance with Rime under G1, plays it, forces an interruption partway through,
then does what a real interruption does: fence G1, refuse G1's late synthesiser result, and let G2
speak. Prints the trace and compares how much audio was COMMITTED against how long the speaker was
actually fed, which is what "cut, not drained" means in numbers.

Safe mode only. Test-only unsafe mode (RULES.md R11) arrives on Day 4.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.audio.player import AudioGate      # noqa: E402
from aether.audio.rime import RimeTTS          # noqa: E402
from aether.events import EventType            # noqa: E402
from aether.trace import Trace, now_ms         # noqa: E402

OLD = ("This is the first answer, and it is deliberately long so that there is plenty of audio "
       "still queued and still being played when the interruption arrives.")
NEW = "Here is the new answer."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interrupt-after-ms", type=float, default=700.0)
    ap.add_argument("--trace-dir", default="traces")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    trace = Trace.new_run(args.trace_dir, echo=True)
    gate = AudioGate(trace)
    gate.open()
    rime = RimeTTS(trace)

    print(f"provider={rime.name} configured={rime.configured}\n")

    # --- G1 starts speaking ---------------------------------------------------------
    old_pcm = rime.synthesize(OLD, target_samplerate=gate.samplerate, gen="G1")
    old_ms = len(old_pcm) / gate.samplerate * 1000.0
    gate.set_active_generation("G1", turn_id=1)
    gate.enqueue(old_pcm, turn_id=1, gen="G1")
    trace.emit(EventType.RESPONSE_SPOKEN, turn_id=1, gen="G1", provider=rime.name,
               text=OLD, audio_ms=round(old_ms, 1))
    playback_started = now_ms()
    print(f"\nG1 committed {old_ms:.0f} ms of Rime audio\n")

    # --- interruption -----------------------------------------------------------------
    time.sleep(args.interrupt_after_ms / 1000.0)
    trace.emit(EventType.SPEECH_ONSET)
    gate.request_duck(reason="speech_onset")
    trace.emit(EventType.FENCE_REQUESTED, gen="G1", reason="meaningful_interruption")
    gate.fence_generation("G1", reason="meaningful_interruption")
    gate._emitted.wait(1.0)
    playback_stopped = now_ms()
    actually_played = playback_stopped - playback_started

    # --- G1's synthesiser result arrives late (the stale result) ----------------------
    late_pcm = rime.synthesize("This late answer must never be heard.",
                               target_samplerate=gate.samplerate, gen="G1")
    accepted = gate.enqueue(late_pcm, turn_id=1, gen="G1")
    print(f"\nlate G1 audio accepted? {accepted}  (must be False)\n")

    # --- G2 speaks --------------------------------------------------------------------
    new_pcm = rime.synthesize(NEW, target_samplerate=gate.samplerate, gen="G2")
    gate.set_active_generation("G2", turn_id=2)
    if gate.enqueue(new_pcm, turn_id=2, gen="G2"):
        trace.emit(EventType.RESPONSE_SPOKEN, turn_id=2, gen="G2", provider=rime.name,
                   text=NEW, audio_ms=round(len(new_pcm) / gate.samplerate * 1000.0, 1))
    gate.wait_until_drained(timeout=15)
    gate.close()
    trace.close()

    # --- verdict ----------------------------------------------------------------------
    fence = trace.first(EventType.FENCE_REQUESTED)
    stopped = trace.first(EventType.AUDIO_STOPPED)
    g1_discards = [e for e in trace.all(EventType.RESULT_DISCARDED) if e.gen == "G1"]
    g1_spoken_after = [e for e in trace.all(EventType.RESPONSE_SPOKEN)
                       if e.gen == "G1" and e.seq > fence.seq]

    print("=" * 68)
    print(f"G1 audio committed          : {old_ms:8.0f} ms")
    print(f"G1 audio actually played    : {actually_played:8.0f} ms")
    print(f"G1 audio never played (cut) : {old_ms - actually_played:8.0f} ms")
    print("-" * 68)
    print(f"AudioStopped after fence    : {stopped.seq > fence.seq}")
    print(f"G1 ResultDiscarded events   : {len(g1_discards)}")
    print(f"G1 spoke after fence        : {len(g1_spoken_after)}  (must be 0)")
    print(f"ResultLeaked events         : {len(trace.all(EventType.RESULT_LEAKED))}  (must be 0)")
    print("=" * 68)
    print(f"trace: {trace.path}")

    ok = (stopped.seq > fence.seq and g1_discards and not g1_spoken_after
          and actually_played < old_ms)
    print("\nVERDICT:", "CUT, not drained — invariant held" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
