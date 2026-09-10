"""Before-and-after evidence for the delivery claims, in the format the brief asks for.

    python scripts/render_delivery_variants.py

THE BRIEF (page 04, "How to prove the claim"):

    For prompting or delivery claims, hold the model and voice constant, render at least two text
    variants, save the clips, and explain which wording or punctuation changed the result.

AETHER makes delivery claims all over its code and documentation -- prices are spelled as words, a
room number is said digit by digit as a door rather than as a quantity, a time is spoken rather than
punctuated. Those claims were *reasoned*, not *heard*. `scripts/render_rime_compare.py` varies the
MODEL against one fixed sentence, which is the opposite experiment: it answers "which model", not
"which wording".

So this holds the model, the voice and the language constant and varies only the TEXT, one pair at a
time, saving both clips. The judgement itself is a listening task and stays a human's -- this script
produces the material and the measurements, and deliberately draws no conclusion about which sounds
better.

Writes `delivery_<n>_<a|b>.wav` to the repository root (`*.wav` is gitignored). Reads no secret it
prints: the API key opens the socket and is never displayed.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Each pair is ONE claim the codebase actually makes, rendered both ways. The A variant is what
# AETHER speaks today; the B variant is what it would speak if the claim were wrong. Nothing here is
# hypothetical -- every A string is produced by a real renderer.
PAIRS: list[tuple[str, str, str, str]] = [
    (
        "price as words vs digits",
        "aether/hotel/__init__.py:say_price -- 'Rime is never handed a digit'",
        "The Chicken Kebab is four hundred and twenty rupees.",
        "The Chicken Kebab is 420 rupees.",
    ),
    (
        "room number as a door vs a quantity",
        "say_room_number -- 'a room number names a door, not a count'",
        "Room three oh five is occupied at the moment.",
        "Room three hundred and five is occupied at the moment.",
    ),
    (
        "time spoken vs punctuated",
        "say_time -- 'never gamble on how a TTS engine reads a colon'",
        "Check-in is from two in the afternoon, and check-out is by twelve noon.",
        "Check-in is from 14:00, and check-out is by 12:00.",
    ),
    (
        "extension digit by digit vs as a number",
        "tools._speak_service_hours -- 'read the way a phone number is given'",
        "You can reach it on extension one oh one.",
        "You can reach it on extension one hundred and one.",
    ),
]


class _Sink:
    """Stands in for `AudioGate`: collects the PCM the client would have played, nothing else."""

    def __init__(self, samplerate: int):
        self.samplerate = samplerate
        self.blocksize = 480
        self.chunks: list[np.ndarray] = []

    def enqueue(self, pcm, *, turn_id=None, gen=None) -> bool:  # noqa: ARG002
        self.chunks.append(np.asarray(pcm, dtype=np.int16).reshape(-1))
        return True

    def set_active_generation(self, gen, *, turn_id=None) -> None: ...

    @property
    def pcm(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.int16)


def render(tts, text: str, samplerate: int, out: Path) -> dict:
    sink = _Sink(samplerate)
    started = time.perf_counter()
    tts.speak(text, gate=sink, gen="delivery", turn_id=1, is_valid=lambda: True)
    wall = (time.perf_counter() - started) * 1000.0
    pcm = sink.pcm
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(samplerate)
        wav.writeframes(pcm.tobytes())
    return {
        "audio_ms": round(len(pcm) / samplerate * 1000.0, 1),
        "first_audio_ms": round(getattr(tts, "last_latency_ms", 0.0) or 0.0, 1),
        "wall_ms": round(wall, 1),
        "path": out.name,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samplerate", type=int, default=48000)
    args = ap.parse_args()

    import os

    from dotenv import load_dotenv

    load_dotenv()

    from aether.audio.rime_ws import build_tts
    from aether.lang import ENGLISH
    from aether.trace import Trace

    # HELD CONSTANT, which is the whole point of the experiment.
    os.environ.update(RIME_MODEL=ENGLISH.rime_model, RIME_VOICE=ENGLISH.rime_voice,
                      RIME_LANGUAGE=ENGLISH.code, RIME_TRANSPORT="")
    tts = build_tts(Trace(), samplerate=args.samplerate, language=ENGLISH)
    if not tts.configured:
        raise SystemExit("Rime is not configured; missing " + ", ".join(tts.missing_config()))

    root = Path(__file__).resolve().parents[1]
    print(f"held constant: model={ENGLISH.rime_model} voice={ENGLISH.rime_voice} "
          f"lang={ENGLISH.code} transport=/ws3 pcm@{args.samplerate}")
    print("varying: the TEXT only\n")

    # One warm-up so the first pair is not measured on a cold socket -- the brief asks for warm and
    # cold runs to be distinguished, and mixing them inside one comparison would do the opposite.
    render(tts, "Warming the connection.", args.samplerate, root / "delivery_warmup.wav")

    for index, (claim, source, a, b) in enumerate(PAIRS, 1):
        print(f"--- {index}. {claim}")
        print(f"    claim in code : {source}")
        ra = render(tts, a, args.samplerate, root / f"delivery_{index}_a.wav")
        rb = render(tts, b, args.samplerate, root / f"delivery_{index}_b.wav")
        print(f"    A (shipped)   : {a}")
        print(f"                    {ra['audio_ms']} ms audio, first audio {ra['first_audio_ms']} ms"
              f"  -> {ra['path']}")
        print(f"    B (variant)   : {b}")
        print(f"                    {rb['audio_ms']} ms audio, first audio {rb['first_audio_ms']} ms"
              f"  -> {rb['path']}")
        print()

    print("=" * 78)
    print("All runs WARM (one warm-up render precedes the first pair).")
    print("Listen to each A/B pair and record which wording changed the delivery, and how, in")
    print("RIME_EVIDENCE.md. This script measures; it does not judge -- length is not quality, and")
    print("the difference these pairs exist to expose is audible rather than numeric.")


if __name__ == "__main__":
    main()
