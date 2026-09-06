"""Measure audio-kill latency: VAD onset -> agent audio stopped.

Metric (PRD.md section 6):  audio_kill_latency_ms = AudioStopped.t - SpeechOnset.t

`SpeechOnset.t` is the arrival time of the first voiced frame, so the VAD confirmation window is
included in the number. Both modes drive the same MicVAD.process_frame and the same AudioGate used
by the live spike.

Modes
-----
synthetic   Injects synthetic voiced frames into the VAD at real-time pacing while a test tone
            plays through the real output device. Measures the full software path
            (VAD decision -> duck -> confirm -> stop applied in the PortAudio callback).
            EXCLUDES microphone capture latency, which happens upstream of the first frame.
            Runnable unattended.

mic         Real microphone, real human speech. Play starts, a human speaks over it. Measures the
            same metric with real capture in the loop. Requires a person.

The test tone is an audio SOURCE for the gate, not a TTS provider. There is no fallback TTS in this
project (RULES.md R9.4); the tone never carries speech and never enters the spoken response path.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.audio.player import AudioGate          # noqa: E402
from aether.audio.vad import MicVAD                # noqa: E402
from aether.events import EventType                # noqa: E402
from aether.trace import Trace                     # noqa: E402

SR_IN = 16000
FRAME_MS = 20
FRAME_N = SR_IN * FRAME_MS // 1000
MEANINGFUL_SPEECH_MS = 300.0   # same promotion threshold as aether/spike.py


def test_tone(samplerate: int, seconds: float, freq: float = 220.0, amp: float = 0.18) -> np.ndarray:
    t = np.arange(int(samplerate * seconds)) / samplerate
    sig = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * freq * 2 * t)
    sig = sig / np.abs(sig).max() * amp
    return (sig * 32767).astype(np.int16)


def voiced_frame(offset: int, f0: float = 140.0, amp: float = 0.35) -> np.ndarray:
    """A synthetic voiced frame that WebRTC VAD accepts as speech."""
    t = (np.arange(FRAME_N) + offset) / SR_IN
    sig = np.zeros(FRAME_N)
    for k, a in [(1, 1.0), (2, 0.6), (3, 0.45), (4, 0.3), (5, 0.2), (8, 0.12), (12, 0.08)]:
        sig += a * np.sin(2 * np.pi * f0 * k * t)
    sig += 0.05 * np.random.randn(FRAME_N)
    sig = sig / np.abs(sig).max() * amp
    return (sig * 32767).astype(np.int16)


class Rig:
    """Wires MicVAD to AudioGate with the same duck/confirm/stop policy as the spike."""

    def __init__(self, trace: Trace, aggressiveness: int, output_device: int | None):
        self.trace = trace
        self.gate = AudioGate(trace, device=output_device)
        self.mic = MicVAD(trace, aggressiveness=aggressiveness)
        self.stop_issued = False
        self.barge = False
        self.mic.on_onset = self._on_onset
        self.mic.on_voiced_progress = self._on_progress

    def _on_onset(self, onset_t: float) -> None:
        if self.gate.is_playing:
            self.barge = True
            self.stop_issued = False
            self.gate.request_duck(reason="speech_onset")

    def _on_progress(self, voiced_ms: float) -> None:
        if self.barge and not self.stop_issued and voiced_ms >= MEANINGFUL_SPEECH_MS:
            if self.gate.is_playing:
                self.stop_issued = True
                self.gate.request_stop(reason="voiced_duration_confirmed")


def one_synthetic_trial(rig: Rig, trace: Trace) -> dict | None:
    mark = len(trace.events)
    rig.barge = False
    rig.stop_issued = False

    rig.gate.enqueue(test_tone(rig.gate.samplerate, 4.0))
    time.sleep(0.6)  # let the tone actually be playing
    if not rig.gate.is_playing:
        return None

    # Feed silence, then voiced frames, paced in real time like a live mic would.
    period = FRAME_MS / 1000.0
    next_t = time.perf_counter()
    for _ in range(10):
        next_t += period
        rig.mic.process_frame(np.zeros(FRAME_N, dtype=np.int16))
        time.sleep(max(0.0, next_t - time.perf_counter()))

    for i in range(60):   # up to 1.2 s of speech; stop fires long before this
        next_t += period
        rig.mic.process_frame(voiced_frame(i * FRAME_N))
        time.sleep(max(0.0, next_t - time.perf_counter()))
        if rig.stop_issued and any(
            e.type == EventType.AUDIO_STOPPED.value for e in trace.events[mark:]
        ):
            break

    new = trace.events[mark:]
    onset = next((e for e in new if e.type == EventType.SPEECH_ONSET.value), None)
    duck = next((e for e in new if e.type == EventType.AUDIO_DUCKED.value), None)
    stopped = next((e for e in new if e.type == EventType.AUDIO_STOPPED.value), None)
    if not (onset and stopped):
        return None

    rig.gate.stop(reason="trial_teardown")
    # Feed trailing silence so the detector observes speech offset and resets for the next trial.
    # Without this, _speech_active stays latched and no further SpeechOnset is ever emitted.
    for _ in range(rig.mic.offset_frames + 5):
        next_t += period
        rig.mic.process_frame(np.zeros(FRAME_N, dtype=np.int16))
        time.sleep(max(0.0, next_t - time.perf_counter()))
    try:
        while True:
            rig.mic.utterances.get_nowait()
    except Exception:
        pass
    time.sleep(0.2)
    return {
        "duck_latency_ms": round(duck.t - onset.t, 2) if duck else None,
        "audio_kill_latency_ms": round(stopped.t - onset.t, 2),
        "confirm_ms": onset.fields.get("confirm_ms"),
        "stream_output_latency_ms": stopped.fields.get("stream_output_latency_ms"),
    }


def run_synthetic(args) -> int:
    trace = Trace.new_run(args.trace_dir, echo=args.verbose)
    rig = Rig(trace, args.vad_aggressiveness, args.output_device)
    rig.gate.open()
    print(f"output device latency: {rig.gate.stream_output_latency_ms:.2f} ms")
    print(f"VAD aggressiveness   : {args.vad_aggressiveness}")
    print(f"onset confirm window : {rig.mic.onset_frames * FRAME_MS} ms")
    print(f"meaningful threshold : {MEANINGFUL_SPEECH_MS:.0f} ms\n")

    trials = []
    for i in range(args.repeats):
        r = one_synthetic_trial(rig, trace)
        if r is None:
            print(f"trial {i + 1}: FAILED (no onset/stop recorded)")
            continue
        trials.append(r)
        print(
            f"trial {i + 1}: duck={r['duck_latency_ms']} ms  "
            f"audio_kill={r['audio_kill_latency_ms']} ms"
        )

    rig.gate.close()
    trace.close()

    if not trials:
        print("\nNO MEASUREMENT OBTAINED")
        return 1

    kill = [t["audio_kill_latency_ms"] for t in trials]
    ducks = [t["duck_latency_ms"] for t in trials if t["duck_latency_ms"] is not None]
    summary = {
        "mode": "synthetic",
        "n": len(trials),
        "audio_kill_latency_ms": {
            "min": min(kill), "median": round(statistics.median(kill), 2), "max": max(kill),
        },
        "duck_latency_ms": {
            "min": min(ducks), "median": round(statistics.median(ducks), 2), "max": max(ducks),
        } if ducks else None,
        "excludes": "microphone capture latency; OS/driver audio already buffered past the callback",
        "stream_output_latency_ms": trials[0]["stream_output_latency_ms"],
        "trace": str(trace.path),
    }
    print("\n" + json.dumps(summary, indent=2))
    return 0


def run_mic(args) -> int:
    trace = Trace.new_run(args.trace_dir, echo=True)
    rig = Rig(trace, args.vad_aggressiveness, args.output_device)
    rig.gate.open()
    rig.mic.open()
    print(f"mic input latency    : {rig.mic.stream_input_latency_ms:.2f} ms")
    print(f"output device latency: {rig.gate.stream_output_latency_ms:.2f} ms")
    print("\nHEADPHONES RECOMMENDED (no AEC on Day 1).")
    print("A tone will play. Speak over it, normally, for about a second.\n")

    results = []
    for i in range(args.repeats):
        mark = len(trace.events)
        rig.barge = False
        rig.stop_issued = False
        print(f"--- trial {i + 1}/{args.repeats}: tone starting, speak now ---")
        rig.gate.enqueue(test_tone(rig.gate.samplerate, 6.0))
        deadline = time.time() + 12.0
        while time.time() < deadline:
            new = trace.events[mark:]
            onset = next((e for e in new if e.type == EventType.SPEECH_ONSET.value), None)
            stopped = next((e for e in new if e.type == EventType.AUDIO_STOPPED.value), None)
            if onset and stopped:
                lat = round(stopped.t - onset.t, 2)
                duck = next((e for e in new if e.type == EventType.AUDIO_DUCKED.value), None)
                results.append(lat)
                print(f"    audio_kill={lat} ms  duck="
                      f"{round(duck.t - onset.t, 2) if duck else None} ms")
                break
            time.sleep(0.02)
        else:
            print("    no interruption detected in this trial")
        rig.gate.stop(reason="trial_teardown")
        time.sleep(0.7)

    rig.mic.close()
    rig.gate.close()
    trace.close()

    if not results:
        print("\nNO MEASUREMENT OBTAINED")
        return 1
    print("\n" + json.dumps({
        "mode": "mic",
        "n": len(results),
        "audio_kill_latency_ms": {
            "min": min(results),
            "median": round(statistics.median(results), 2),
            "max": max(results),
        },
        "excludes": "OS/driver audio already buffered past the callback",
        "trace": str(trace.path),
    }, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("synthetic", "mic"), default="synthetic")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--vad-aggressiveness", type=int, default=2)
    ap.add_argument("--output-device", type=int, default=None)
    ap.add_argument("--trace-dir", default="traces")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    return run_synthetic(args) if args.mode == "synthetic" else run_mic(args)


if __name__ == "__main__":
    raise SystemExit(main())
