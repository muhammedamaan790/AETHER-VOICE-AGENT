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
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        turn_id: int | None = None,
        gen: str | None = None,
    ) -> str:
        """int16 mono PCM -> final transcript. Emits TranscriptFinal."""
        if audio.dtype == np.int16:
            samples = audio.astype(np.float32) / 32768.0
        else:
            samples = audio.astype(np.float32)

        t0 = now_ms()
        segments, _info = self._model.transcribe(
            samples,
            language="en",
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
            model=self.model_size,
            audio_ms=round(len(audio) / self.samplerate * 1000.0, 1),
        )
        return text
