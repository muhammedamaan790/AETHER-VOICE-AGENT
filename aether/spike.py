"""The turn loop: audio in, one answer out, and everything that decides whether it is spoken.

    mic / phone -> VAD -> STT -> classify -> menu router -> hotel tool -> Rime -> AudioGate
                                                         -> LLM       ->

The same class drives the local microphone and a phone call. On a call the two `aether.bridge`
adapters replace the devices, and nothing between them changes.

Interruption is decided in two stages, and they are deliberately different kinds of decision.

**By duration, on the realtime thread** (open-mic only) -- fast, meaning-free, and reversible:

    speech onset               -> duck immediately            (AudioDucked)
    voiced speech continues    -> confirmed meaningful, stop  (AudioStopped)

**By meaning, on the turn thread** -- `aether.classify`, running on the transcript BEFORE a
generation is allocated, because allocation is what fences the previous one:

    BACKCHANNEL / STATUS_QUERY -> no generation is allocated, the answer in flight survives
    CANCEL                     -> fenced, and no successor task starts
    REFINEMENT / REPLACEMENT   -> an ordinary turn, labelled on TaskReplaced
    NEW_TASK                   -> an ordinary turn with nothing displaced

The classifier can only ever WITHHOLD a fence for a phrase it recognises exactly; everything
ambiguous falls through to replacement, which fences. Generation fencing stays the final authority
on what may be heard -- the classifier decides whether a turn begins, never what may be spoken.

Not implemented, and said plainly rather than faked: salvage (`ResultSalvaged`). AETHER has no
partial-result store, so a refinement reuses nothing and reports nothing (RULES.md R8).
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading

from .audio.player import AudioGate
from .audio.rime import RimeNotConfigured
from .audio.rime_ws import SpeakResult, build_tts
from .audio.vad import MicVAD, describe_device
from .classify import classify
from .config import RuntimeConfig
from .conversation import ConversationHistory
from .hotel import MenuStore
from .hotel.router import route as route_menu
from .hotel.tools import HOTEL_TOOLS, render
from .events import EventType
from .interruption import BargeInCoordinator
from .llm import build_llm, supports_streaming
from .tools import ToolRunner
from .stt import WhisperSTT
from .supervisor.generations import GenerationRegistry
from .timing import TurnTiming
from .trace import Trace, now_ms

# Voiced duration that promotes a duck into a full stop. Placeholder until Day 3.
# Only consulted in open-mic mode; push-to-talk never promotes a duck, because it never ducks.
MEANINGFUL_SPEECH_MS = 300.0

# How long an utterance may sit unread before the loop is too late to answer it, measured from the
# moment the caller stopped speaking. Only applied when NEWER speech is already waiting -- see
# `Day1Spike._is_stale_on_intake`.
#
# Measured, not guessed: healthy turns on real phone calls completed end to end in 2798, 4288,
# 4474 and 5922 ms. Six seconds sits above all of them, so normal operation never reaches this and
# only a genuine stall does.
STALE_UTTERANCE_MS = 6000.0

# How the microphone is driven.
#
# Listening and fencing are ORTHOGONAL, and conflating them was a real bug. Push-to-talk closed the
# mic to stop noise from fencing turns, and the side effect was that the user had to press
# INTERRUPT before the agent could hear them at all -- found in live use. The two questions are
# separate: "is the mic open?" and "may voice fence the active generation?"
#
#                          mic open by default?   voice may fence?
#   HANDS_FREE (default)          yes                   no
#   PUSH_TO_TALK                  no                    no
#   OPEN_MIC                      yes                   yes
#
# HANDS_FREE: just speak. The mic is always open, and voice NEVER fences -- INTERRUPT is the only
# thing that stops an active task, which is what makes background noise harmless: a rejected
# utterance never reaches the turn loop, so it cannot fence anything. Interruption by speech still
# works, because `GenerationRegistry.allocate()` fences the previous generation when the next turn
# begins; it simply lands at end-of-utterance rather than 300 ms into it.
#
# Neither realtime callback is wired here, and that is deliberate rather than lazy. Ducking without
# fencing would be a trap: `should_resume_after_short_utterance()` is
# `gate.is_ducked and not _fence_applied`, so a ducked-but-unfenced utterance takes the
# resume-and-return path in `handle_utterance` and is silently DISCARDED. Duck only where a fence
# can follow it.
#
# PUSH_TO_TALK: the mic is closed until the operator interrupts. Still the most noise-proof mode --
# the mic is shut for the whole of the agent's turn -- and the right choice in a very loud room,
# at the cost of a press before every utterance.
#
# OPEN_MIC: the original always-listening barge-in. Voice onset ducks, and sustained voiced audio
# promotes that duck to a fence. The strongest demonstration where acoustics allow it, and the
# only mode in which a BACKCHANNEL produces AudioDucked -> AudioResumed rather than simply
# leaving the answer playing untouched.
#
# All three reach the SAME fence through `BargeInCoordinator.fence_now`, so the generation model,
# the AudioGate and every stale-result guarantee are identical. Only the trigger differs, and the
# trace records which one fired.
PUSH_TO_TALK = "push_to_talk"
OPEN_MIC = "open_mic"
HANDS_FREE = "hands_free"
INPUT_MODES = (HANDS_FREE, PUSH_TO_TALK, OPEN_MIC)


class Day1Spike:
    def __init__(
        self,
        trace: Trace,
        *,
        stt_model: str = "base.en",
        input_device: int | None = None,
        output_device: int | None = None,
        vad_aggressiveness: int = 2,
        vad_onset_frames: int | None = None,
        # NOTE the default is OPEN_MIC while the *entry points* default to PUSH_TO_TALK.
        # That split is deliberate. `python -m aether.spike` and `python -m aether.web` are the
        # product, and the product is push-to-talk. This class is the library underneath, and its
        # historical behaviour is open-mic barge-in -- which is still fully built, tested and
        # measured. Keeping it as the class default means the existing suite keeps testing exactly
        # what it was written to test, and nothing silently changes meaning under it.
        input_mode: str = OPEN_MIC,
    ):
        if input_mode not in INPUT_MODES:
            raise ValueError(f"input_mode must be one of {INPUT_MODES}, got {input_mode!r}")
        self.input_mode = input_mode
        self.trace = trace
        self.gens = GenerationRegistry(trace)
        self.gate = AudioGate(trace, device=output_device)
        mic_kwargs = {} if vad_onset_frames is None else {"onset_frames": vad_onset_frames}
        self.mic = MicVAD(
            trace, aggressiveness=vad_aggressiveness, device=input_device, **mic_kwargs
        )
        self.stt = WhisperSTT(trace, model_size=stt_model)
        self.llm = build_llm()
        # Streaming /ws3 by default, HTTP fallback via RIME_TRANSPORT=http. Still Rime either way.
        self.rime = build_tts(trace, samplerate=self.gate.samplerate)
        # Session-scoped conversation history. Owned by this pipeline instance, never by the
        # provider object, so two sessions can never share or leak context.
        self.history = ConversationHistory()
        # The hotel menu, and the runner that executes its tools under the SAME fencing the audio
        # path uses. Deterministic lookups answer menu questions without the LLM; anything the
        # router is not confident about still goes to Gemini.
        self.menu = MenuStore()
        self.tools = ToolRunner(trace, self.menu, tools=HOTEL_TOOLS)
        # Sentence streaming is additive; set AETHER_LLM_STREAMING=0 to use the
        # settled blocking path with its retry behaviour.
        self._last_stream_metrics: dict = {}
        self._streaming_enabled = (
            os.environ.get('AETHER_LLM_STREAMING', '1').strip() != '0'
        )

        self._turn = 0
        # Utterances the loop was too late to answer. Reported in the call diagnostics, because
        # "the caller spoke and got nothing" must never be invisible.
        self.utterances_dropped_stale = 0
        # Set by run_turns(); cleared by stop_turns()/shutdown(). A plain flag, because the
        # telephony worker sets it from an asyncio teardown while the loop runs on a thread.
        self._running = False
        # Interruption/recovery coordination. Owns *when* a barge-in becomes a fence; the
        # registry and the gate remain authoritative about what fencing means.
        self.barge = BargeInCoordinator(
            trace, self.gens, self.gate, meaningful_speech_ms=MEANINGFUL_SPEECH_MS
        )

        if input_mode == OPEN_MIC:
            self.mic.on_onset = self._on_onset
            self.mic.on_voiced_progress = self._on_voiced_progress
        elif input_mode == PUSH_TO_TALK:
            # Neither callback, and the mic starts closed: nothing is captured until an interrupt.
            self.mic.set_listening(False)
        else:
            # HANDS_FREE: mic stays open (the default), and neither callback is wired, so voice can
            # never fence. See the note above on why ducking without fencing would drop utterances.
            pass

    # --- realtime-safe callbacks (PortAudio input thread) ----------------------------

    def _on_onset(self, onset_t: float) -> None:
        """Duck immediately. No understanding exists at this point -- that is the whole design."""
        self.barge.on_speech_onset()

    def _on_voiced_progress(self, voiced_ms: float) -> None:
        """Promote duck -> fence once the interruption looks meaningful (duration proxy)."""
        self.barge.on_voiced_progress(voiced_ms)

    # --- manual interrupt (push-to-talk) ----------------------------------------------

    def interrupt(self, *, source: str = "manual") -> str | None:
        """Fence whatever is in flight and open the microphone. Returns the fenced generation id.

        The whole point of the push-to-talk design, and deliberately only nine lines: it reaches
        the SAME `fence_now` the voice path reaches, so the generation registry, the AudioGate's
        per-chunk tag and every stale-result guarantee behave identically. A different trigger,
        never a different fence.

        Faster than the voice path by construction, because there is nothing to confirm. Voice
        costs 40 ms of onset plus `MEANINGFUL_SPEECH_MS` (300 ms) of duration before it dares
        fence; a deliberate press is already unambiguous, so the fence is immediate and the only
        remaining cost is the gate flushing its queue.

        Safe to call when nothing is speaking: `fence_now` returns None, which is what lets one
        control mean "stop talking" without having to know whether it is talking.

        It opens the mic ONLY in push-to-talk, where the press is by definition how you get a turn.
        In hands-free and open-mic the mic is already open and its state belongs to the user --
        forcing it open here would silently cancel STANDBY on the next press, which is exactly the
        listening/fencing conflation this control set exists to avoid.

        Thread-safety: called from the web bridge's socket thread and from the terminal listener,
        never from the PortAudio callback. `mark_fenced` and `fence_generation` are plain flag
        writes, and trace emission is deferred to `drain_fenced()` on the main loop.
        """
        fenced = self.barge.fence_now(reason=f"{source}_interrupt")
        if self.input_mode == PUSH_TO_TALK:
            self.mic.set_listening(True)
        return fenced

    # --- standby: stop LISTENING, which is not the same as stopping talking -------------

    def set_listening(self, on: bool) -> bool:
        """Open or close the microphone. Returns whether AETHER is now listening.

        THE LISTENING CONTROL. One toggle, two directions, and deliberately NOT a fence:

            START LISTENING   inbound audio is processed again. No generation changes.
            STOP LISTENING    inbound audio is dropped. The call stays up, an answer already
                              in flight keeps playing, and no generation changes.

        Stopping listening is not a hangup and not an interrupt. On a phone call the LiveKit room,
        the published track and the outbound pump are all untouched -- `InboundBridge.push` checks
        `mic.listening` and drops the frame, which is the only thing that changes. That is what
        makes "stop listening" honest rather than cosmetic: the audio really is not reaching the
        VAD, and the caller really is still connected.

        Delegates to `MicVAD.set_listening`, which resets detector state on the closing edge, so a
        half-captured utterance can never be stitched onto the next start.

        Thread-safe: `MicVAD._enabled` is a `threading.Event`, so this is safe from the websocket
        thread, the terminal listener and an asyncio teardown alike.
        """
        self.mic.set_listening(bool(on))
        return self.mic.listening

    def start_listening(self) -> bool:
        """START LISTENING. Idempotent -- pressing it twice is not an error."""
        return self.set_listening(True)

    def stop_listening(self) -> bool:
        """STOP LISTENING. Fences nothing, hangs up nothing, changes no generation."""
        return self.set_listening(False)

    def toggle_listening(self) -> bool:
        """Flip the one toggle. Returns whether AETHER is now listening."""
        return self.set_listening(not self.mic.listening)

    @property
    def listening(self) -> bool:
        """Read from the microphone, never tracked separately -- one source of truth."""
        return self.mic.listening

    # --- standby: the same control, named from the other end ---------------------------
    #
    # Kept because the terminal listener and the earlier web action speak in these terms. Standby
    # is exactly `not listening`; both names reach the same `MicVAD` flag, so there is no second
    # state to fall out of step.

    def set_standby(self, on: bool) -> bool:
        """Mute or un-mute the microphone. Returns True when standby is engaged.

        Deliberately NOT a fence. Standby answers "stop listening to me"; INTERRUPT answers "stop
        talking". They are independent intentions and conflating them was a real bug: push-to-talk
        closed the mic to stop noise fencing turns, and the side effect was that the user could not
        speak at all without pressing something first.

        Nothing about the active generation changes here -- no `FenceRequested`, no
        `GenerationChanged`, no gate call. A turn already in flight keeps running and keeps
        speaking. This mutes the input, not the output.

        Why it is needed at all: hands-free keeps the mic open, so a conversation with someone else
        in the room is captured, transcribed and answered aloud. Voice can no longer falsely
        *interrupt*, but it can still falsely *respond*, and only the user knows which speech was
        meant for the agent.

        Delegates to `MicVAD.set_listening`, which resets detector state on the closing edge, so a
        half-captured utterance can never be stitched onto the next un-mute.
        """
        self.mic.set_listening(not on)
        return on

    def toggle_standby(self) -> bool:
        """Flip standby. Returns True when muted, which is what the UI and terminal report."""
        return self.set_standby(self.mic.listening)

    @property
    def standby(self) -> bool:
        """True when the microphone is muted. Derived from the mic, never tracked separately."""
        return not self.mic.listening

    def _close_mic_after_capture(self) -> None:
        """Push-to-talk only: the mic is open between the press and the end of the utterance.

        NOT the listening control -- deliberately renamed away from `stop_listening`, which is now
        the user-facing STOP LISTENING and means something else entirely. This is turn plumbing:
        one press, one utterance. In hands-free and open-mic the mic state belongs to the user and
        this does nothing.
        """
        if self.input_mode == PUSH_TO_TALK:
            self.mic.set_listening(False)

    # --- turn handling ----------------------------------------------------------------

    def handle_utterance(self, audio, onset_t: float) -> None:
        # Close the mic the instant the utterance exists, not when the turn finishes. Everything
        # after this -- STT, the model, synthesis, playback -- happens with the microphone shut, so
        # a conversation with a colleague during the agent's answer cannot be captured, and the
        # agent's own output cannot re-trigger it. One press, one utterance.
        self._close_mic_after_capture()

        # Latency marks for this turn. Read the end-of-speech moment off the append-only trace
        # rather than threading a new value out of the VAD callback.
        ended = self.trace.last(EventType.SPEECH_ENDED)
        timing = TurnTiming(speech_ended=ended.t if ended else None)

        self._turn += 1
        for fenced_gen, fence_reason in self.barge.drain_fenced():
            # The reason comes from whoever fenced, never from here. A hardcoded string would have
            # recorded a button press as "meaningful_interruption" and quietly destroyed the one
            # distinction the evidence needs to make.
            self.trace.emit(EventType.FENCE_REQUESTED, gen=fenced_gen, reason=fence_reason)

        # TRANSCRIBE BEFORE ALLOCATING A GENERATION.
        #
        # `begin_turn` allocates, and allocation is what fences the previous generation. Doing that
        # first meant the active answer was destroyed before anybody knew what had been said -- so
        # "mm-hm" during an answer killed it, and so did a noise burst that transcribed to nothing
        # at all. Whether a generation should exist is a question about the words, so the words
        # come first. Nothing is fenced on this path; the coordinator is not touched until the
        # classifier has had its say.
        # "Is there anything to interrupt?" -- and the answer is NOT just `turn_in_flight`. The
        # turn function returns as soon as Rime's audio is enqueued, so for most of a menu answer
        # the flag is already false while the caller is still listening to it. A backchannel said
        # over that audio has to be recognised as one, so the gate is asked as well.
        in_flight = self.barge.turn_in_flight or self.gate.is_playing
        self.mic.set_context(turn_id=self._turn, gen=None)
        text = self.stt.transcribe(audio, turn_id=self._turn, gen=None)
        final = self.trace.last(EventType.TRANSCRIPT_FINAL)
        timing.transcript = final.t if final else None
        if not text:
            # No longer fences the answer in flight -- see above. A duck raised by the onset is
            # released here, because nothing was said that could justify keeping the volume down.
            if self.barge.should_resume_after_short_utterance():
                self.gate.request_resume()
                self.barge.note_resumed()
            print("  (empty transcript, ignoring)")
            return

        # How much the caller actually SAID, measured by the VAD and read off the same event the
        # latency marks come from. Without it a garbled three-second question that Whisper renders
        # as "Okay." looks exactly like a backchannel, and the caller silently loses their turn.
        voiced_ms = ended.fields.get("voiced_ms") if ended else None
        decision = classify(text, in_flight=in_flight, voiced_ms=voiced_ms)
        if self._resolve_without_a_turn(text, decision, in_flight):
            return

        gen = self.barge.begin_turn(turn_id=self._turn)
        try:
            self.mic.set_context(turn_id=self._turn, gen=gen.id)

            self.trace.emit(
                EventType.INTERRUPTION_CLASSIFIED,
                turn_id=self._turn, gen=gen.id,
                interruption_class=decision.cls.value, rule=decision.rule,
                in_flight=in_flight, text=text,
            )
            if in_flight and decision.transition is not None:
                # A task really was displaced. The canonical event carries WHICH transition, so a
                # refinement and an outright replacement stay distinguishable in the evidence even
                # though both fence -- AETHER has nothing to salvage, and says so rather than
                # inventing a salvage count (RULES.md R8).
                self.trace.emit(
                    EventType.TASK_REPLACED,
                    turn_id=self._turn, gen=gen.id,
                    reason=decision.transition.value,
                    interruption_class=decision.cls.value,
                    records_salvaged=0,
                )

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
            # DETERMINISTIC MENU PATH, tried before the model.
            #
            # A menu fact is a lookup, not a question for a language model: measured, the LLM stage
            # costs ~1.8 s that a tool call does not, and a model asked to read back a price can
            # still say the wrong number. The router answers only when confident and returns None
            # otherwise, so anything ambiguous still reaches Gemini.
            self._last_stream_metrics = {}
            spoken = self._menu_answer(text, gen)
            if spoken is not None:
                # No model was consulted, so the LLM stage took no time. Recording it as a zero
                # span rather than leaving it None keeps the turn's arithmetic honest.
                timing.llm_start = timing.llm_end = now_ms()
                outcome = self._speak_answer(spoken, gen, timing)
            else:
                timing.llm_start = now_ms()
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


    def _resolve_without_a_turn(self, text: str, decision, in_flight: bool) -> bool:
        """Handle the classes that must NOT start a turn. Returns True when the utterance is done.

        Two of the six classes are not requests, and treating them as requests is what destroys a
        caller's answer:

        * **BACKCHANNEL** -- "mm-hm", "right", "okay" while AETHER is speaking. Encouragement, not
          a question. It gets no generation, so the answer in flight is never fenced and keeps
          playing. If a duck is outstanding (open-mic only, where voice onset ducks) the volume is
          restored, which is the audio half of "carry on".

        * **STATUS_QUERY** -- "are you still there?" while a turn is running. A question ABOUT the
          task, so the task survives it: no fence, no allocation, no `TaskReplaced`.

          It is deliberately NOT answered aloud. Speaking a status line over an answer already in
          flight would need a second audio path that could bypass the gate's single active
          generation, and inventing one to say "just a moment" is not worth putting a hole in the
          thing the whole design exists to guarantee. The caller hears the answer they were already
          waiting for, which is the truthful response to "are you still there".

        **CANCEL** is handled here too, but is not one of the protected classes -- it is the
        opposite. It fences through the ordinary `fence_now` and then starts nothing, which is the
        one transition that ends a task without a successor.

        Everything else returns False and proceeds into the normal turn path, where generation
        fencing remains the final authority.
        """
        from .events import InterruptionClass

        if decision.cls is InterruptionClass.CANCEL:
            # The same `fence_now` a barge-in and the button reach -- a different trigger, never a
            # different fence. `reason` is what distinguishes a cancellation from a replacement in
            # the trace; without it the evidence could not tell them apart.
            fenced = self.barge.fence_now(reason="cancelled_by_caller")
            for fenced_gen, fence_reason in self.barge.drain_fenced():
                self.trace.emit(EventType.FENCE_REQUESTED, gen=fenced_gen, reason=fence_reason)
            self.trace.emit(
                EventType.INTERRUPTION_CLASSIFIED,
                turn_id=self._turn, gen=fenced,
                interruption_class=decision.cls.value, rule=decision.rule,
                in_flight=in_flight, text=text,
            )
            self.trace.emit(
                EventType.CANCELLATION_RESOLVED,
                turn_id=self._turn, gen=fenced,
                # Honest about the no-op case: "stop" said into silence cancels nothing, and the
                # event says so rather than implying a task was killed.
                cancelled=bool(fenced), had_task_in_flight=in_flight,
                successor_task=None, text=text,
            )
            self.barge.end_turn()
            print(f"  CANCEL: {text!r} -- " + (f"fenced {fenced}" if fenced else "nothing to stop"))
            return True

        if not decision.protects_task:
            return False

        self.trace.emit(
            EventType.INTERRUPTION_CLASSIFIED,
            turn_id=self._turn, gen=self.gens.active.id if self.gens.active else None,
            interruption_class=decision.cls.value, rule=decision.rule,
            in_flight=in_flight, text=text,
        )

        if decision.cls is InterruptionClass.BACKCHANNEL:
            self.trace.emit(
                EventType.BACKCHANNEL_DETECTED,
                turn_id=self._turn, gen=self.gens.active.id if self.gens.active else None,
                text=text, rule=decision.rule,
            )
            if self.gate.is_ducked:
                self.gate.request_resume()
                self.barge.note_resumed()
            print(f"  BACKCHANNEL: {text!r} -- the answer keeps playing")
        else:
            print(f"  STATUS QUERY: {text!r} -- the task is untouched")
        return True

    # --- reply production: two paths, one contract -------------------------------------
    #
    # Both return (reply_text, SpeakResult) or None. None means the turn is over and the path has
    # already emitted its own ResultDiscarded; the caller simply stops. Everything after the call
    # -- the completed/spoken boundary, history, ResponseSpoken -- is shared, so streaming cannot
    # acquire different history or fencing semantics by accident.

    def _menu_answer(self, text: str, gen) -> str | None:
        """Answer from the hotel menu, or None to let the model handle it.

        Routing and execution stay separate: `route_menu` only names a tool and its arguments, and
        `ToolRunner` executes it with the usual fence check, injectable delay and identity
        stamping. The router can never bypass fencing because it never runs anything.

        Returns None on every uncertain path -- no route, a stale result, or a tool that could not
        answer -- so the LLM gets the sentence. A slower answer is better than a confident wrong one.
        """
        decision = route_menu(text)
        if decision is None:
            return None

        result = self.tools.run(
            decision.tool, gen=gen.id, turn_id=self._turn,
            is_valid=lambda: self.barge.is_valid(gen.id),
            **decision.params,
        )
        if not result.may_speak:
            # Fenced mid-lookup. The caller has moved on; `ToolRunner` already recorded the
            # discard, and returning None here would hand a stale question to the LLM.
            print("  (menu lookup fenced mid-flight -- nothing spoken)")
            return None

        spoken = render(result)
        if spoken is None:
            return None
        print(f"  MENU[{decision.tool}] via {decision.reason}: {spoken}")
        return spoken

    def _speak_answer(self, reply: str, gen, timing):
        """Synthesise an already-composed sentence. The tool path's equivalent of the LLM paths.

        Deliberately mirrors the tail of `_blocking_turn`: same fence check before speaking, same
        gate hand-off, same SpeakResult contract, so everything downstream -- the completed/spoken
        boundary, history, `ResponseSpoken` -- is shared rather than duplicated.
        """
        if not self.barge.is_valid(gen.id):
            self.trace.emit(
                EventType.RESULT_DISCARDED, turn_id=self._turn, gen=gen.id,
                active_gen=self.gens.active.id if self.gens.active else None,
                reason="stale_generation_menu", stage="menu",
            )
            return None

        self.gate.set_active_generation(gen.id, turn_id=self._turn)
        speak_start = now_ms()
        try:
            result = self.rime.speak(
                reply, gate=self.gate, gen=gen.id, turn_id=self._turn,
                is_valid=lambda: self.barge.is_valid(gen.id),
            )
        except RimeNotConfigured as exc:
            print(f"  !! CANNOT SPEAK: {exc}")
            return None
        except Exception as exc:
            self.trace.emit(
                EventType.RESULT_DISCARDED, turn_id=self._turn, gen=gen.id,
                reason="tts_error", stage="tts", provider=self.rime.name,
                error=type(exc).__name__,
            )
            return None

        if result.first_audio_ms is not None:
            timing.first_audio = speak_start + result.first_audio_ms
        return reply, result

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


    def _start_interrupt_listener(self) -> None:
        """ENTER on the terminal is the web UI's INTERRUPT button.

        `python -m aether.spike` previously had no interrupt path at all. Combined with a
        push-to-talk default that starts the mic closed, that made the documented CLI entrypoint
        completely deaf: no way to open the mic, and no error to explain why.

        A daemon thread blocking on stdin, rather than a key-capture library: no new dependency,
        the same behaviour on every platform, and it dies with the process. It only ever calls the
        same `interrupt()` the browser button calls, so there is one interrupt path, not two.
        """
        def listen() -> None:
            while True:
                try:
                    line = sys.stdin.readline()
                    if line == "":
                        return                      # stdin closed (piped, or detached)
                except Exception:
                    return

                if line.strip().lower() == "m":
                    # STANDBY, not a fence: mutes the mic and leaves any active turn speaking.
                    muted = self.toggle_standby()
                    print("  >> MIC " + ("STANDBY -- not listening" if muted else "LIVE -- listening"))
                    continue

                fenced = self.interrupt(source="keyboard")
                print("  >> INTERRUPT" + (f" -- fenced {fenced}" if fenced else " -- nothing to stop"))

        threading.Thread(target=listen, name="aether-interrupt", daemon=True).start()

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
        if self.input_mode == PUSH_TO_TALK:
            print("\nPress ENTER to speak. Ctrl-C to quit.\n")
        elif self.input_mode == HANDS_FREE:
            print("\nJust speak -- the mic is open.")
            print("  ENTER    STOP the agent mid-answer (does not mute)")
            print("  m ENTER  MIC standby on/off (does not stop the agent)")
            print("  Ctrl-C   quit\n")
        else:
            print("\nSpeak at any time; your voice interrupts the agent. Ctrl-C to quit.\n")
        self._start_interrupt_listener()
        try:
            self.run_turns()
        except KeyboardInterrupt:
            print("\nstopping...")
        finally:
            self.shutdown()
            print(f"trace: {self.trace.path}")

    def run_turns(self) -> None:
        """Consume utterances and answer them, until `stop_turns()` or an interrupt.

        Extracted from `run()` so the telephony worker can drive the SAME loop without also
        adopting the local path's device lifecycle and console banner. On a call the transport is
        the device: LiveKit owns the audio, the bridges hand frames to this same `MicVAD` and pull
        from this same `AudioGate`, and everything in between is unchanged. A copy of this loop
        living in the telephony package is exactly how the two paths would drift apart.

        Blocking, and meant to run on its own thread. It does not touch asyncio.
        """
        self._running = True
        while self._running:
            try:
                audio, onset_t = self.mic.utterances.get(timeout=0.25)
            except queue.Empty:
                continue
            if self._is_stale_on_intake(audio, onset_t):
                continue
            self.handle_utterance(audio, onset_t)

    def _is_stale_on_intake(self, audio, onset_t: float) -> bool:
        """Drop an utterance that the loop is far too late to answer. Returns True if dropped.

        The turn loop is strictly sequential -- one utterance is transcribed, answered and
        synthesised before the next is looked at -- so anything that blocks it backs the queue up
        behind it. Observed on a real call: one transcription stalled, and three later utterances,
        all loud clear speech, sat unread until the caller hung up. Answering them when the stall
        cleared would have been worse than dropping them: the caller would have got a reply to
        something they said forty seconds earlier, after several unanswered questions.

        BOTH conditions are required, because either alone is too eager:

        * **Stale** -- more than `STALE_UTTERANCE_MS` has passed since the caller stopped speaking.
          Measured on real calls, a healthy turn completes in 2.8-5.9 s end to end, so the bound
          sits clear of normal operation and only a genuine stall reaches it.
        * **Superseded** -- there is newer audio already waiting. Without this, a caller who simply
          asks two questions in quick succession would lose the first, which is a worse failure
          than a late answer.

        This is the same reasoning as generation fencing, applied one stage earlier: a result the
        caller has moved on from must not be spoken, and an utterance the caller has already
        followed up on is exactly that.
        """
        if self.mic.utterances.empty():
            return False
        duration_ms = len(audio) / getattr(self.mic, "samplerate", 16000) * 1000.0
        waited_ms = now_ms() - onset_t - duration_ms
        if waited_ms <= STALE_UTTERANCE_MS:
            return False
        self.utterances_dropped_stale += 1
        print(f"  (dropped an utterance queued {waited_ms / 1000:.1f}s ago -- newer speech is "
              f"waiting; the caller has moved on)")
        return True

    def stop_turns(self) -> None:
        """Ask `run_turns` to return at the next quarter-second. Safe from any thread."""
        self._running = False

    def shutdown(self) -> None:
        """Release everything this pipeline owns. Idempotent, and safe if `run()` never started.

        The telephony worker calls this on hangup; `run()` calls it in its `finally`. Ordering
        matters: the mic first so no new utterance is queued, then Rime's socket, then the gate,
        then the trace -- closing the trace first would lose the teardown events.
        """
        self._running = False
        self.mic.close()
        if hasattr(self.rime, "close"):
            self.rime.close()
        self.gate.close()
        self.trace.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="AETHER Day-1 voice spike")
    ap.add_argument("--stt-model", default="base.en")
    ap.add_argument("--input-device", type=int, default=None)
    ap.add_argument("--output-device", type=int, default=None)
    # Default None, not 2, so "was a value actually given?" stays answerable. `RuntimeConfig`
    # already parses AETHER_VAD_AGGRESSIVENESS and AETHER_VAD_ONSET_MS from the environment, but
    # nothing here ever read them -- .env.example documented two knobs that silently did nothing.
    ap.add_argument("--vad-aggressiveness", type=int, default=None,
                    help="0-3, higher rejects more non-speech (default: AETHER_VAD_AGGRESSIVENESS, else 2)")
    ap.add_argument("--input-mode", choices=INPUT_MODES, default=HANDS_FREE,
                    help="hands_free (default): just speak; ENTER stops the agent mid-answer. "
                         "push_to_talk: mic closed until you press ENTER. "
                         "open_mic: always listening, and voice itself interrupts.")
    ap.add_argument("--vad-onset-ms", type=int, default=None,
                    help="voiced audio needed to confirm an onset (default: AETHER_VAD_ONSET_MS, else 40)")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    cfg = RuntimeConfig.from_env()
    # Precedence, matching `resolve_input_device`: explicit flag, then environment, then default.
    aggressiveness = (
        args.vad_aggressiveness
        if args.vad_aggressiveness is not None
        else (cfg.vad_aggressiveness if cfg.vad_aggressiveness is not None else 2)
    )
    onset_ms = args.vad_onset_ms if args.vad_onset_ms is not None else cfg.vad_onset_ms

    trace = Trace.new_run(cfg.trace_dir, echo=True)
    kwargs = {}
    if onset_ms is not None:
        # MicVAD counts frames, not milliseconds. At least one frame, or onset can never confirm.
        kwargs["vad_onset_frames"] = max(1, round(onset_ms / 20))
    Day1Spike(
        trace,
        stt_model=args.stt_model,
        input_device=args.input_device,
        output_device=args.output_device,
        vad_aggressiveness=aggressiveness,
        input_mode=args.input_mode,
        **kwargs,
    ).run()


if __name__ == "__main__":
    main()
