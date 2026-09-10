"""Per-turn latency breakdown.

Answers one diagnostic question: when a conversation feels slow, WHICH stage was slow?

The marks are the boundaries a caller actually experiences, from the moment they stop talking to
the moment they hear a reply:

    SpeechEnded ──stt──▶ TranscriptFinal ──llm──▶ LLM done ──tts──▶ first audio ──▶ ResponseSpoken
    └──────────────────────── turn_latency_ms (the silence the user sits through) ─────┘

`turn_latency_ms` is the headline number: everything before the first sample of audio is dead air.
The three components sum to it, so a slow turn points at its own culprit.

This module only does arithmetic on timestamps handed to it. It emits nothing, owns no state
beyond one turn, and adds no event to the canonical vocabulary — the numbers ride on the existing
`ResponseSpoken` event (MEMORY.md locked decision 10). Nothing here can influence fencing,
history, or what gets spoken.
"""

from __future__ import annotations

from dataclasses import dataclass

# All marks are monotonic milliseconds from aether.trace.now_ms().


@dataclass
class TurnTiming:
    """Timestamps for one turn. Any mark may be None; deltas that depend on it are then None."""

    speech_ended: float | None = None   # VAD saw the user stop talking
    transcript: float | None = None     # STT produced the final transcript
    llm_start: float | None = None      # request handed to the provider
    llm_end: float | None = None        # provider's answer in hand
    first_audio: float | None = None    # first chunk ACCEPTED from Rime -- upstream of the queue
    first_output: float | None = None   # callback handed real samples to the device (observable end)
    device_latency_ms: float | None = None   # the device buffer after that; reported, never added
    spoken: float | None = None         # ResponseSpoken emitted

    @staticmethod
    def delta(end: float | None, start: float | None) -> float | None:
        """`end - start`, or None if either mark is missing.

        A negative result is returned as-is rather than clamped: it would mean the marks were
        recorded out of order, and hiding that would turn a bug into a plausible-looking number.
        """
        if end is None or start is None:
            return None
        return round(end - start, 1)

    # --- component stages -------------------------------------------------------------

    @property
    def stt_ms(self) -> float | None:
        """End of speech -> transcript ready."""
        return self.delta(self.transcript, self.speech_ended)

    @property
    def llm_ms(self) -> float | None:
        """Transcript -> the model's answer."""
        return self.delta(self.llm_end, self.llm_start)

    @property
    def tts_ms(self) -> float | None:
        """Answer -> the first audio the caller can actually hear."""
        return self.delta(self.first_audio, self.llm_end)

    # --- headline numbers -------------------------------------------------------------

    @property
    def turn_latency_ms(self) -> float | None:
        """Silence the user sits through: they stopped talking, and heard nothing until now."""
        return self.delta(self.first_audio, self.speech_ended)

    @property
    def response_latency_ms(self) -> float | None:
        """PRD.md section 6: `ResponseSpoken.t - TranscriptFinal.t`, verbatim."""
        return self.delta(self.spoken, self.transcript)

    @property
    def output_latency_ms(self) -> float | None:
        """Speech ended -> the audio gate actually handed samples to the device.

        The honest end-to-end figure, and it is deliberately NOT the same as `turn_latency_ms`.
        That one ends when the TTS client accepts Rime's first chunk, which is upstream of the
        output queue and of the device buffer; this one ends at the last point this process can
        observe. The difference between them is the buffering the application adds.

        Still not "when the caller heard it": the device's own buffer sits after this, reported
        separately as `device_latency_ms` and never added in.
        """
        return self.delta(self.first_output, self.speech_ended)

    # --- trace payload ----------------------------------------------------------------

    def fields(self) -> dict[str, float | None]:
        """Flat fields for the `ResponseSpoken` event. Flat so a trace stays greppable."""
        return {
            "t_speech_ended": self.speech_ended,
            "t_transcript": self.transcript,
            "t_llm_start": self.llm_start,
            "t_llm_end": self.llm_end,
            "t_first_audio": self.first_audio,
            "t_spoken": self.spoken,
            "stt_ms": self.stt_ms,
            "llm_ms": self.llm_ms,
            "tts_ms": self.tts_ms,
            "turn_latency_ms": self.turn_latency_ms,
            "response_latency_ms": self.response_latency_ms,
            "output_latency_ms": self.output_latency_ms,
            "device_latency_ms": self.device_latency_ms,
        }

    def summary(self) -> str:
        """One-line operator diagnosis, e.g. `stt=310 llm=880 tts=1540 -> turn=2730 ms`."""
        def fmt(v: float | None) -> str:
            return "?" if v is None else f"{v:.0f}"

        return (
            f"stt={fmt(self.stt_ms)} llm={fmt(self.llm_ms)} tts={fmt(self.tts_ms)} "
            f"-> turn={fmt(self.turn_latency_ms)} ms"
        )
