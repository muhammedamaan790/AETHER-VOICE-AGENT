"""Always-open microphone + WebRTC VAD.

The mic stream is opened once and stays open for the whole session, including while the agent is
speaking -- that is what makes barge-in detectable (PRD FR-2.1).

SpeechOnset timestamp: `t` is the arrival time of the **first voiced frame** of the run, not the
moment onset was declared after N confirming frames. That keeps `audio_kill_latency_ms =
AudioStopped.t - SpeechOnset.t` (PRD section 6) inclusive of the VAD confirmation window, which is
the honest reading of "VAD onset -> audio stopped". The later declaration time is also recorded, as
`declared_t`, so the split is visible.

Not included upstream of `t`: microphone hardware/driver capture latency, which happens before the
callback sees the frame. Noted as a limitation rather than silently absorbed.

Day-1 note: `on_onset` and `on_voiced_progress` fire on the PortAudio input thread and must stay
realtime-safe. Finished utterances are handed off through a queue so that STT never runs in the
callback.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

import numpy as np
import sounddevice as sd
import webrtcvad

from ..events import EventType
from ..trace import Trace, now_ms


class MicVAD:
    def __init__(
        self,
        trace: Trace,
        samplerate: int = 16000,
        frame_ms: int = 20,
        aggressiveness: int = 2,
        onset_frames: int = 2,        # 40 ms of voiced audio confirms an onset
        offset_frames: int = 25,      # 500 ms of silence ends an utterance
        preroll_frames: int = 15,     # 300 ms kept before onset so STT is not clipped
        device: int | None = None,
    ):
        if frame_ms not in (10, 20, 30):
            raise ValueError("webrtcvad supports 10, 20 or 30 ms frames")
        self.trace = trace
        self.samplerate = samplerate
        self.frame_ms = frame_ms
        self.frame_samples = samplerate * frame_ms // 1000
        self.onset_frames = onset_frames
        self.offset_frames = offset_frames
        self.preroll_frames = preroll_frames

        self._vad = webrtcvad.Vad(aggressiveness)
        self._stream = sd.InputStream(
            samplerate=samplerate,
            channels=1,
            dtype="int16",
            blocksize=self.frame_samples,
            device=device,
            latency="low",
            callback=self._callback,
        )

        # detector state (input-callback thread only)
        self._voiced_run = 0
        self._silence_run = 0
        self._speech_active = False
        self._first_voiced_t: float | None = None
        self._utterance: list[np.ndarray] = []
        self._preroll: list[np.ndarray] = []
        self._turn_id: int | None = None
        self._gen: str | None = None

        # hand-off
        self.utterances: queue.Queue[tuple[np.ndarray, float]] = queue.Queue()
        self.on_onset: Callable[[float], None] | None = None
        self.on_voiced_progress: Callable[[float], None] | None = None

        self._enabled = threading.Event()
        self._enabled.set()

    # --- lifecycle ------------------------------------------------------------------

    def open(self) -> None:
        self._stream.start()

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    @property
    def stream_input_latency_ms(self) -> float:
        return float(self._stream.latency) * 1000.0

    def set_context(self, *, turn_id: int | None = None, gen: str | None = None) -> None:
        self._turn_id = turn_id
        self._gen = gen

    # --- callback -------------------------------------------------------------------

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if not self._enabled.is_set():
            return
        self.process_frame(indata[:, 0].copy())

    def process_frame(self, frame: np.ndarray) -> None:
        """Run the detector over one frame.

        The live input callback and the latency harness both go through here, so a measured
        number describes the same code path that runs in the demo.
        """
        if len(frame) != self.frame_samples:
            return

        try:
            voiced = self._vad.is_speech(frame.tobytes(), self.samplerate)
        except Exception:
            return

        if voiced:
            if self._voiced_run == 0:
                self._first_voiced_t = now_ms()
            self._voiced_run += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
            if not self._speech_active:
                self._voiced_run = 0
                self._first_voiced_t = None

        if self._speech_active:
            self._utterance.append(frame)
        else:
            self._preroll.append(frame)
            if len(self._preroll) > self.preroll_frames:
                self._preroll.pop(0)

        # --- onset ---
        if not self._speech_active and self._voiced_run >= self.onset_frames:
            self._speech_active = True
            onset_t = self._first_voiced_t if self._first_voiced_t is not None else now_ms()
            self._utterance = list(self._preroll)
            self._preroll = []
            self.trace.emit(
                EventType.SPEECH_ONSET,
                turn_id=self._turn_id,
                gen=self._gen,
                t=onset_t,
                declared_t=round(now_ms(), 3),
                confirm_ms=round(now_ms() - onset_t, 3),
            )
            if self.on_onset is not None:
                self.on_onset(onset_t)   # must be realtime-safe (e.g. gate.request_duck)

        # --- ongoing voiced progress (drives the Day-1 meaningfulness window) ---
        if self._speech_active and self.on_voiced_progress is not None and self._first_voiced_t:
            self.on_voiced_progress(now_ms() - self._first_voiced_t)

        # --- offset ---
        if self._speech_active and self._silence_run >= self.offset_frames:
            audio = np.concatenate(self._utterance) if self._utterance else np.zeros(0, np.int16)
            onset_t = self._first_voiced_t or now_ms()
            self.trace.emit(
                EventType.SPEECH_ENDED,
                turn_id=self._turn_id,
                gen=self._gen,
                duration_ms=round(len(audio) / self.samplerate * 1000.0, 1),
            )
            self._speech_active = False
            self._voiced_run = 0
            self._silence_run = 0
            self._first_voiced_t = None
            self._utterance = []
            self.utterances.put((audio, onset_t))
