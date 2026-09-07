"""Interruption and recovery coordination.

This subdomain owns **when** an interruption is acted on. It owns nothing about **what** fencing
means: `GenerationRegistry` still decides which generation is valid, `AudioGate` still decides what
may be heard, and `RimeStreamingTTS` still sends the verified `{"operation": "clear"}`. Those three
remain authoritative and are called, never reimplemented.

The invariant it exists to uphold:

    No output belonging to an old generation may leak into a newer generation.

Before this module the same logic lived as ~12 loose statements across `Day1Spike` -- two realtime
callbacks, four flags and three fence checks -- with no single place to read it or test it. Nothing
here is new behaviour; it is the same coordination, named.

Two deliberate non-features:

* **No generation counter.** `begin_turn()` delegates to the existing `GenerationRegistry`. A
  second counter would be a second source of truth about which generation is current, which is
  precisely the bug class this subdomain exists to prevent.
* **No second state machine.** `Phase` is *derived* from state that already exists -- the registry,
  the gate, and whether a turn is in flight. It is a read-only view for observability, never
  something the pipeline branches on. If it disagreed with the registry, the registry is right.
"""

from __future__ import annotations

from enum import Enum

from ..audio.player import AudioGate
from ..supervisor.generations import Generation, GenerationRegistry
from ..trace import Trace


class Phase(str, Enum):
    """Where the agent is in a turn. Derived, never authoritative."""

    LISTENING = "listening"        # idle, nothing in flight
    THINKING = "thinking"          # a turn is in flight, nothing audible yet
    SPEAKING = "speaking"          # a turn is in flight and audio is playing
    INTERRUPTED = "interrupted"    # barge-in confirmed, the turn is being torn down
    RECOVERING = "recovering"      # fenced, awaiting the replacement turn


