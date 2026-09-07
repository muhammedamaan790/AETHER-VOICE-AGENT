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
import os
import queue

from .audio.player import AudioGate
from .audio.rime import RimeNotConfigured
from .audio.rime_ws import SpeakResult, build_tts
from .audio.vad import MicVAD, describe_device
from .config import RuntimeConfig
from .conversation import ConversationHistory
from .events import EventType
from .interruption import BargeInCoordinator
from .llm import build_llm, supports_streaming
from .stt import WhisperSTT
from .supervisor.generations import GenerationRegistry
from .timing import TurnTiming
from .trace import Trace, now_ms

# Voiced duration that promotes a duck into a full stop. Placeholder until Day 3.
MEANINGFUL_SPEECH_MS = 300.0


class Day1Spike:
    def __init__(
        self,
        trace: Trace,
        *,
        stt_model: str = "base.en",
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
        # Streaming /ws3 by default, HTTP fallback via RIME_TRANSPORT=http. Still Rime either way.
        self.rime = build_tts(trace, samplerate=self.gate.samplerate)
        # Session-scoped conversation history. Owned by this pipeline instance, never by the
        # provider object, so two sessions can never share or leak context.
        self.history = ConversationHistory()
        # Sentence streaming is additive; set AETHER_LLM_STREAMING=0 to use the
        # settled blocking path with its retry behaviour.
        self._last_stream_metrics: dict = {}
        self._streaming_enabled = (
            os.environ.get('AETHER_LLM_STREAMING', '1').strip() != '0'
        )

        self._turn = 0
        # Interruption/recovery coordination. Owns *when* a barge-in becomes a fence; the
        # registry and the gate remain authoritative about what fencing means.
        self.barge = BargeInCoordinator(
            trace, self.gens, self.gate, meaningful_speech_ms=MEANINGFUL_SPEECH_MS
        )

        self.mic.on_onset = self._on_onset
        self.mic.on_voiced_progress = self._on_voiced_progress

    # --- realtime-safe callbacks (PortAudio input thread) ----------------------------

    def _on_onset(self, onset_t: float) -> None:
        """Duck immediately. No understanding exists at this point -- that is the whole design."""
        self.barge.on_speech_onset()

    def _on_voiced_progress(self, voiced_ms: float) -> None:
        """Promote duck -> fence once the interruption looks meaningful (duration proxy)."""
        self.barge.on_voiced_progress(voiced_ms)

    # --- turn handling ----------------------------------------------------------------

    def handle_utterance(self, audio, onset_t: float) -> None:
        # A duck that never became a stop: the agent is still talking, so put the volume back.
        # Audio-control decision only -- no semantic claim is made about the utterance.
        if self.barge.should_resume_after_short_utterance():
            self.gate.request_resume()
            self.barge.note_resumed()
            print("  (short utterance during playback -> resumed; no semantic judgement on Day 1)")
            return

        # Latency marks for this turn. Read the end-of-speech moment off the append-only trace
        # rather than threading a new value out of the VAD callback.
        ended = self.trace.last(EventType.SPEECH_ENDED)
        timing = TurnTiming(speech_ended=ended.t if ended else None)

        self._turn += 1
        for fenced_gen in self.barge.drain_fenced():
            self.trace.emit(
                EventType.FENCE_REQUESTED, gen=fenced_gen, reason="meaningful_interruption"
            )
        gen = self.barge.begin_turn(turn_id=self._turn)
        try:
            self.mic.set_context(turn_id=self._turn, gen=gen.id)

            text = self.stt.transcribe(audio, turn_id=self._turn, gen=gen.id)
            final = self.trace.last(EventType.TRANSCRIPT_FINAL)
            timing.transcript = final.t if final else None
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

            # A provider can fail outright (503, timeout, auth). One bad turn must not end the
            # session: without this, the exception unwinds past the run loop, which only catches
            # KeyboardInterrupt, and the voice agent dies.
            timing.llm_start = now_ms()
            self._last_stream_metrics = {}
            if self._streaming_enabled and supports_streaming(self.llm):
                outcome = self._streaming_turn(text, gen, timing)
            else:
                outcome = self._blocking_turn(text, gen, timing)
            if outcome is None:
                return            # the chosen path already emitted its ResultDiscarded
            reply, result = outcome

            # No audio reached the speaker: fenced mid-stream, refused by the gate, or a transport
            # failure. Either way nothing was heard, so nothing is spoken and nothing is remembered.
            if not result.accepted:
                self.trace.emit(
                    EventType.RESULT_DISCARDED,
                    turn_id=self._turn,
                    gen=gen.id,
                    active_gen=self.gens.active.id if self.gens.active else None,
                    reason=result.reason or "tts_no_audio",
                    stage="tts",
                    provider=self.rime.name,
                    transport=self.rime.transport,
                )
                print(f"  (nothing spoken -- {result.reason or 'no audio from Rime'})")
                return

            # Interrupted mid-utterance: some audio WAS heard, but the turn never finished. It is
            # not a completed turn, so it must not be spoken-of or remembered -- committing it
            # would tell the next turn the user heard a whole answer they were cut off from.
            # Level 1 remembers a turn whole or not at all; recording the partially heard prefix
            # is the Level-2 upgrade MEMORY.md deliberately defers.
            if not self.barge.is_valid(gen.id):
                self.trace.emit(
                    EventType.RESULT_DISCARDED,
                    turn_id=self._turn,
                    gen=gen.id,
                    active_gen=self.gens.active.id if self.gens.active else None,
                    reason=result.reason or "fenced_midstream",
                    stage="tts",
                    provider=self.rime.name,
                    transport=self.rime.transport,
                    partial_samples=result.samples,
                )
                print("  (interrupted mid-answer -- not committed as a completed turn)")
                return

            # THE COMPLETED/SPOKEN BOUNDARY. The gate has accepted the audio, so this turn really
            # happened: only now may it become conversational context. Every fenced path above
            # returned before reaching this line, which is precisely why a fenced generation cannot
            # pollute history -- the existing fencing stays the authority and history just rides
            # behind it. Level 1: a turn is remembered whole or not at all (MEMORY.md section 4).
            self.history.commit_turn(text, reply)

            timing.spoken = now_ms()
            print(f"  latency: {timing.summary()}")

            # Token usage for the turn, when the provider reports it. Existing event, existing
            # vocabulary -- this is the evidence needed to confirm whether thinking tokens drive
            # llm_ms before any generation config is touched.
            usage = getattr(self.llm, "last_usage", None) or {}

            self.trace.emit(
                EventType.RESPONSE_SPOKEN,
                turn_id=self._turn,
                gen=gen.id,
                **timing.fields(),
                **self._last_stream_metrics,
                llm_provider=self.llm.name,
                llm_prompt_tokens=usage.get("prompt_tokens"),
                llm_thoughts_tokens=usage.get("thoughts_tokens"),
                llm_output_tokens=usage.get("output_tokens"),
                provider=self.rime.name,
                transport=self.rime.transport,
                text=reply,
                tts_latency_ms=self.rime.last_latency_ms,
                first_audio_ms=result.first_audio_ms,
                completed=result.completed,
                model=self.rime.config.model,
                voice=self.rime.config.voice,
                audio_ms=round(result.samples / self.gate.samplerate * 1000.0, 1),
            )
        finally:
            # Runs on every path: completed, discarded, fenced or failed.
            self.barge.end_turn()


    # --- reply production: two paths, one contract -------------------------------------
    #
    # Both return (reply_text, SpeakResult) or None. None means the turn is over and the path has
    # already emitted its own ResultDiscarded; the caller simply stops. Everything after the call
    # -- the completed/spoken boundary, history, ResponseSpoken -- is shared, so streaming cannot
    # acquire different history or fencing semantics by accident.

    def _blocking_turn(self, text, gen, timing):
        """The settled path: one request, retried, then synthesise the whole reply."""
        try:
            reply = self.llm.respond(text, self.history.messages())
        except Exception as exc:
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=self._turn,
                gen=gen.id,
                reason="llm_error",
                stage="llm",
                provider=self.llm.name,
                error=type(exc).__name__,
            )
            # Concise diagnostic only. Raw provider text never becomes speech.
            print(f"  !! LLM call failed ({type(exc).__name__}); skipping this turn")
            return None
        finally:
            timing.llm_end = now_ms()
        print(f"  LLM({self.llm.name}): {reply}")

        # THE FENCE CHECK. An LLM call takes real time, and the user can interrupt during it.
        # If this generation was fenced while the model was thinking, its answer is stale: the
        # user has already moved on. Discard it here, before it can reach Rime -- no synthesis,
        # no audio, no ResponseSpoken (RULES.md R1). Cancellation is not relied upon: the
        # provider is allowed to answer anyway, and this check still catches it.
        if not self.barge.is_valid(gen.id):
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=self._turn,
                gen=gen.id,
                active_gen=self.gens.active.id if self.gens.active else None,
                reason="stale_generation_llm",
                stage="llm",
            )
            print("  (LLM answer discarded -- generation was fenced while the model was thinking)")
            return None

        # An LLM can legitimately return NOTHING. A thinking model whose whole output budget was
        # consumed by internal reasoning finishes with no text parts at all, and the adapters
        # normalise that to "". Empty text is not a spoken turn: Rime rejects it with a 400, which
        # used to propagate out of the run loop and kill the session.
        #
        # The safe failure is silence: skip the turn, stay alive, and do not invent an answer the
        # model never produced. Nothing is synthesised and nothing enters history, so the next turn
        # is unaffected.
        if not reply.strip():
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=self._turn,
                gen=gen.id,
                reason="empty_llm_response",
                stage="llm",
                provider=self.llm.name,
            )
            print(f"  !! {self.llm.name} returned an empty response; nothing to speak")
            return None

        # Streaming: the gate must know which generation may play BEFORE the first chunk
        # arrives, otherwise enqueue would refuse it. The fence check above still gates entry.
        self.gate.set_active_generation(gen.id, turn_id=self._turn)

        speak_start = now_ms()
        try:
            result = self.rime.speak(
                reply,
                gate=self.gate,
                gen=gen.id,
                turn_id=self._turn,
                # Checked before every chunk, so an interruption stops the stream mid-utterance.
                is_valid=lambda: self.barge.is_valid(gen.id),
            )
        except RimeNotConfigured as exc:
            print(f"  !! CANNOT SPEAK: {exc}")
            print("  !! No fallback TTS exists by design (RULES.md R9.4).")
            return None
        except Exception as exc:
            # Both transports already convert failures into a SpeakResult, so this is defence in
            # depth -- it keeps the guarantee that no TTS failure can end the voice session.
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=self._turn,
                gen=gen.id,
                reason="tts_error",
                stage="tts",
                provider=self.rime.name,
                error=type(exc).__name__,
            )
            print(f"  !! Rime synthesis failed ({type(exc).__name__}); skipping this turn")
            return None

        # `first_audio_ms` is measured inside the client from when it began the request, so
        # anchoring it to speak_start gives the absolute instant sound first existed.
        if result.first_audio_ms is not None:
            timing.first_audio = speak_start + result.first_audio_ms
        return reply, result

    def _collect_stream_metrics(self, first_sentence_ms: float | None) -> dict:
        """Flatten the provider's stream measurements into trace fields.

        Uses the existing `ResponseSpoken` event -- no new event type. Absent measurements stay
        None rather than being guessed, so a provider that reports nothing is visibly silent.
        """
        raw = getattr(self.llm, "last_stream_timing", None) or {}
        return {
            "llm_request_sent_ms": raw.get("request_sent_ms"),
            "llm_ttft_ms": raw.get("ttft_ms"),
            "llm_total_ms": raw.get("total_ms"),
            "llm_first_sentence_ms": first_sentence_ms,
            "llm_raw_chunks": raw.get("raw_chunks"),
            "llm_transport_reused": raw.get("transport_reused"),
            "llm_client_init_ms": raw.get("client_init_ms"),
            "llm_requests_made": raw.get("requests_made"),
        }

    def _streaming_turn(self, text, gen, timing):
        """Speak each sentence as it is produced, instead of waiting for the whole reply.

        The fence is re-checked before every sentence, so an interruption stops the turn at the
        next linguistic boundary rather than after the model finishes. Text produced after the
        fence is dropped with the accumulator and never reaches Rime.
        """
        sentences: list[str] = []
        spoken_samples = 0
        accumulator_dropped = False
        # Time to the first *assembled sentence*, distinct from the provider's raw TTFT. The gap
        # between them is the accumulator holding text back for a boundary; separating the two is
        # the point of this instrumentation.
        first_sentence_ms: float | None = None
        # Rime's own send -> first-chunk DURATION for the first sentence. Kept separately from
        # `timing.first_audio`, which is an absolute mark: `ResponseSpoken.first_audio_ms` is a
        # duration on the blocking path, so it must be a duration here too or the same field means
        # two different things depending on which path ran.
        first_audio_duration_ms: float | None = None
        stream_start = now_ms()
        self.gate.set_active_generation(gen.id, turn_id=self._turn)

        try:
            for sentence in self.llm.respond_stream(text, self.history.messages()):
                if timing.llm_end is None:
                    timing.llm_end = now_ms()      # first usable sentence == thinking is over
                    first_sentence_ms = round(timing.llm_end - stream_start, 3)

                # THE FENCE CHECK, per sentence. A stale sentence must never be synthesised.
                if not self.barge.is_valid(gen.id):
                    accumulator_dropped = True
                    break

                sentences.append(sentence)
                speak_start = now_ms()
                result = self.rime.speak(
                    sentence,
                    gate=self.gate,
                    gen=gen.id,
                    turn_id=self._turn,
                    is_valid=lambda: self.barge.is_valid(gen.id),
                )
                spoken_samples += result.samples
                if timing.first_audio is None and result.first_audio_ms is not None:
                    # The first sentence is when sound first existed for this turn.
                    timing.first_audio = speak_start + result.first_audio_ms
                    first_audio_duration_ms = result.first_audio_ms
                if not result.completed:
                    accumulator_dropped = True
                    break
        except RimeNotConfigured as exc:
            print(f"  !! CANNOT SPEAK: {exc}")
            print("  !! No fallback TTS exists by design (RULES.md R9.4).")
            return None
        except Exception as exc:
            self.trace.emit(
                EventType.RESULT_DISCARDED, turn_id=self._turn, gen=gen.id,
                reason="llm_stream_error", stage="llm",
                provider=self.llm.name, error=type(exc).__name__,
            )
            print(f"  !! streamed reply failed ({type(exc).__name__}); skipping this turn")
            return None
        finally:
            if timing.llm_end is None:
                timing.llm_end = now_ms()

        reply = " ".join(sentences).strip()

        if accumulator_dropped or not self.barge.is_valid(gen.id):
            # Buffered and partially spoken text is discarded outright: Level 1 remembers a turn
            # whole or not at all, so a half-spoken answer is not a turn.
            self.trace.emit(
                EventType.RESULT_DISCARDED, turn_id=self._turn, gen=gen.id,
                active_gen=self.gens.active.id if self.gens.active else None,
                reason="stale_generation_stream", stage="llm",
                provider=self.llm.name, sentences_spoken=len(sentences),
            )
            self._last_stream_metrics = self._collect_stream_metrics(first_sentence_ms)
            print("  (streamed reply discarded -- generation was fenced mid-stream)")
            return None

        if not reply:
            self.trace.emit(
                EventType.RESULT_DISCARDED, turn_id=self._turn, gen=gen.id,
                reason="empty_llm_response", stage="llm", provider=self.llm.name,
            )
            print(f"  !! {self.llm.name} streamed no usable text; nothing to speak")
            return None

        self._last_stream_metrics = self._collect_stream_metrics(first_sentence_ms)
        print(f"  LLM({self.llm.name}, streamed {len(sentences)}): {reply}")
        return reply, SpeakResult(
            accepted=spoken_samples > 0, completed=True,
            samples=spoken_samples, first_audio_ms=first_audio_duration_ms,
        )


    # --- run loop ----------------------------------------------------------------------

    def run(self) -> None:
        self.gate.open()
        self.mic.open()
        # The RESOLVED endpoint, not the env var: on this machine the default input is a headset
        # mic that captures near-silence, so "which mic am I actually on" is the first question.
        print(f"input device       : {describe_device(self.mic.device)}"
              f"  (override with AETHER_INPUT_DEVICE)")
        print(f"mic input latency  : {self.mic.stream_input_latency_ms:.1f} ms")
        print(f"gate output latency: {self.gate.stream_output_latency_ms:.1f} ms")
        # Which endpoint is actually being fed matters: "I can't hear it" is usually the agent
        # playing correctly into a device nobody is listening to. Override with AETHER_OUTPUT_DEVICE.
        try:
            import sounddevice as _sd
            dev = self.gate.device
            name = _sd.query_devices(dev if dev is not None else _sd.default.device[1])["name"]
            host = _sd.query_hostapis()[
                _sd.query_devices(dev if dev is not None else _sd.default.device[1])["hostapi"]
            ]["name"]
            print(f"output device      : [{dev}] {name} ({host}) @ {self.gate.samplerate} Hz")
        except Exception:
            print(f"output device      : [{self.gate.device}] @ {self.gate.samplerate} Hz")
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
            if hasattr(self.rime, "close"):
                self.rime.close()
            self.gate.close()
            self.trace.close()
            print(f"trace: {self.trace.path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="AETHER Day-1 voice spike")
    ap.add_argument("--stt-model", default="base.en")
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
