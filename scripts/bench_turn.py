"""Headless end-to-end turn bench: WAV -> the real Day1Spike turn -> real LLM -> real Rime.

Why this exists
---------------
Every latency number quoted about AETHER so far came from a component probe, not from the
assembled pipeline. Nothing had ever measured a whole turn through the code that actually runs:
`llm_ttft_ms` and `llm_first_sentence_ms` -- the two fields the sentence-streaming work was built
to expose -- appear in **zero** of the traces in `traces/`, because no harness drives
`Day1Spike.handle_utterance`. `scripts/verify_pipeline.py` predates the streaming path and calls
the HTTP `RimeTTS` directly, so it exercises neither the WS3 transport nor per-sentence fencing.

This bench closes that hole. It runs the *same* `handle_utterance` the live loop runs, with the
real LLM, the real Rime WS3 client and the real AudioGate, and then reads the numbers back off the
append-only trace rather than out of its own stopwatch. What it prints is what the pipeline
recorded -- no separate measurement to disagree with the evidence.

It is NOT the live demo. There is no microphone and no human, so it says nothing about VAD,
endpointing or barge-in latency (PHASES.md Day 6 still requires a real mic and real speech). It
measures the part of the turn that starts once an utterance already exists: STT -> LLM -> Rime.

Usage
-----
    python scripts/bench_turn.py --wav tests/fixtures_stt_probe.wav
    python scripts/bench_turn.py --wav probe.wav --provider groq --repeats 3
    python scripts/bench_turn.py --wav probe.wav --provider gemini --repeats 3

`--provider` sets LLM_PROVIDER for this process only; it never writes to .env. Comparing two
providers means two runs, and the run's provider is recorded in the trace, so a pasted number can
always be traced back to the configuration that produced it.

Audio plays through the real output device, because the AudioGate is the component under test --
`ResponseSpoken` is only emitted once the gate has actually committed samples (MEMORY.md locked
decision 15). Wear headphones if a mic is nearby; nothing here reads the mic, but the point is to
keep the room honest.
"""

from __future__ import annotations

import argparse
import os
import statistics as st
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.audio.rime import resample_int16   # noqa: E402
from aether.events import EventType            # noqa: E402
from aether.spike import Day1Spike             # noqa: E402
from aether.trace import Trace                 # noqa: E402

# Stage fields as the pipeline records them. Absent stays absent: a provider that reports nothing
# prints "--", never 0, so a missing measurement can never be mistaken for a fast one.
FIELDS = [
    ("stt_ms", "STT"),
    ("llm_ms", "LLM (wall)"),
    ("llm_ttft_ms", "  LLM TTFT"),
    ("llm_first_sentence_ms", "  first sentence"),
    ("llm_total_ms", "  LLM total"),
    ("tts_ms", "TTS"),
    ("first_audio_ms", "  Rime first audio"),
    ("turn_latency_ms", "TURN (speech->spoken)"),
    ("response_latency_ms", "RESPONSE"),
    ("audio_ms", "audio produced"),
]


def load_wav_16k(path: str) -> np.ndarray:
    """Read any PCM WAV as the 16 kHz mono int16 the STT stage expects."""
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return resample_int16(pcm, sr, 16000)


def run_turn(spike: Day1Spike, audio: np.ndarray, trace: Trace) -> dict | None:
    """Drive one real turn and return the ResponseSpoken it produced, if it spoke at all."""
    before = len(trace.events)
    # `TurnTiming` anchors stt_ms and turn_latency_ms to the last SpeechEnded on the trace, which
    # only the VAD emits. Without this mark both stages read as `--`, which is how the first run
    # of this bench managed to hide the STT stage entirely.
    #
    # The WAV is a complete utterance, so "the user stopped talking" is the instant it is handed
    # over. That makes stt_ms honest -- it is real transcription time -- but it means
    # `turn_latency_ms` here EXCLUDES VAD endpointing (~620 ms observed live, MEMORY.md §5).
    # A live turn is that much slower than anything this bench prints.
    trace.emit(EventType.SPEECH_ENDED, reason="bench_wav_handoff")
    spike.handle_utterance(audio, 0.0)
    for ev in trace.events[before:]:
        if ev.type == EventType.RESPONSE_SPOKEN.value:
            return {"seq": ev.seq, **ev.fields}
    # A turn that did not speak is a result too -- report why rather than silently scoring it.
    for ev in reversed(trace.events[before:]):
        if ev.type == EventType.RESULT_DISCARDED.value:
            return {"discarded": ev.fields.get("reason"), "stage": ev.fields.get("stage")}
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wav", default="tests/fixtures_stt_probe.wav",
                    help="16-bit PCM WAV of one spoken utterance")
    ap.add_argument("--provider", default=None,
                    help="override LLM_PROVIDER for this process only (groq|gemini|anthropic|openai)")
    ap.add_argument("--stt-model", default="base.en")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--trace-dir", default="traces")
    ap.add_argument("--output-device", type=int, default=None)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".env")

    # Process-local only. Never written back to .env, so a bench run cannot silently change the
    # provider the demo will use.
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider

    wav = Path(args.wav)
    if not wav.exists():
        print(f"!! no such WAV: {wav}")
        return 2
    audio = load_wav_16k(str(wav))

    trace = Trace.new_run(args.trace_dir, echo=False)
    spike = Day1Spike(trace, stt_model=args.stt_model, output_device=args.output_device)

    # The gate is under test, so it is opened. The mic deliberately is not: `MicVAD.__init__` only
    # constructs the stream and `open()` starts it, so never calling `run()` keeps capture off --
    # which matters, because an open mic would hear this bench's own output and fence the turn it
    # is trying to measure.
    spike.gate.open()

    print(f"wav      : {wav}  ({len(audio) / 16000:.2f} s)")
    print(f"LLM      : {spike.llm.name}")
    print(f"Rime     : {spike.rime.name}/{spike.rime.transport} "
          f"model={spike.rime.config.model} voice={spike.rime.config.voice} "
          f"configured={spike.rime.configured}")
    print(f"STT      : {args.stt_model}")
    print(f"gate     : {spike.gate.samplerate} Hz, out latency "
          f"{spike.gate.stream_output_latency_ms:.1f} ms")
    print()

    results: list[dict] = []
    try:
        for i in range(args.repeats):
            print(f"--- turn {i + 1}/{args.repeats} ---")
            row = run_turn(spike, audio, trace)
            if row is None:
                print("  (no ResponseSpoken and no ResultDiscarded -- nothing happened)")
            elif "discarded" in row:
                print(f"  DISCARDED at {row['stage']}: {row['discarded']}")
            else:
                results.append(row)
                print(f'  spoke: "{str(row.get("text"))[:70]}"')
                for key, label in FIELDS:
                    v = row.get(key)
                    print(f"    {label:24s} {v:9.1f} ms" if isinstance(v, (int, float))
                          else f"    {label:24s}        --")
            print()
    finally:
        if hasattr(spike.rime, "close"):
            spike.rime.close()
        spike.gate.close()
        trace.close()

    if len(results) > 1:
        print("=== medians over", len(results), "spoken turns ===")
        for key, label in FIELDS:
            vals = [r[key] for r in results if isinstance(r.get(key), (int, float))]
            print(f"  {label:24s} {st.median(vals):9.1f} ms   (n={len(vals)})" if vals
                  else f"  {label:24s}        --")
    print(f"\ntrace: {trace.path}")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
