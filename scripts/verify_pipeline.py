"""Offline verification of the Day-1 pipeline: WAV -> VAD -> STT -> LLM -> Rime.

Drives the same MicVAD, WhisperSTT, LLM and RimeTTS objects the live spike uses, but feeds audio
from a file instead of the microphone. Lets the whole chain be re-checked without a person and
without a mic, and shows exactly where it stops when Rime is unconfigured.

This is NOT the live demo (PHASES.md Day 6 requires real mic + real human speech + real Rime).

Make a test WAV on Windows without adding any TTS dependency to the project:

    powershell -c "Add-Type -AssemblyName System.Speech; \
      $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; \
      $s.SetOutputToWaveFile('probe.wav'); $s.Speak('your sentence here'); $s.Dispose()"

That synthesiser is a throwaway test fixture. It is not a TTS provider for AETHER and never
touches the spoken response path (RULES.md R9.4).
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.audio.rime import RimeNotConfigured, RimeTTS, resample_int16  # noqa: E402
from aether.audio.vad import MicVAD                                       # noqa: E402
from aether.events import EventType                                       # noqa: E402
from aether.llm import build_llm                                          # noqa: E402
from aether.stt import WhisperSTT                                         # noqa: E402
from aether.supervisor.generations import GenerationRegistry              # noqa: E402
from aether.trace import Trace                                            # noqa: E402


def load_wav_16k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return resample_int16(pcm, sr, 16000)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wav", required=True, help="16-bit PCM WAV of a spoken utterance")
    ap.add_argument("--stt-model", default="base.en")
    ap.add_argument("--trace-dir", default="traces")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    trace = Trace.new_run(args.trace_dir, echo=True)
    gens = GenerationRegistry(trace)
    vad = MicVAD(trace)
    stt = WhisperSTT(trace, model_size=args.stt_model)
    llm = build_llm()
    rime = RimeTTS(trace)

    audio = load_wav_16k(args.wav)
    print(f"\ninput: {args.wav}  ({len(audio) / 16000:.2f}s @16k)\n")

    # Feed the file through the real detector, frame by frame, then trailing silence so the
    # detector sees speech offset (WebRTC VAD hangover means it needs more than offset_frames).
    n = vad.frame_samples
    for i in range(len(audio) // n):
        vad.process_frame(audio[i * n:(i + 1) * n])
    for _ in range(vad.offset_frames + 10):
        vad.process_frame(np.zeros(n, dtype=np.int16))

    if vad.utterances.empty():
        print("\nFAIL: VAD produced no utterance from this file")
        return 1
    utt, _onset_t = vad.utterances.get_nowait()

    turn = 1
    gen = gens.allocate(turn_id=turn)
    text = stt.transcribe(utt, turn_id=turn, gen=gen.id)
    if not text:
        print("\nFAIL: empty transcript")
        return 1

    trace.emit(EventType.TASK_STARTED, turn_id=turn, gen=gen.id, task="respond",
               params={"utterance": text})
    reply = llm.respond(text)
    print(f"\nLLM({llm.name}): {reply}")

    spoke = False
    try:
        pcm = rime.synthesize(reply, target_samplerate=48000, turn_id=turn, gen=gen.id)
        trace.emit(EventType.RESPONSE_SPOKEN, turn_id=turn, gen=gen.id, provider=rime.name,
                   text=reply, tts_latency_ms=rime.last_latency_ms,
                   audio_ms=round(len(pcm) / 48000 * 1000.0, 1))
        print(f"Rime returned {len(pcm) / 48000:.2f}s of audio")
        spoke = True
    except RimeNotConfigured as exc:
        print(f"\nBLOCKED AT TTS: {exc}")
    except Exception as exc:  # network/contract failure against a configured endpoint
        print(f"\nRIME REQUEST FAILED: {type(exc).__name__}: {exc}")

    print("\n--- stage summary ---")
    print(f"  VAD onset      : {'ok' if trace.first(EventType.SPEECH_ONSET) else 'MISSING'}")
    print(f"  VAD offset     : {'ok' if trace.first(EventType.SPEECH_ENDED) else 'MISSING'}")
    print(f"  STT transcript : {text!r}")
    print(f"  LLM provider   : {llm.name}")
    print(f"  Rime spoke     : {spoke}")
    print(f"  trace          : {trace.path}")
    trace.close()
    return 0 if spoke else 2


if __name__ == "__main__":
    raise SystemExit(main())
