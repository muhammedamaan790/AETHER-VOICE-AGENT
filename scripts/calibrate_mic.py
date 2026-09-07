"""Derive the speech floor for THIS microphone, instead of guessing a constant.

Why this exists
---------------
A hardcoded absolute floor cannot suit every microphone, and trying anyway caused a real failure:
a floor of 60 RMS was set from one session where every transcribed utterance measured 61 or above,
and it then rejected genuine speech at 37-59 on the same machine in a later session. Gain,
distance, room and voice all move the absolute numbers; `aether/audio/vad.py`'s own docstring
warned against "an absolute number that would need retuning per environment" before one was added.

So measure instead. This samples the room, then samples you speaking, and prints the value to set.

    python scripts/calibrate_mic.py
    # then put the printed line in .env:  AETHER_SPEECH_FLOOR=...

What it measures
----------------
The **peak** RMS of voiced frames, not the mean, because that is what the gate now tests. Speech
carries vowel peaks far above its own average, while low-level noise is flat -- and a mean is
dragged down by the quiet voiced frames every longer utterance contains, which is precisely what
made long real sentences look quieter than short ones.

The recommendation is placed between the two measured populations rather than at a fixed offset
from either: high enough to sit clear of the room, low enough to leave real headroom under the
quietest thing you actually said. If the two overlap it says so and recommends nothing, because a
floor cannot separate populations that are not separated.

It drives the same `MicVAD.process_frame` the live pipeline uses, so the numbers describe the real
path rather than a parallel measurement.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.audio.vad import DEFAULT_SPEECH_FLOOR, MicVAD, describe_device  # noqa: E402
from aether.trace import Trace                                             # noqa: E402

PROMPTS = [
    "Find the priority orders in aisle nine.",
    "Actually, only the ones that are in stock.",
    "Stop.",
    "How many are left?",
]


def _collect(vad: MicVAD, seconds: float) -> list[float]:
    """Run the mic for `seconds` and return the per-frame peak RMS of each voiced run."""
    peaks: list[float] = []
    deadline = time.monotonic() + seconds
    last_n = 0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        # A completed run shows up as the peak being reset; sample it while it is still growing.
        if vad._voiced_rms_n > last_n and vad._voiced_rms_peak > 0:
            peaks.append(vad._voiced_rms_peak)
        last_n = vad._voiced_rms_n
    return peaks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input-device", type=int, default=None)
    ap.add_argument("--room-seconds", type=float, default=5.0)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".env")

    vad = MicVAD(Trace(), device=args.input_device)
    print(f"microphone : {describe_device(vad.device)}")
    print(f"current floor: {vad.speech_floor_abs}  (default {DEFAULT_SPEECH_FLOOR})\n")
    vad.open()

    try:
        # --- 1. the room -----------------------------------------------------------------
        print(f"[1/2] Stay SILENT for {args.room_seconds:.0f}s -- measuring the room...")
        room_peaks = _collect(vad, args.room_seconds)
        ambient = vad._ambient_rms or 0.0
        room_peak = max(room_peaks) if room_peaks else 0.0
        print(f"      ambient RMS {ambient:.1f}   loudest thing heard while silent: {room_peak:.1f}\n")

        # --- 2. you ----------------------------------------------------------------------
        print("[2/2] Now SPEAK each line normally, at your usual distance:")
        speech_peaks: list[float] = []
        for line in PROMPTS:
            print(f'      say: "{line}"')
            vad._voiced_rms_peak = 0.0
            vad._voiced_rms_n = 0
            got = _collect(vad, 3.5)
            peak = max(got) if got else 0.0
            print(f"           peak {peak:.1f}")
            if peak > 0:
                speech_peaks.append(peak)
    finally:
        vad.close()

    # --- recommendation ------------------------------------------------------------------
    print()
    if not speech_peaks:
        print("No speech was detected at all. Check the input device, then run this again.")
        return 1

    quietest = min(speech_peaks)
    noise_ceiling = max(room_peak, ambient)
    print(f"quietest utterance peak : {quietest:.1f}")
    print(f"room noise ceiling      : {noise_ceiling:.1f}")

    if quietest <= noise_ceiling:
        print("\nYour quietest speech is not louder than the room. No floor can separate them.")
        print("Move closer to the mic, raise the input gain, or use a headset, then re-run.")
        return 1

    # Geometric midpoint: equidistant from both populations in the ratio sense, which is how
    # audio levels actually behave. Then held to at most half the quietest utterance, so there is
    # always real headroom under the softest thing actually said.
    midpoint = (quietest * max(noise_ceiling, 1.0)) ** 0.5
    recommended = round(min(midpoint, quietest / 2.0), 1)

    print(f"\nRecommended:  AETHER_SPEECH_FLOOR={recommended}")
    print(f"  ({recommended / quietest:.0%} of your quietest utterance, "
          f"{recommended / max(noise_ceiling, 0.1):.1f}x the room)")
    print("\nPut that line in .env. Re-run this if you change microphone, gain or seating.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
