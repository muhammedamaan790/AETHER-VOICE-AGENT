"""Does Rime speak Hindi well enough to build on, and which script does it want?

    python scripts/verify_rime_hindi.py

THE GATE BEFORE THE HINDI PATH. Two things are unknown, and every line of the Hindi work depends on
both:

1. Does `arcana` + `anaya` + `hin` actually return audio over `/ws3`?
2. **Does it expect Devanagari or romanised text?** The catalogue says the voice speaks Hindi. It
   does not say what to send it. Guessing here would mean writing the whole template layer in a
   script the engine mispronounces, and finding out on camera.

Answering (2) is the point. The same sentence goes twice -- once in Devanagari, once romanised --
and you listen. `RULES.md` R9.5 is explicit that Rime settings come from a human verifying the live
service, never from an assumption, and a script is a setting.

Writes `rime_hindi_devanagari.wav` and `rime_hindi_romanised.wav` to the repository root (`*.wav` is
gitignored). Prints latency and length for each. **Reads no secret it prints:** the API key is used
to open the socket and never displayed.

If neither render sounds right, that is a real answer: say so, and multilingual goes back to being
declared rather than built. That decision costs this script, not a rewrite.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The Windows console is cp1252 and raises UnicodeEncodeError the moment Devanagari reaches it.
# Worth knowing beyond this script: the turn loop prints every answer it speaks, so ANY code path
# that logs a Hindi string will crash on Windows unless the stream is UTF-8. Fixed here at the
# source rather than by avoiding the characters, because the real fix is the same one.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# The sentence a caller would actually hear: identity, a spelled-out price, and a room fact. It
# carries the two things Hindi rendering has to get right -- a number said as money, and an English
# proper noun (the dish name) sitting inside a Hindi sentence.
DEVANAGARI = (
    "नमस्ते, आप AETHER से बात कर रहे हैं, होटल का ड्यूटी मैनेजर। "
    "Chicken Kebab की कीमत चार सौ बीस रुपये है।"
)
ROMANISED = (
    "Namaste, aap AETHER se baat kar rahe hain, hotel ka duty manager. "
    "Chicken Kebab ki keemat chaar sau bees rupaye hai."
)

# Hindi is not on mistv3. Verified against the public catalogue
# (users.rime.ai/data/voices/voice_details.json, 863 entries): mistv3 offers eng/fra/ger/spa only,
# and `astra` is eng on every model it appears under. Hindi lives on `arcana` and `coda`; `anaya`,
# `anil` and `arya` are the flagship Indian voices on arcana.
MODEL = "arcana"
VOICE = "anaya"
LANGUAGE = "hin"


class _Sink:
    """Stands in for `AudioGate`: collects the PCM the client would have played, and nothing else."""

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


def render(text: str, label: str, samplerate: int, out: Path) -> dict:
    """Synthesise one sentence and write it to `out`. Returns what was measured, nothing invented."""
    import os

    from aether.audio.rime_ws import build_tts
    from aether.trace import Trace

    os.environ["RIME_MODEL"] = MODEL
    os.environ["RIME_VOICE"] = VOICE
    os.environ["RIME_LANGUAGE"] = LANGUAGE
    os.environ["RIME_TRANSPORT"] = ""          # /ws3 streaming, the judged transport

    trace = Trace()
    sink = _Sink(samplerate)
    tts = build_tts(trace, samplerate=samplerate)
    if not tts.configured:
        missing = ", ".join(tts.missing_config())
        raise SystemExit(f"Rime is not configured; missing {missing}. Set it in .env.")

    t0 = time.perf_counter()
    result = tts.speak(text, gate=sink, gen="verify", turn_id=1, is_valid=lambda: True)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    pcm = sink.pcm
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(samplerate)
        wav.writeframes(pcm.tobytes())

    return {
        "label": label,
        "accepted": bool(getattr(result, "accepted", False)),
        "completed": bool(getattr(result, "completed", False)),
        "reason": getattr(result, "reason", None),
        "samples": int(len(pcm)),
        "audio_ms": round(len(pcm) / samplerate * 1000.0, 1),
        "first_audio_ms": round(getattr(tts, "last_latency_ms", 0.0) or 0.0, 1),
        "wall_ms": round(elapsed_ms, 1),
        "path": str(out),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samplerate", type=int, default=48000)
    ap.add_argument("--voice", default=VOICE, help=f"Hindi voice (default {VOICE}); "
                                                  "arcana: anaya, anil, arya | coda: nadi, taru")
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    globals()["VOICE"] = args.voice
    root = Path(__file__).resolve().parents[1]

    print(f"model={MODEL}  voice={args.voice}  lang={LANGUAGE}  transport=/ws3  "
          f"pcm@{args.samplerate}")
    print("(the API key is used to open the socket and is never printed)\n")

    rows = []
    for text, label, name in (
        (DEVANAGARI, "devanagari", "rime_hindi_devanagari.wav"),
        (ROMANISED, "romanised", "rime_hindi_romanised.wav"),
    ):
        print(f"--- {label} ---")
        print(f"    sent: {text}")
        try:
            row = render(text, label, args.samplerate, root / name)
        except Exception as exc:                                  # noqa: BLE001
            print(f"    FAILED: {type(exc).__name__}: {exc}\n")
            rows.append({"label": label, "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows.append(row)
        print(f"    accepted={row['accepted']} completed={row['completed']} "
              f"reason={row['reason']}")
        print(f"    first audio {row['first_audio_ms']} ms, {row['audio_ms']} ms of speech")
        print(f"    wrote {row['path']}\n")

    print("=" * 78)
    ok = [r for r in rows if r.get("samples")]
    if not ok:
        print("NO AUDIO CAME BACK. Hindi is not usable on this account/voice as configured.")
        print("That is a real answer: report it, and leave multilingual declared rather than built.")
        return

    print("LISTEN TO BOTH, then decide -- this cannot be decided from the numbers:")
    print("  1. Is the Hindi intelligible and natural?")
    print("  2. Which script does the engine actually want, Devanagari or romanised?")
    print("  3. Is the English proper noun ('Chicken Kebab') pronounced acceptably inside it?")
    print("\nRecord the answer in RIME_EVIDENCE.md Part 2 before any Hindi templates are written.")


if __name__ == "__main__":
    main()
