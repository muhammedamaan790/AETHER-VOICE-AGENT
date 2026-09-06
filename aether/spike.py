"""Day-1 vertical slice: mic -> VAD -> STT -> LLM -> Rime -> audio, plus the barge-in audio path.

Scope (PHASES.md Day 1). This deliberately does NOT contain:
  - the six-class classifier
  - refinement / replacement / salvage / cancel supervisor behaviour
  - fencing or Output Gate validity checks
  - warehouse tools, evaluator, dashboard

Barge-in on Day 1 is decided by *duration*, not meaning:

    speech onset               -> duck immediately            (AudioDucked)
    voiced speech continues    -> confirmed meaningful, stop  (AudioStopped)
    voiced speech stops early  -> resume                      (AudioResumed)

The duration test is an explicit placeholder for Day 3's classifier. It is not backchannel
detection: no BackchannelDetected event is emitted here, because that event means a *semantic*
judgement and Day 1 makes none.
"""

from __future__ import annotations

import argparse
import queue

from .audio.player import AudioGate
from .audio.rime import RimeNotConfigured, RimeTTS
from .audio.vad import MicVAD
from .config import RuntimeConfig
from .events import EventType
from .llm import build_llm
from .stt import WhisperSTT
from .supervisor.generations import GenerationRegistry
from .trace import Trace

# Voiced duration that promotes a duck into a full stop. Placeholder until Day 3.
MEANINGFUL_SPEECH_MS = 300.0


