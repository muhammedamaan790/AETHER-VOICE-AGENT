"""STT over Groq's hosted Whisper, behind the same interface as the local recogniser.

    STT_PROVIDER=groq          (default stays `faster-whisper`)
    GROQ_API_KEY=...
    GROQ_STT_MODEL=whisper-large-v3-turbo

**Why a second recogniser at all.** `base.en` is local, free and offline, and it is the reason the
recorded demo is reproducible without a network. It is also small, English-only and the source of
the two worst misrecognitions on real calls -- "deluxe king" heard as "daily speed", "aisle 9" as
"IL-9" -- and Hindi and Spanish need a *different* model loaded beside it because `base.en` cannot
transcribe them at all.

Measured on this project's own captured phone audio, a three-second utterance (the length of a real
turn), four runs each:

    local  base.en                927 ms median   (841-1147, from evidence/demo-run.jsonl)
    groq   whisper-large-v3-turbo 276 ms median   (263-436)
    groq   whisper-large-v3       304 ms median   (300-311)

Against a measured turn latency of ~1262 ms that is roughly half the turn. The hosted model is also
`large-v3` rather than `base`, and multilingual in one model rather than two.

**What it costs, stated rather than buried.** The local path works with no network and no
credential; this one works only with both. So it is opt-in, `faster-whisper` remains the default,
and a failure here degrades to a lost turn that is visible in the trace rather than to a crash --
see `transcribe`.

The numbers above were taken on one network on one evening. They are a reason to offer the choice,
not a benchmark.
"""

from __future__ import annotations

import io
import os
import wave

import numpy as np

from .events import EventType
from .trace import Trace, now_ms

DEFAULT_MODEL = "whisper-large-v3-turbo"


class GroqSTT:
    """Groq-hosted Whisper. Same constructor shape, same `transcribe`, same emitted event.

    Deliberately mirrors `WhisperSTT` attribute for attribute -- `language`, `prompt`, `samplerate`,
    `model_size` -- because the turn loop sets those as state and must not learn which recogniser it
    is holding.
    """

    def __init__(
        self,
        trace: Trace,
        model_size: str = DEFAULT_MODEL,
        api_key: str | None = None,
        samplerate: int = 16000,
        **_ignored,
    ):
        from groq import Groq  # imported here: the SDK is only needed for this provider

        key = api_key or os.environ.get("GROQ_API_KEY") or os.environ.get("STT_API_KEY")
        if not key:
            raise RuntimeError(
                "STT_PROVIDER=groq needs GROQ_API_KEY (or STT_API_KEY). Presence is checked, "
                "never the value."
            )
        self.trace = trace
        self.samplerate = samplerate
        self.model_size = model_size
        self._client = Groq(api_key=key)
        # Kept ONLY so a provider error can be scrubbed of it before it is written down. The trace
        # redacts by FIELD NAME (`api_key`, `token`, ...), which cannot help here: the key would be
        # inside the text of a field called `stt_error`. A test caught exactly that.
        self._key = key
        # Mirrors of the local recogniser's state. The turn loop writes both.
        self.language = None
        self.prompt = None

    def _wav(self, audio: np.ndarray) -> bytes:
        """int16 mono PCM -> a WAV container, which is what the API accepts.

        Done in memory rather than through a temporary file: a turn is on a phone call, and a disk
        round-trip per utterance is latency for nothing.
        """
        pcm = audio if audio.dtype == np.int16 else (
            np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(self.samplerate)
            out.writeframes(pcm.tobytes())
        return buf.getvalue()

    def _safe_error(self, exc: Exception) -> str:
        """A provider error, shortened and stripped of anything that could be a credential.

        Truncation is NOT redaction, and assuming it was is the bug this exists to fix: a 401 that
        quotes the key back lands inside the first hundred characters. `Trace.redact` cannot help,
        because it redacts by field NAME and this is the text of a field called `stt_error` -- so
        the scrubbing has to happen here, where the key is known.
        """
        import re

        text = f"{type(exc).__name__}: {exc}"
        if self._key:
            text = text.replace(self._key, "<redacted>")
        # Defence in depth for anything key-shaped that is not OUR key -- a proxy's credential in a
        # relayed message, say. Deliberately blunt: an over-redacted error is still diagnosable.
        text = re.sub(r"\b(?:sk|gsk|rk)[-_][A-Za-z0-9_\-]{8,}", "<redacted>", text)
        return text[:160]

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        turn_id: int | None = None,
        gen: str | None = None,
        language=None,
    ) -> str:
        """int16 mono PCM -> final transcript. Emits TranscriptFinal, exactly as the local path does.

        **A provider failure returns "" rather than raising.** The turn loop already treats an empty
        transcript as "nothing was said" and moves on, which on a phone call is a dropped turn the
        caller can simply repeat -- where an exception would unwind past the run loop and end the
        session. The failure is not silent: `stt_error` is recorded on the event, so a trace shows
        which turns were lost and why rather than showing an inexplicable silence.
        """
        chosen = language if language is not None else self.language
        code = chosen.whisper_code if chosen is not None else "en"
        prompt = getattr(self, "prompt", None)

        t0 = now_ms()
        text, error = "", None
        try:
            kwargs = {
                "file": ("utterance.wav", self._wav(audio)),
                "model": self.model_size,
                "language": code,
            }
            if prompt:
                kwargs["prompt"] = prompt
            text = (self._client.audio.transcriptions.create(**kwargs).text or "").strip()
        except Exception as exc:                                    # noqa: BLE001
            error = self._safe_error(exc)
        elapsed = now_ms() - t0

        self.trace.emit(
            EventType.TRANSCRIPT_FINAL,
            turn_id=turn_id,
            gen=gen,
            text=text,
            stt_latency_ms=round(elapsed, 1),
            model=self.model_size,
            language=code,
            prompted=bool(prompt),
            provider="groq",
            stt_error=error,
            audio_ms=round(len(audio) / self.samplerate * 1000.0, 1),
        )
        if error:
            print(f"  (STT failed: {error})")
        return text


