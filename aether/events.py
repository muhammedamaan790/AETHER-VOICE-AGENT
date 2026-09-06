"""Canonical event vocabulary for AETHER.

This module is the machine-readable source of truth. The runtime, the trace writer, the tests, and
the evaluator all use these names -- no synonyms, no free-form strings, no per-module local names.

Adding, renaming, or removing an event means changing this file AND ARCHITECTURE.md section 4 in the
same change. See RULES.md R3.
"""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    """The one event vocabulary. Documented in ARCHITECTURE.md section 4."""

    # --- Audio / speech detection -------------------------------------------------
    SPEECH_ONSET = "SpeechOnset"
    SPEECH_ENDED = "SpeechEnded"
    AUDIO_DUCKED = "AudioDucked"
    AUDIO_RESUMED = "AudioResumed"
    AUDIO_STOPPED = "AudioStopped"

    # --- Understanding ------------------------------------------------------------
    TRANSCRIPT_FINAL = "TranscriptFinal"
    BACKCHANNEL_DETECTED = "BackchannelDetected"
    INTERRUPTION_CLASSIFIED = "InterruptionClassified"

    # --- Task / generation lifecycle ----------------------------------------------
    TASK_STARTED = "TaskStarted"
    TASK_REPLACED = "TaskReplaced"
    FENCE_REQUESTED = "FenceRequested"
    GENERATION_CHANGED = "GenerationChanged"
    CANCELLATION_RESOLVED = "CancellationResolved"

    # --- Results ------------------------------------------------------------------
    RESULT_RECEIVED = "ResultReceived"
    RESULT_DISCARDED = "ResultDiscarded"
    RESULT_SALVAGED = "ResultSalvaged"
    RESULT_LEAKED = "ResultLeaked"

    # --- Output -------------------------------------------------------------------
    RESPONSE_SPOKEN = "ResponseSpoken"


class InterruptionClass(str, Enum):
    """The frozen six-class taxonomy. Exactly six. See MEMORY.md locked decision 3."""

    BACKCHANNEL = "BACKCHANNEL"
    REFINEMENT = "REFINEMENT"
    REPLACEMENT = "REPLACEMENT"
    STATUS_QUERY = "STATUS_QUERY"
    CANCEL = "CANCEL"
    NEW_TASK = "NEW_TASK"


class GenerationStatus(str, Enum):
    """Core generation model: two states only.

    There is deliberately no SUSPENDED. It is added only if single-slot suspend/resume is actually
    implemented and tested (PHASES.md Day 5). See RULES.md R4.
    """

    ACTIVE = "active"
    FENCED = "fenced"


class TransitionReason(str, Enum):
    """`reason` field on TaskReplaced -- the three transitions share one canonical event."""

    REFINEMENT = "refinement"
    REPLACEMENT = "replacement"
    NEW_TASK = "new_task"


# TODO(Day 4): Event dataclass + append-only JSONL trace writer.
# Every event carries: seq (monotonic), t (monotonic ms), turn_id, gen (nullable for
# pre-generation audio events), plus the per-event fields in ARCHITECTURE.md section 4.
