"""How long the audio gate itself adds, between accepting a chunk and playing it.

    python scripts/measure_playback_latency.py --repeats 8

WHY THIS EXISTS. Every latency number AETHER quotes stops at `first_audio`: the moment the TTS
client accepts Rime's first chunk. That is upstream of the output queue and of the device buffer,
so it is not "when the caller heard it" -- and quoting it as if it were would overstate the system
by exactly the amount this script measures.

`TurnTiming.output_latency_ms` closes that gap by ending at `first_output`, stamped inside the
PortAudio callback the moment it hands real samples for a generation to the device. This measures
the piece that stamp adds: enqueue -> callback, on the real output device, with no model, no
network and no Rime involved, so the number is the application's own buffering and nothing else.

COLD AND WARM ARE REPORTED SEPARATELY, never averaged together. The first enqueue after `open()`
also pays for the output stream spinning up, which a caller pays once per session and never again;
folding it into the mean would make a one-off setup cost look like a per-turn cost.

The device's own buffer sits AFTER the last point this process can observe. It is printed
alongside, never added -- see `AudioGate.device_latency_ms`.

Needs a working output device. Prints no secret; it opens no network connection at all.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _tone(samplerate: int, ms: float) -> np.ndarray:
    """A quiet tone. Quiet because this runs on a laptop and measures timing, not loudness."""
    n = int(samplerate * ms / 1000.0)
    t = np.arange(n, dtype=np.float64) / samplerate
    return (np.sin(2 * np.pi * 440.0 * t) * 0.05 * 32767).astype(np.int16)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeats", type=int, default=8, help="warm measurements after the cold one")
    ap.add_argument("--samplerate", type=int, default=48000)
    ap.add_argument("--output-device", type=int, default=None)
    ap.add_argument("--tone-ms", type=float, default=200.0)
    args = ap.parse_args()

    from aether.audio.player import AudioGate, preferred_output_device
    from aether.trace import Trace, now_ms

    device = args.output_device if args.output_device is not None else preferred_output_device()
    gate = AudioGate(Trace(), samplerate=args.samplerate, device=device)
    gate.open()

    pcm = _tone(args.samplerate, args.tone_ms)
    samples: list[tuple[str, float]] = []

    try:
        for index in range(args.repeats + 1):
            gen = f"g{index}"
            gate.set_active_generation(gen, turn_id=index)
            asked = now_ms()
            gate.enqueue(pcm, turn_id=index, gen=gen)

            # Wait for the callback to stamp it. Polling, not sleeping a fixed amount: a fixed
            # sleep would silently become the measurement floor.
            deadline = time.perf_counter() + 3.0
            stamped = None
            while time.perf_counter() < deadline:
                stamped = gate.first_output_ms(gen)
                if stamped is not None:
                    break
                time.sleep(0.001)
            if stamped is None:
                raise SystemExit(f"the callback never played {gen}; is the output device live?")

            samples.append(("cold" if index == 0 else "warm", round(stamped - asked, 1)))
            # Let the tone finish, so the next enqueue starts from an idle queue rather than
            # measuring how long the previous tone still had left to play.
            time.sleep(args.tone_ms / 1000.0 + 0.05)

        device_ms = gate.device_latency_ms
    finally:
        gate.close()

    warm = [ms for kind, ms in samples if kind == "warm"]
    cold = next(ms for kind, ms in samples if kind == "cold")

    print(f"device            : {device if device is not None else 'PortAudio default'} "
          f"@ {args.samplerate} Hz, blocksize {gate.blocksize}")
    print(f"enqueue -> device : COLD {cold} ms  (includes output-stream spin-up; paid once)")
    if warm:
        print(f"enqueue -> device : WARM n={len(warm)} min {min(warm)} / median "
              f"{round(statistics.median(warm), 1)} / max {max(warm)} ms")
    print(f"device buffer     : {device_ms} ms  (reported, NEVER added to the figures above)")
    print()
    print("This is the application's own buffering only -- no model, no network, no Rime. It is")
    print("the difference between `turn_latency_ms` and `output_latency_ms` in TurnTiming.")


if __name__ == "__main__":
    main()