def build_stt(trace: Trace, model_size: str | None = None, **kwargs):
    """The recogniser named by `STT_PROVIDER`. Defaults to the local one.

    Selection is explicit and never silently substituted, matching how `build_llm` chooses a
    provider: an unknown value raises rather than quietly falling back, because a recogniser that is
    not the one you configured is exactly the kind of difference that is invisible until a demo.

    **On which to choose.** Round-tripped through Rime and back -- each language's real deterministic
    answers, spoken by the voice that would speak them, transcribed by each recogniser, scored with
    digit notation normalised so "420" is not counted as an error against "four hundred and twenty":

        language   local    groq     local model
        English    95.4%    95.4%    base.en        identical; groq 2-4x faster
        Spanish    92.4%    98.6%    base
        Hindi      21.7%    89.2%    base           <-- the local model does not work

    Hindi on the local multilingual `base` is not merely worse, it is unusable: it returned Urdu
    script for one answer and romanised transliteration for two others. **The multilingual product
    depends on the hosted recogniser.** English is a genuine choice -- identical words, three times
    the speed, against a network dependency the local path does not have.
    """
    provider = (os.environ.get("STT_PROVIDER") or "faster-whisper").strip().lower()

    if provider in ("groq", "groq-whisper"):
        return GroqSTT(
            trace,
            model_size=model_size or os.environ.get("GROQ_STT_MODEL") or DEFAULT_MODEL,
            **kwargs,
        )
    if provider in ("faster-whisper", "whisper", "local", ""):
        from .stt import WhisperSTT

        return WhisperSTT(trace, model_size=model_size or "base.en", **kwargs)

    raise RuntimeError(
        f"STT_PROVIDER={provider!r} is not a recogniser this build knows. "
        "Use 'faster-whisper' (local, default) or 'groq'."
    )
