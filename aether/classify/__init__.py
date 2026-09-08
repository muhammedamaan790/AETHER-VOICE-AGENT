"""Deterministic interruption classification: what did this utterance MEAN for the task in flight?

Closed sets and whole-utterance matching. No model, no embeddings, no heuristic scoring. Three
reasons, and they are the same three that justify the deterministic menu router:

1. **Latency.** This runs between the transcript and the decision to fence. A model here would add
   a second network round-trip to the interruption path, which is the one path that must be fast.
2. **Correctness over recall.** A misfire is expensive in both directions -- classifying a real
   question as a backchannel loses the caller's turn, and classifying "mm-hm" as a new question
   destroys the answer they were listening to. Everything ambiguous therefore falls through to
   `REPLACEMENT`, which is exactly what AETHER did before this module existed. The classifier can
   only ever *withhold* a fence for a phrase it recognises with certainty; it can never invent one.
3. **Determinism.** The demo must classify the same way every rehearsal, and a test must be able to
   assert a class rather than a probability.

**Where this sits.** It runs after STT and BEFORE `BargeInCoordinator.begin_turn`, because
`begin_turn` allocates a generation and allocation is what fences the previous one. Classifying
afterwards would be classifying a task that had already been destroyed. It returns a judgement and
nothing else -- it holds no state, touches no registry, and fences nothing. Generation fencing
remains the final authority: every class except `BACKCHANNEL` and `STATUS_QUERY` proceeds into the
ordinary turn path and is fenced by the ordinary mechanism.

**What is deliberately NOT here.**

* No `RESULT_SALVAGED`. Salvage means reusing part of a fenced task's work, and AETHER has no
  partial-result store to reuse from -- Level 1 remembers a turn whole or not at all. Emitting a
  salvage event with `records_reused = 0` on every refinement would be evidence-shaped noise
  (RULES.md R8), so refinement is labelled honestly and fences like a replacement.
* No spoken status answer. `STATUS_QUERY` protects the task; it does not talk over it. See
  `Day1Spike._resolve_without_a_turn` for why speaking one would need a second audio path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..events import InterruptionClass, TransitionReason


# A leading hesitation carries no meaning, and "uh, stop" must cancel exactly as "stop" does.
# Stripping is tried as an ALTERNATIVE reading rather than applied unconditionally, because some
# of these words are also the first half of a real phrase -- "uh huh" is a backchannel, and
# stripping its "uh" leaves "huh", which is not in any table.
_FILLERS = ("uh", "um", "er", "ah", "so", "and", "okay", "ok", "well")


def normalise(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. STT output is not tidy."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _readings(spoken: str) -> tuple[str, ...]:
    """The forms to match against: the sentence as heard, then with a leading filler removed.

    Order matters. The full form wins, so a phrase that IS a table entry is never destroyed by
    stripping a word that happens to look like a hesitation.
    """
    head, _, rest = spoken.partition(" ")
    if head in _FILLERS and rest:
        return (spoken, rest)
    return (spoken,)


# --- the closed sets -------------------------------------------------------------------------
#
# Every entry is matched against the WHOLE normalised utterance, never as a substring. That is the
# single most important property in this file: "stop" cancels, "how do I stop by the front desk"
# does not. Substring matching here would let an ordinary question destroy a task, which is a worse
# failure than not recognising a phrase at all.

_BACKCHANNEL = frozenset({
    "mm", "mmm", "mhm", "mm hm", "mm hmm", "mmhm", "hmm", "hm",
    "uh huh", "uhuh", "aha", "ah", "oh", "oh ok", "oh okay",
    "yeah", "yep", "yes", "yup", "ya", "right", "ok", "okay", "k",
    "sure", "i see", "got it", "go on", "carry on", "gotcha", "cool", "nice",
})

_CANCEL = frozenset({
    "stop", "stop it", "stop please", "please stop", "stop talking", "be quiet", "quiet",
    "cancel", "cancel that", "cancel it", "forget it", "forget that", "leave it",
    "never mind", "nevermind", "no never mind", "not any more", "not anymore",
    "that s all", "thats all", "that s it", "thats it", "nothing", "no thanks",
    "no thank you", "we re done", "were done", "i m done", "im done", "done",
})

_STATUS_QUERY = frozenset({
    "are you still there", "you still there", "still there", "hello are you there",
    "are you there", "you there", "are you ok", "are you okay",
    "what are you doing", "what s happening", "whats happening", "what s going on",
    "whats going on", "how long will this take", "how long", "how much longer",
    "are you done", "are you finished", "did you get that", "did you hear me",
    "can you hear me", "you get that",
})

# A refinement narrows or corrects the question just asked; a replacement abandons it. Both fence,
# because AETHER cannot salvage partial work -- see the module docstring. The distinction is
# recorded so `TaskReplaced.reason` is honest, not because it changes what happens.
#
# Matched as a PREFIX, not as a whole utterance, because a refinement always carries the correction
# after the marker: "actually make it vegetarian". The markers are chosen to be unambiguous at the
# start of a sentence.
_REFINEMENT_PREFIXES = (
    "actually", "sorry i meant", "sorry i mean", "i meant", "i mean", "make it", "make that",
    "no i meant", "no i mean", "no make it", "rather", "instead", "change that to",
    "sorry not", "no not", "i said",
)


# How long a phrase of N words can plausibly have taken to say.
#
# MEASURED, not guessed. The repository's one real speech fixture
# (`tests/fixtures_stt_probe.wav`, "Find the priority orders in aisle 9.") runs through the real
# `MicVAD` detector as **2320 ms of voiced audio for 7 words = 331 ms per word**. The bound below
# is 1000 ms per word: three times slower than that measurement, so it admits deliberate, drawled
# or hesitant speech and rejects only what could not be a faithful reading of the audio at all.
#
# It compares against `SpeechEnded.voiced_ms`, which counts voiced frames only -- the 300 ms
# preroll and the 500 ms of trailing silence that end an utterance are already excluded, so this
# is genuinely "how long were they talking" and not "how big was the buffer".
#
# This is NOT a VAD threshold and changes nothing about capture, endpointing or transcription. It
# decides only whether a closed-set phrase is allowed to withhold a turn.
_MAX_MS_PER_WORD = 1000.0
# One short word still gets a fair hearing: 331 ms measured, and frame quantisation is 20 ms.
_MIN_CREDIBLE_MS = 400.0


def _duration_is_credible(spoken: str, voiced_ms: float | None) -> bool:
    """Could this transcript really have taken this long to say?

    `None` means the caller has no duration evidence -- some tests, and any future caller that is
    not the live pipeline. It is treated as credible, so the closed sets behave exactly as they did
    before this check existed. The live path always has the figure: `MicVAD` emits `SpeechEnded`
    immediately before it queues the utterance, so there is always a measurement to read.
    """
    if voiced_ms is None:
        return True
    return voiced_ms <= max(_MIN_CREDIBLE_MS, len(spoken.split()) * _MAX_MS_PER_WORD)


@dataclass(frozen=True)
class Classification:
    """One judgement about one utterance. Immutable, and carries why it was made."""

    cls: InterruptionClass
    rule: str                       # which table matched, recorded in the trace for auditability
    text: str                       # the normalised form the rule matched against

    @property
    def protects_task(self) -> bool:
        """True when this class must NOT begin a turn, and so must not fence the active one.

        The two classes where the caller's words were not a new request: a backchannel is
        encouragement, and a status query is a question about the task rather than a new one.
        """
        return self.cls in (InterruptionClass.BACKCHANNEL, InterruptionClass.STATUS_QUERY)

    @property
    def transition(self) -> TransitionReason | None:
        """The `TaskReplaced.reason` this class implies, or None if no task was displaced."""
        return {
            InterruptionClass.REFINEMENT: TransitionReason.REFINEMENT,
            InterruptionClass.REPLACEMENT: TransitionReason.REPLACEMENT,
            InterruptionClass.NEW_TASK: TransitionReason.NEW_TASK,
        }.get(self.cls)


def classify(text: str, *, in_flight: bool, voiced_ms: float | None = None) -> Classification:
    """Classify one utterance. `in_flight` is whether a turn is currently running.

    `in_flight` is not context in the conversational sense -- it is the question of whether there
    is anything to interrupt. The same words mean different things: "okay" while AETHER is
    mid-answer is a backchannel, and "okay" said into silence is the start of a turn.

    `voiced_ms` is how much the caller ACTUALLY SAID, from `SpeechEnded.voiced_ms` -- voiced frames
    only, with the preroll and the trailing silence excluded. It exists to catch the one failure
    mode the closed sets cannot see on their own: a garbled three-second question that Whisper
    renders as the single word "Okay." A caller who was talking for three seconds did not utter a
    backchannel, whatever the transcript says, and treating that as one silently loses their turn.
    See `_duration_is_credible`.

    Order is by confidence, not by frequency. The three closed sets are exact and are tried first;
    everything after them is a fallback, and the final fallback is the behaviour AETHER had before
    this module existed.
    """
    spoken = normalise(text)
    if not spoken:
        # An empty transcript is not an interruption. Treated as a new task so the caller does not
        # silently lose a turn, and so this function never returns a class it cannot justify.
        return Classification(InterruptionClass.NEW_TASK, "empty", spoken)

    readings = _readings(spoken)
    # Every closed set is gated on this. A phrase that is too long to have been spoken in the audio
    # we heard is a transcription artefact, and an artefact must never be allowed to withhold a
    # turn or cancel a task -- it falls through to REPLACEMENT, which is what AETHER did before
    # this module existed.
    credible = _duration_is_credible(spoken, voiced_ms)

    # 1. BACKCHANNEL -- only meaningful against something to encourage. "yes" into silence is an
    #    answer to a question AETHER asked, not a backchannel, and must start a turn.
    if in_flight and credible:
        for form in readings:
            if form in _BACKCHANNEL:
                return Classification(InterruptionClass.BACKCHANNEL, "closed_set", form)

    # 2. CANCEL -- whole utterance only. Legal with nothing in flight, where it resolves to a
    #    no-op: the caller asked for silence and silence is what they get.
    if credible:
        for form in readings:
            if form in _CANCEL:
                return Classification(InterruptionClass.CANCEL, "closed_set", form)

    # 3. STATUS_QUERY -- a question ABOUT the task. With nothing in flight it is ordinary
    #    conversation ("can you hear me?" on a bad line) and belongs to the model, so it only
    #    counts as a status query when there is a status to report.
    if in_flight and credible:
        for form in readings:
            if form in _STATUS_QUERY:
                return Classification(InterruptionClass.STATUS_QUERY, "closed_set", form)

    # 4. REFINEMENT -- an explicit correction of the question in flight. Prefix-matched, and only
    #    while something is running: "actually, what time is checkout" said into silence is just a
    #    question.
    if in_flight:
        for form in readings:
            for prefix in _REFINEMENT_PREFIXES:
                if form == prefix or form.startswith(prefix + " "):
                    return Classification(InterruptionClass.REFINEMENT, f"prefix:{prefix}", form)

        # 5. Anything else said over a running task replaces it. THE SAFE FALLBACK: uncertain
        #    refinement-versus-replacement lands here, and replacement fences, which is the
        #    conservative outcome.
        return Classification(InterruptionClass.REPLACEMENT, "default_in_flight", spoken)

    # 6. Nothing was running, so nothing was interrupted.
    return Classification(InterruptionClass.NEW_TASK, "default_idle", spoken)
