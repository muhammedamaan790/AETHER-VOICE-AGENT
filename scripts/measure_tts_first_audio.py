"""Measure text-sent -> first-audio latency for both Rime transports, against the live API.

    python scripts/measure_tts_first_audio.py
    python scripts/measure_tts_first_audio.py --interrupt-after-ms 400

`first_audio_ms` is the metric that matters for a voice agent: how long the caller waits in silence
before the agent starts speaking. Streaming should be far below the HTTP figure, because HTTP
cannot make a sound until the entire utterance has been synthesised and downloaded.

Real API calls. Nothing here is simulated, and no number is written down that did not come from a
run. Audio is played through the real AudioGate.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.audio.player import AudioGate            # noqa: E402
from aether.audio.rime_ws import RimeHttpSpeaker, RimeStreamingTTS   # noqa: E402
from aether.trace import Trace, now_ms               # noqa: E402

SENTENCE = ("The three priority orders are ten forty two, ten forty seven, and ten fifty one, "
            "and they are all waiting in aisle nine.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--interrupt-after-ms", type=float, default=None,
                    help="fence mid-speech this long after the first audio, to test clear")
    ap.add_argument("--silent", action="store_true", help="do not open an output device")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    trace = Trace()
    gate = AudioGate(trace)
    if not args.silent:
        gate.open()

    ws = RimeStreamingTTS(trace, samplerate=gate.samplerate)
    http = RimeHttpSpeaker(trace, samplerate=gate.samplerate)
    if not ws.configured:
        print(f"Rime not configured; missing {', '.join(ws.missing_config())}")
        return 2

    print(f"model={ws.config.model} voice={ws.config.voice} rate={gate.samplerate} format=pcm\n")

    results: dict[str, list[float]] = {"ws3": [], "http": []}
    for label, client in (("ws3", ws), ("http", http)):
        for i in range(args.repeats):
            gate.set_active_generation(f"G-{label}-{i}")
            r = client.speak(SENTENCE, gate=gate, gen=f"G-{label}-{i}", turn_id=i)
            if r.first_audio_ms is None:
                print(f"  {label} trial {i+1}: NO AUDIO ({r.reason})")
            else:
                results[label].append(r.first_audio_ms)
                print(f"  {label} trial {i+1}: first_audio={r.first_audio_ms:.0f} ms  "
                      f"total={client.last_latency_ms:.0f} ms  "
                      f"audio={r.samples / gate.samplerate:.2f}s  completed={r.completed}")
            gate.stop(reason="measurement_teardown")
            time.sleep(0.3)

    print()
    for label, vals in results.items():
        if vals:
            print(f"{label:>5} first_audio_ms  n={len(vals)}  "
                  f"min={min(vals):.0f}  median={statistics.median(vals):.0f}  max={max(vals):.0f}")
        else:
            print(f"{label:>5}: no successful trials")

    # --- mid-speech interruption against the live socket ---
    if args.interrupt_after_ms is not None:
        print(f"\n--- interruption test: fence {args.interrupt_after_ms:.0f} ms after first audio ---")
        gen = "G-interrupt"
        gate.set_active_generation(gen)
        state = {"t_first": None}

        def is_valid():
            if state["t_first"] is None:
                return True
            return (now_ms() - state["t_first"]) < args.interrupt_after_ms

        t0 = now_ms()
        original_enqueue = gate.enqueue

        def watching_enqueue(pcm, **kw):
            ok = original_enqueue(pcm, **kw)
            if ok and state["t_first"] is None:
                state["t_first"] = now_ms()
            return ok

        gate.enqueue = watching_enqueue
        r = ws.speak(SENTENCE, gate=gate, gen=gen, turn_id=99, is_valid=is_valid)
        gate.enqueue = original_enqueue

        spoken_s = r.samples / gate.samplerate
        print(f"  first_audio      : {r.first_audio_ms} ms")
        print(f"  audio streamed   : {spoken_s:.2f} s before the fence")
        print(f"  completed        : {r.completed}  (False = cut off, as intended)")
        print(f"  reason           : {r.reason}")
        print(f"  wall time        : {now_ms() - t0:.0f} ms")
        gate.stop(reason="interrupt_teardown")

    gate.close()
    trace.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
