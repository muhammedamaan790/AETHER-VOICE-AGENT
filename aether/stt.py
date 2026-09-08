"""Day-1 STT: faster-whisper, running locally on CPU.

Local by choice: no network round-trip in the interruption path, no STT credential needed for the
Day-1 spike, and deterministic enough to re-run. Emits TranscriptFinal from the canonical
vocabulary.
"""

from __future__ import annotations

import numpy as np

from .events import EventType
from .trace import Trace, now_ms


class WhisperSTT:
    def __init__(
        self,
        trace: Trace,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        samplerate: int = 16000,
    ):
        from faster_whisper import WhisperModel  # imported here: model load is slow

        self.trace = trace
        self.samplerate = samplerate
        self.model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        # More than one model can be alive at once, because more than one is needed: `base.en` is
        # MONOLINGUAL and cannot transcribe Hindi at all -- it is not a matter of passing a
        # different language code. Loaded lazily and cached, so an English-only call never pays for
        # the multilingual weights and the recorded English demo is bit-for-bit unaffected.
        self._models = {model_size: self._model}
        # The language currently being spoken. State, like `model_size`, because the turn loop sets
        # it once on a switch rather than repeating it on every call.
        self.language = None

    def _model_for(self, size: str):
        from faster_whisper import WhisperModel

        if size not in self._models:
            self._models[size] = WhisperModel(
                size, device=self._device, compute_type=self._compute_type
            )
        return self._models[size]

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        turn_id: int | None = None,
        gen: str | None = None,
        language=None,
    ) -> str:
        """int16 mono PCM -> final transcript. Emits TranscriptFinal.

        `language` names a `aether.lang.Language` and selects both the model and the code passed to
        Whisper. **Omitting it keeps the exact previous behaviour** -- this instance's model, and
        English -- so every existing caller and the recorded demo are untouched.
        """
        if audio.dtype == np.int16:
            samples = audio.astype(np.float32) / 32768.0
        else:
            samples = audio.astype(np.float32)

        size, code = self.model_size, "en"
        chosen = language if language is not None else self.language
        if chosen is not None:
            size, code = chosen.whisper_model, chosen.whisper_code
        model = self._model if size == self.model_size else self._model_for(size)

        t0 = now_ms()
        segments, _info = model.transcribe(
            samples,
            language=code,
            beam_size=5,
            vad_filter=False,   # segmentation already happened upstream in MicVAD
        )
        text = " ".join(s.text for s in segments).strip()
        elapsed = now_ms() - t0

        self.trace.emit(
            EventType.TRANSCRIPT_FINAL,
            turn_id=turn_id,
            gen=gen,
            text=text,
            stt_latency_ms=round(elapsed, 1),
            # The model that actually ran, not the one this instance was constructed with -- on a
            # Hindi turn they differ, and a trace that said otherwise would misattribute the latency.
            model=size,
            language=code,
            audio_ms=round(len(audio) / self.samplerate * 1000.0, 1),
        )
        return text
