"""Render one identical sentence through two Rime models, so a human can listen and choose.

    python scripts/render_rime_compare.py
    python scripts/render_rime_compare.py --models mistv2 mistv3 --voice astra

Writes `rime_<model>.wav` into the repository root. `*.wav` is gitignored, so nothing produced here
is ever committed.

**This script measures two things and judges neither.** It reports audio length and time to first
audio, both of which are facts. Which model *sounds* better is a human judgement that cannot be
made by anything in this repository, and RIME_EVIDENCE.md keeps it as an open checklist item until
somebody has actually listened (RULES.md R10).

Everything else is held constant on purpose -- same sentence, same speaker, same language, same
`/ws3` transport, same PCM sample rate -- so the only variable between the two files is the model.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Identity, a spelled-out price, and a negative: the three things the demo actually has to say.
# Deliberately fixed rather than configurable, so two renders a week apart stay comparable.
SENTENCE = (
    "You've reached AETHER, the hotel manager. The chicken kebab is three hundred and eighty "
    "rupees, and the seafood platter is not available today."
)


class _Sink:
    """Stands in for `AudioGate`: collects the PCM the client would have played, and nothing else.

    Only the three attributes the Rime client touches. Using the real gate would drag in PortAudio
    and generation fencing, neither of which has anything to do with rendering a WAV.
    """

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


def render(model: str, voice: str, language: str, samplerate: int, out: Path) -> dict:
    """Synthesise `SENTENCE` with one model and write it to `out`. Returns the measurements."""
    import os

    from aether.audio.rime_ws import build_tts
    from aether.trace import Trace

    os.environ["RIME_MODEL"] = model
    os.environ["RIME_VOICE"] = voice
    os.environ["RIME_LANGUAGE"] = language
    os.environ["RIME_TRANSPORT"] = ""          # /ws3 streaming, the judged transport

    trace = Trace()
    sink = _Sink(samplerate)
    tts = build_tts(trace, samplerate=samplerate)
    if not tts.configured:
        raise SystemExit("RIME_API_KEY is not set; nothing to render. (The key is never printed.)")

    result = tts.speak(SENTENCE, gate=sink, gen="G1", turn_id=1, is_valid=lambda: True)
    if hasattr(tts, "close"):
        tts.close()

    pcm = sink.pcm
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(samplerate)
        wav.writeframes(pcm.tobytes())

    return {
        "model": model,
        "path": out.name,
        "seconds": round(len(pcm) / samplerate, 2),
        "first_audio_ms": None if result.first_audio_ms is None else round(result.first_audio_ms),
        "samples": int(len(pcm)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs="+", default=["mistv2", "mistv3"])
    ap.add_argument("--voice", default="astra")
    ap.add_argument("--language", default="eng")
    ap.add_argument("--samplerate", type=int, default=48000)
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    out_dir = Path(args.out_dir)
    rows = [
        render(model, args.voice, args.language, args.samplerate,
               out_dir / f"rime_{model}.wav")
        for model in args.models
    ]

    print()
    print(f"sentence : {SENTENCE}")
    print(f"speaker  : {args.voice}   language: {args.language}   "
          f"transport: /ws3   format: pcm@{args.samplerate}")
    print()
    print(f"{'model':10} {'file':22} {'length':>9} {'first audio':>12} {'samples':>10}")
    for row in rows:
        first = "n/a" if row["first_audio_ms"] is None else f"{row['first_audio_ms']} ms"
        print(f"{row['model']:10} {row['path']:22} {row['seconds']:>7.2f}s {first:>12} "
              f"{row['samples']:>10}")
    print()
    print("Now LISTEN to both and choose. Nothing here can tell you which is better --")
    print("record the decision in RIME_EVIDENCE.md Part 2.")


if __name__ == "__main__":
    main()
