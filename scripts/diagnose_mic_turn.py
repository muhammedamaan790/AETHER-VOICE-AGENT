"""Say a phrase; see exactly which stage changed it. The local-microphone path, one row per stage.

    python scripts/diagnose_mic_turn.py
    python scripts/diagnose_mic_turn.py --phrases            # walk the five demo phrases
    python scripts/diagnose_mic_turn.py --save capture.wav   # keep the audio of each utterance

Reads the pipeline; changes nothing. No generation is allocated, nothing is fenced, no LLM is
called and no audio is spoken, so running this cannot disturb a session. It needs **no
credentials** -- it constructs only `MicVAD` and `WhisperSTT`, and never reads an API key, so
nothing it prints can be a secret.

**Why this exists.** "AETHER misunderstood me" has at least five distinct causes, and from the
outside they are indistinguishable:

    mic -> VAD          the audio never became an utterance (level, floor, endpointing)
    VAD -> endpointing  the utterance was clipped or ran together
    endpointing -> STT  the buffer handed to Whisper was too short, too quiet, or clipped
    STT -> classifier   the transcript was right and the classifier withheld the turn
    classifier -> router the transcript was right and the wrong tool answered it

Each row below belongs to exactly one of those, so a bad turn names its own stage instead of
being argued about.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.audio.vad import MicVAD, describe_device            # noqa: E402
from aether.classify import classify                            # noqa: E402
from aether.events import EventType                             # noqa: E402
from aether.hotel import MenuStore                              # noqa: E402
from aether.hotel.router import route                           # noqa: E402
from aether.hotel.tools import HOTEL_TOOLS, render              # noqa: E402
from aether.stt import WhisperSTT                               # noqa: E402
from aether.tools import ToolRunner                             # noqa: E402
from aether.trace import Trace                                  # noqa: E402

# The exact phrases the demo depends on. Spoken in this order, they exercise every menu tool the
# product uses plus one sentence that must fall through to the model.
PHRASES = [
    "What starters do you have?",
    "How much is the chicken kebab?",
    "Do you have vegetarian mains?",
    "Is the seafood platter available?",
    "The chicken kebab is three hundred and eighty rupees.",
    "What time do you close?",
]

# int16 full scale. A capture that reaches this is clipped, and clipping is the one audio fault
# that degrades Whisper badly while still sounding fine to a person monitoring the room.
FULL_SCALE = 32767
CLIP_GUARD = 32000


def describe_audio(pcm: np.ndarray, samplerate: int) -> dict:
    """Everything measurable about the buffer that is about to reach Whisper."""
    if not len(pcm):
        return {"samples": 0}
    f = pcm.astype(np.float64)
    peak = int(np.abs(pcm).max())
    clipped = int(np.count_nonzero(np.abs(pcm) >= CLIP_GUARD))
    return {
        "samples": int(len(pcm)),
        "samplerate": samplerate,
        "channels": 1,
        "dtype": str(pcm.dtype),
        "duration_ms": round(len(pcm) / samplerate * 1000.0, 1),
        "rms": round(float(np.sqrt(np.mean(f * f))), 1),
        "peak": peak,
        "peak_dbfs": round(20 * np.log10(max(peak, 1) / FULL_SCALE), 1),
        "clipped_samples": clipped,
        "clipped_pct": round(clipped / len(pcm) * 100.0, 2),
        "leading_silence_ms": round(_edge_silence(pcm, samplerate), 1),
        "trailing_silence_ms": round(_edge_silence(pcm[::-1], samplerate), 1),
    }


def _edge_silence(pcm: np.ndarray, samplerate: int, threshold: int = 200) -> float:
    """How much near-silence sits at the front of the buffer, in ms.

    Front and back are reported separately because they mean different things: too little at the
    front is a clipped opening word, and a lot at the back is late endpointing.
    """
    loud = np.flatnonzero(np.abs(pcm) > threshold)
    return (loud[0] if len(loud) else len(pcm)) / samplerate * 1000.0


def save_wav(path: Path, pcm: np.ndarray, samplerate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(samplerate)
        wav.writeframes(pcm.tobytes())


def report(index: int, expected: str | None, pcm: np.ndarray, mic: MicVAD, stt: WhisperSTT,
           trace: Trace, tools: ToolRunner, save: Path | None) -> None:
    """One utterance, one block, one row per stage."""
    audio = describe_audio(pcm, mic.samplerate)
    ended = trace.last(EventType.SPEECH_ENDED)
    vad = dict(ended.fields) if ended else {}

    print(f"\n{'=' * 92}")
    if expected:
        print(f"  #{index}  EXPECTED: {expected!r}")
    else:
        print(f"  #{index}")
    print("=" * 92)

    print("  MIC -> VAD")
    print(f"    device            : {describe_device(mic.device)}")
    print(f"    frame             : {mic.frame_samples} samples ({mic.frame_ms} ms) "
          f"@ {mic.samplerate} Hz mono int16")
    print(f"    ambient rms       : {vad.get('ambient_rms')}")
    print(f"    speech rms / peak : {vad.get('speech_rms')} / {vad.get('speech_peak_rms')}")
    print(f"    speech floor      : {vad.get('speech_floor')}   "
          f"(set AETHER_SPEECH_FLOOR; derive with scripts/calibrate_mic.py)")
    print(f"    rejected          : {vad.get('rejected') or 'no'}")

    print("  VAD -> ENDPOINTING")
    print(f"    voiced            : {vad.get('voiced_ms')} ms   "
          f"(min_speech_ms={mic.min_speech_ms:.0f})")
    print(f"    buffer            : {vad.get('duration_ms')} ms   "
          f"(preroll {mic.preroll_frames * mic.frame_ms} ms + "
          f"offset {mic.offset_frames * mic.frame_ms} ms of silence)")
    print(f"    leading silence   : {audio['leading_silence_ms']} ms   "
          f"(near zero means the opening word may be clipped)")
    print(f"    trailing silence  : {audio['trailing_silence_ms']} ms")

    print("  ENDPOINTING -> STT")
    print(f"    handed to whisper : {audio['duration_ms']} ms, {audio['samples']} samples, "
          f"{audio['samplerate']} Hz, {audio['channels']} ch, {audio['dtype']}")
    print(f"    level             : rms {audio['rms']}, peak {audio['peak']} "
          f"({audio['peak_dbfs']} dBFS)")
    clip_note = "  <-- CLIPPED" if audio["clipped_pct"] > 0.1 else ""
    print(f"    clipping          : {audio['clipped_samples']} samples "
          f"({audio['clipped_pct']}%){clip_note}")

    t0 = time.perf_counter()
    text = stt.transcribe(pcm)
    stt_ms = (time.perf_counter() - t0) * 1000.0

    print("  STT -> CLASSIFIER")
    print(f"    model             : {stt.model_size}, language=en, beam_size=5, vad_filter=False")
    print(f"    latency           : {stt_ms:.0f} ms")
    print(f"    RAW TRANSCRIPT    : {text!r}")
    if expected:
        match = text.strip().lower().rstrip(".?!") == expected.strip().lower().rstrip(".?!")
        print(f"    matches expected  : {'YES' if match else 'NO'}")
    # faster-whisper exposes no per-word confidence through this wrapper; saying so beats
    # inventing a number.
    print("    confidence        : not exposed by this STT wrapper")

    voiced_ms = vad.get("voiced_ms")
    for in_flight in (False, True):
        decision = classify(text, in_flight=in_flight, voiced_ms=voiced_ms)
        outcome = ("WITHHELD (no turn)" if decision.protects_task
                   else "FENCED, no answer" if decision.cls.value == "CANCEL"
                   else "answered as a turn")
        label = "AETHER speaking" if in_flight else "idle"
        print(f"    class ({label:15}): {decision.cls.value:12} via {decision.rule:20} -> {outcome}")

    print("  CLASSIFIER -> ROUTER")
    decision = route(text)
    if decision is None:
        print("    route             : none -> the model answers this")
    else:
        print(f"    route             : {decision.tool} via {decision.reason}  {decision.params}")
        result = tools.run(decision.tool, gen="diag", turn_id=index,
                           is_valid=lambda: True, **decision.params)
        print(f"    ANSWER            : {render(result)!r}")

    if save is not None:
        path = save.with_name(f"{save.stem}_{index}{save.suffix or '.wav'}")
        save_wav(path, pcm, mic.samplerate)
        print(f"    saved             : {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phrases", action="store_true",
                    help="prompt through the five demo phrases in order")
    ap.add_argument("--count", type=int, default=0,
                    help="stop after this many utterances (0 = until Ctrl-C)")
    ap.add_argument("--save", type=Path, default=None,
                    help="write each captured utterance to WAV (gitignored)")
    ap.add_argument("--stt-model", default="base.en")
    ap.add_argument("--input-device", type=int, default=None)
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    trace = Trace()
    mic = MicVAD(trace, device=args.input_device)
    stt = WhisperSTT(trace, model_size=args.stt_model)
    tools = ToolRunner(trace, MenuStore(), tools=HOTEL_TOOLS)

    expected = list(PHRASES) if args.phrases else []
    limit = args.count or (len(expected) if expected else 0)

    print(f"input device : {describe_device(mic.device)}")
    print(f"speech floor : {mic.speech_floor():.1f}")
    print("Read-only: no generation is allocated, nothing is fenced, nothing is spoken.")
    if expected:
        print(f"\nSay each phrase when prompted. {len(expected)} to go.\n")
    else:
        print("\nSpeak whenever you like. Ctrl-C to stop.\n")

    mic.open()
    seen = 0
    try:
        while True:
            if expected and seen < len(expected):
                print(f"\n>>> SAY: {expected[seen]!r}")
            pcm, _onset = mic.utterances.get()
            seen += 1
            report(seen, expected[seen - 1] if seen <= len(expected) else None,
                   pcm, mic, stt, trace, tools, args.save)
            if limit and seen >= limit:
                break
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        mic.close()
        print(f"\n{seen} utterance(s) captured.")


if __name__ == "__main__":
    main()