class BargeInCoordinator:
    """Decides when a barge-in becomes a fence, and applies it through the existing primitives.

    Threading: `on_speech_onset` and `on_voiced_progress` run on the PortAudio input thread and
    must stay realtime-safe -- plain flag writes and the two primitives that are themselves
    realtime-safe (`mark_fenced` does no IO; `fence_generation` only sets flags). Trace emission is
    deferred to `drain_fenced()`, which the main loop calls.
    """

    def __init__(
        self,
        trace: Trace,
        gens: GenerationRegistry,
        gate: AudioGate,
        meaningful_speech_ms: float = 300.0,
    ):
        self.trace = trace
        self.gens = gens
        self.gate = gate
        # Duration proxy for "this interruption means something". A placeholder for the Day-3
        # classifier, unchanged from the pipeline's previous behaviour.
        self.meaningful_speech_ms = meaningful_speech_ms

        self._armed = False            # a barge-in is in progress
        self._fence_applied = False    # ...and has already been promoted to a fence
        self._turn_in_flight = False
        # (generation, reason) awaiting FenceRequested emission. The reason travels with the
        # id because the pipeline cannot reconstruct it later: a fence from sustained voice
        # and a fence from a deliberate button press are different facts, and the evidence
        # must not conflate them.
        self._fenced: list[tuple[str | None, str]] = []

    # --- realtime-safe: called from the input callback --------------------------------

    def on_speech_onset(self) -> None:
        """The user started talking. Duck if there is anything to duck; arm either way.

        Arming is deliberately wider than ducking. The agent can be mid-turn with nothing audible
        yet -- the LLM is still thinking -- and interrupting *then* is the case that matters most:
        the user changed their mind before hearing a word. Gating arming on playback meant that
        interruption was silently ignored and the abandoned answer was still spoken.
        """
        if self.gate.is_playing or self._turn_in_flight:
            self._armed = True
            self._fence_applied = False
        if self.gate.is_playing:
            self.gate.request_duck(reason="speech_onset")

    def on_voiced_progress(self, voiced_ms: float) -> None:
        """Promote a duck into a fence once the interruption looks meaningful."""
        if not self._armed or self._fence_applied:
            return
        if voiced_ms < self.meaningful_speech_ms:
            return
        self._fence_applied = True
        self.fence_now(reason="voiced_duration_confirmed")

    def fence_now(self, *, reason: str) -> str | None:
        """Fence the active generation through the existing primitives. Returns its id.

        Order matters and is the whole point: the registry is revoked first, so any work still in
        flight for that generation is already recognisable as stale by the time the gate is told to
        stop. Rime is stopped separately, by the `is_valid` callback the streaming client polls --
        this coordinator never speaks the Rime protocol itself.
        """
        fenced = self.gens.active.id if self.gens.active else None
        # Set here, not only on the voiced-progress path: a fence applied by any route must be
        # reflected in `phase`, or the derived view would contradict the registry.
        self._fence_applied = True
        self._fenced.append((fenced, reason))
        self.gens.mark_fenced(fenced)          # no trace IO: realtime-safe
        self.gate.fence_generation(fenced, reason=reason)
        return fenced

    # --- main thread ------------------------------------------------------------------

    def begin_turn(self, *, turn_id: int) -> Generation:
        """Start a turn. Delegates allocation -- this subdomain owns no counter.

        Both barge-in flags are cleared: a new turn has not been interrupted, and carrying
        `_fence_applied` across would make `phase` report INTERRUPTED for a healthy turn and
        suppress the duck-then-resume path for the next short utterance.
        """
        self._armed = False
        self._fence_applied = False
        gen = self.gens.allocate(turn_id=turn_id)
        self._turn_in_flight = True
        return gen

    def end_turn(self) -> None:
        """Must run on every exit path. A stuck in-flight flag would arm barge-in forever and
        fence generations that were never running."""
        self._turn_in_flight = False
        self._armed = False

    def drain_fenced(self) -> list[tuple[str | None, str]]:
        """Hand back every generation fenced since the last drain, oldest first.

        A list rather than one slot: two interruptions in quick succession must both be
        observable, and the caller emits `FenceRequested` for each from a normal thread.
        """
        drained, self._fenced = self._fenced, []
        return drained

    def is_valid(self, gen_id: str) -> bool:
        """The single question every stage asks before publishing anything.

        Delegates to the registry rather than answering it here, so there is exactly one answer.
        """
        return self.gens.is_active(gen_id)

    def should_resume_after_short_utterance(self) -> bool:
        """A duck that never became a fence: the agent is still talking, so restore the volume.

        An audio-control decision only. No semantic claim is made about the utterance -- that is
        the Day-3 classifier's job.
        """
        return self.gate.is_ducked and not self._fence_applied

    def note_resumed(self) -> None:
        self._armed = False

    # --- observability ----------------------------------------------------------------

    @property
    def fence_applied(self) -> bool:
        return self._fence_applied

    @property
    def turn_in_flight(self) -> bool:
        return self._turn_in_flight

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def phase(self) -> Phase:
        """Derived view. Read for logging and tests; never branched on by the pipeline."""
        if self._fenced:
            return Phase.RECOVERING
        if self._turn_in_flight and self._fence_applied:
            return Phase.INTERRUPTED
        if self._turn_in_flight:
            return Phase.SPEAKING if self.gate.is_playing else Phase.THINKING
        if self.gate.is_playing and self.gens.active is not None:
            # THE AUDIO OUTLIVES THE TURN FUNCTION. `rime.speak` returns once audio is ENQUEUED,
            # not once it has been heard, so `handle_utterance` finishes and clears the in-flight
            # flag while several seconds of answer are still playing out of the gate. Reporting
            # LISTENING there told the UI the agent was idle while it was mid-sentence, and told
            # the INTERRUPT button there was nothing to interrupt at the exact moment there was.
            # Still derived, still never branched on: the gate and the registry are the ones
            # being asked. The registry half matters -- queued audio belonging to a FENCED
            # generation is about to be flushed, and calling that "speaking" would name a sound
            # nobody is going to hear.
            return Phase.SPEAKING
        return Phase.LISTENING
