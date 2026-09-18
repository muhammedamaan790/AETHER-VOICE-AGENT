"""Round-trip a real database answer through Rime and back, in every language, with both recognisers.

    python scripts/compare_recognisers.py

**What this measures, and why it is a round trip.** "Is the Hindi pronunciation right?" and "is the
recogniser good enough?" cannot be answered separately on a phone line, because the caller hears
Rime and AETHER hears Whisper. So each deterministic answer is spoken by the voice that would
actually speak it, and then transcribed by each recogniser that might actually hear it. What comes
back out is compared with what went in.

It is not a substitute for listening. A transcript that matches proves the words survived; it says
nothing about whether they sounded natural. The WAVs are kept for exactly that reason.

Costs Rime and Groq API calls. Nothing is written except WAVs into the scratch directory.
"""

from __future__ import annotations

import argparse
import difflib
import io
import os
import re
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


def normalise(text: str) -> str:
    """Lowercase, strip punctuation and collapse space, so a comparison is about WORDS.

    A recogniser that returns the right words without a full stop has not made a mistake a caller
    would notice, and counting it as one would hide the mistakes that matter.
    """
    return " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())


def similarity(said: str, heard: str) -> float:
    return difflib.SequenceMatcher(None, normalise(said), normalise(heard)).ratio()


class _Sink:
    """Collects the PCM Rime produces, standing in for the AudioGate."""

    def __init__(self, samplerate: int):
        self.samplerate, self.blocksize, self.chunks = samplerate, 480, []

    def enqueue(self, pcm, *, turn_id=None, gen=None) -> bool:      # noqa: ARG002
        self.chunks.append(np.asarray(pcm, dtype=np.int16).reshape(-1))
        return True

    def set_active_generation(self, gen, *, turn_id=None) -> None: ...

    @property
    def pcm(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.int16)


def speak(text: str, language, samplerate: int) -> np.ndarray:
    """Synthesise with the voice this language actually uses."""
    from dataclasses import replace

    from aether.audio.rime_ws import RimeStreamingTTS
    from aether.config import RimeConfig
    from aether.trace import Trace

    config = replace(
        RimeConfig.from_env(),
        model=language.rime_model, voice=language.rime_voice, language=language.code,
    )
    tts = RimeStreamingTTS(Trace(), config=config, samplerate=samplerate)
    sink = _Sink(samplerate)
    tts.speak(text, gate=sink, gen="cmp", turn_id=1, is_valid=lambda: True)
    return sink.pcm


def to_wav(pcm: np.ndarray, samplerate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(samplerate)
        out.writeframes(pcm.tobytes())
    return buf.getvalue()


def resample_to_16k(pcm: np.ndarray, src_rate: int) -> np.ndarray:
    """Whisper wants 16 kHz, and the phone path resamples to it too -- so this mirrors the real
    pipeline rather than handing the recogniser a cleaner signal than it would ever get."""
    if src_rate == 16000:
        return pcm
    n = int(round(len(pcm) * 16000 / src_rate))
    idx = np.linspace(0, len(pcm) - 1, n)
    return np.interp(idx, np.arange(len(pcm)), pcm.astype(np.float64)).astype(np.int16)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", type=Path, default=None, help="directory to write the WAVs into")
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv(".env", override=True)

    from aether.hotel import HotelStore
    from aether.hotel.router import route
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.lang import ENGLISH, HINDI, SPANISH
    from aether.tools import ToolRunner
    from aether.trace import Trace

    store = HotelStore()
    runner = ToolRunner(Trace(), store, tools=HOTEL_TOOLS)

    questions = [
        "How much is the chicken kebab?",
        "Is room three zero five free?",
        "What time is check in?",
    ]

    out_dir = args.keep or Path(os.environ.get("TEMP", ".")) / "aether_compare"
    out_dir.mkdir(parents=True, exist_ok=True)

    from aether.stt import WhisperSTT
    from aether.stt_groq import GroqSTT

    rate = 48000
    local_cache: dict[str, WhisperSTT] = {}
    groq = GroqSTT(Trace(), samplerate=16000)

    for language in (ENGLISH, HINDI, SPANISH):
        print(f"\n{'=' * 78}\n{language.name}  --  Rime {language.rime_model}/{language.rime_voice}"
              f"  |  local {language.whisper_model}\n{'=' * 78}")
        for question in questions:
            decision = route(question)
            result = runner.run(decision.tool, gen="c", turn_id=1, is_valid=lambda: True,
                                **decision.params)
            said = render(result, language)

            pcm = speak(said, language, rate)
            if not len(pcm):
                print(f"  [{decision.tool}] NO AUDIO FROM RIME"); continue
            path = out_dir / f"{language.code}_{decision.tool}.wav"
            path.write_bytes(to_wav(pcm, rate))
            narrow = resample_to_16k(pcm, rate)

            print(f"\n  [{decision.tool}]  {len(pcm)/rate:.1f}s  -> {path.name}")
            print(f"    SAID  : {said}")

            if language.whisper_model not in local_cache:
                local_cache[language.whisper_model] = WhisperSTT(
                    Trace(), model_size=language.whisper_model)
            local = local_cache[language.whisper_model]
            local.samplerate = 16000
            t0 = time.perf_counter()
            heard_local = local.transcribe(narrow, turn_id=1, language=language)
            ms_local = (time.perf_counter() - t0) * 1000

            groq.samplerate = 16000
            t0 = time.perf_counter()
            heard_groq = groq.transcribe(narrow, turn_id=1, language=language)
            ms_groq = (time.perf_counter() - t0) * 1000

            print(f"    local : {heard_local}")
            print(f"            {ms_local:6.0f} ms   match {similarity(said, heard_local)*100:5.1f}%")
            print(f"    groq  : {heard_groq}")
            print(f"            {ms_groq:6.0f} ms   match {similarity(said, heard_groq)*100:5.1f}%")

    print(f"\nWAVs written to {out_dir} -- listen to them; a match score is not a judgement of "
          "how it sounds.")


if __name__ == "__main__":
    main()