class Day1Spike:
    def __init__(
        self,
        trace: Trace,
        *,
        stt_model: str = "tiny.en",
        input_device: int | None = None,
        output_device: int | None = None,
        vad_aggressiveness: int = 2,
    ):
        self.trace = trace
        self.gens = GenerationRegistry(trace)
        self.gate = AudioGate(trace, device=output_device)
        self.mic = MicVAD(trace, aggressiveness=vad_aggressiveness, device=input_device)
        self.stt = WhisperSTT(trace, model_size=stt_model)
        self.llm = build_llm()
        self.rime = RimeTTS(trace)

        self._turn = 0
        self._barge_active = False
        self._stop_issued = False
        self._fenced_gen: str | None = None

        self.mic.on_onset = self._on_onset
        self.mic.on_voiced_progress = self._on_voiced_progress

    # --- realtime-safe callbacks (PortAudio input thread) ----------------------------

    def _on_onset(self, onset_t: float) -> None:
        """Duck immediately. No understanding exists at this point -- that is the whole design."""
        if self.gate.is_playing:
            self._barge_active = True
            self._stop_issued = False
            self.gate.request_duck(reason="speech_onset")

    def _on_voiced_progress(self, voiced_ms: float) -> None:
        """Promote duck -> full stop once the interruption looks meaningful (duration proxy)."""
        if self._barge_active and not self._stop_issued and voiced_ms >= MEANINGFUL_SPEECH_MS:
            if self.gate.is_playing:
                self._stop_issued = True
                # Fence, don't merely stop: revoking the generation is what prevents audio that is
                # still being synthesised for it from being played when it arrives. A bare stop
                # would flush the queue and then happily accept the next stale chunk.
                self._fenced_gen = self.gens.active.id if self.gens.active else None
                # Fence the generation itself, not just the audio. The LLM for this generation
                # may still be in flight; marking it fenced here is what makes its late answer
                # recognisably stale when it finally returns. Realtime-safe: no trace IO.
                self.gens.mark_fenced(self._fenced_gen)
                self.gate.fence_generation(
                    self._fenced_gen, reason="voiced_duration_confirmed"
                )

    # --- turn handling ----------------------------------------------------------------

    def handle_utterance(self, audio, onset_t: float) -> None:
        # A duck that never became a stop: the agent is still talking, so put the volume back.
        # Audio-control decision only -- no semantic claim is made about the utterance.
        if self.gate.is_ducked and not self._stop_issued:
            self.gate.request_resume()
            self._barge_active = False
            print("  (short utterance during playback -> resumed; no semantic judgement on Day 1)")
            return

        self._barge_active = False
        self._turn += 1
        if self._fenced_gen is not None:
            self.trace.emit(
                EventType.FENCE_REQUESTED, gen=self._fenced_gen, reason="meaningful_interruption"
            )
            self._fenced_gen = None
        gen = self.gens.allocate(turn_id=self._turn)
        self.mic.set_context(turn_id=self._turn, gen=gen.id)

        text = self.stt.transcribe(audio, turn_id=self._turn, gen=gen.id)
        if not text:
            print("  (empty transcript, ignoring)")
            return

        self.trace.emit(
            EventType.TASK_STARTED,
            turn_id=self._turn,
            gen=gen.id,
            task="respond",
            params={"utterance": text},
        )

        reply = self.llm.respond(text)
        print(f"  LLM({self.llm.name}): {reply}")

        # THE FENCE CHECK. An LLM call takes real time, and the user can interrupt during it.
        # If this generation was fenced while the model was thinking, its answer is stale: the
        # user has already moved on. Discard it here, before it can reach Rime -- no synthesis,
        # no audio, no ResponseSpoken (RULES.md R1). Cancellation is not relied upon: the
        # provider is allowed to answer anyway, and this check still catches it.
        if not self.gens.is_active(gen.id):
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=self._turn,
                gen=gen.id,
                active_gen=self.gens.active.id if self.gens.active else None,
                reason="stale_generation_llm",
                stage="llm",
            )
            print("  (LLM answer discarded -- generation was fenced while the model was thinking)")
            return

        try:
            pcm = self.rime.synthesize(
                reply,
                target_samplerate=self.gate.samplerate,
                turn_id=self._turn,
                gen=gen.id,
            )
        except RimeNotConfigured as exc:
            print(f"  !! CANNOT SPEAK: {exc}")
            print("  !! No fallback TTS exists by design (RULES.md R9.4).")
            return

        # Synthesis takes real time too, so the user can interrupt during it as well. Re-check
        # before touching the gate: set_active_generation would otherwise re-activate a
        # generation that has just been fenced, and the audio would play.
        if not self.gens.is_active(gen.id):
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=self._turn,
                gen=gen.id,
                active_gen=self.gens.active.id if self.gens.active else None,
                reason="stale_generation_tts",
                stage="tts",
            )
            print("  (audio discarded -- generation was fenced during synthesis)")
            return

        # Committing audio to the speaker is what ResponseSpoken means, and the gate is the
        # check that decides whether committing is even allowed. If this generation was fenced
        # while its audio was being synthesised, enqueue refuses it and records ResultDiscarded --
        # so ResponseSpoken must not be emitted. Nothing was spoken.
        self._stop_issued = False
        self.gate.set_active_generation(gen.id, turn_id=self._turn)
        if not self.gate.enqueue(pcm, turn_id=self._turn, gen=gen.id):
            print("  (audio discarded as stale -- generation was fenced during synthesis)")
            return
        self.trace.emit(
            EventType.RESPONSE_SPOKEN,
            turn_id=self._turn,
            gen=gen.id,
            provider=self.rime.name,
            text=reply,
            tts_latency_ms=self.rime.last_latency_ms,
            model=self.rime.config.model,
            voice=self.rime.config.voice,
            audio_ms=round(len(pcm) / self.gate.samplerate * 1000.0, 1),
        )

    # --- run loop ----------------------------------------------------------------------

    def run(self) -> None:
        self.gate.open()
        self.mic.open()
        print(f"mic input latency  : {self.mic.stream_input_latency_ms:.1f} ms")
        print(f"gate output latency: {self.gate.stream_output_latency_ms:.1f} ms")
        print(f"LLM provider       : {self.llm.name}")
        print(f"Rime configured    : {self.rime.configured}")
        if not self.rime.configured:
            print(f"  missing: {', '.join(self.rime.missing_config())}")
        print("\nSpeak. Ctrl-C to stop.\n")
        try:
            while True:
                try:
                    audio, onset_t = self.mic.utterances.get(timeout=0.25)
                except queue.Empty:
                    continue
                self.handle_utterance(audio, onset_t)
        except KeyboardInterrupt:
            print("\nstopping...")
        finally:
            self.mic.close()
            self.gate.close()
            self.trace.close()
            print(f"trace: {self.trace.path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="AETHER Day-1 voice spike")
    ap.add_argument("--stt-model", default="tiny.en")
    ap.add_argument("--input-device", type=int, default=None)
    ap.add_argument("--output-device", type=int, default=None)
    ap.add_argument("--vad-aggressiveness", type=int, default=2)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    cfg = RuntimeConfig.from_env()
    trace = Trace.new_run(cfg.trace_dir, echo=True)
    Day1Spike(
        trace,
        stt_model=args.stt_model,
        input_device=args.input_device,
        output_device=args.output_device,
        vad_aggressiveness=args.vad_aggressiveness,
    ).run()


if __name__ == "__main__":
    main()
