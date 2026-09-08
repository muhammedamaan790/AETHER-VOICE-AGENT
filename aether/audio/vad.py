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

import os
import queue
import threading
from collections.abc import Callable

import numpy as np
import sounddevice as sd
import webrtcvad

from ..events import EventType
from ..trace import Trace, now_ms


# Conservative default. Chosen from THIS machine's traces, where the quietest utterance that
# transcribed as real speech peaked well above it and the observed room floor sat at 0.5-16.3 RMS.
# It is a starting point for an uncalibrated microphone, not a claim about microphones in general.
DEFAULT_SPEECH_FLOOR = 12.0


def _resolve_speech_floor(explicit: float | None) -> float:
    """Argument, then AETHER_SPEECH_FLOOR, then the default. A bad env value is ignored loudly."""
    if explicit is not None:
        return float(explicit)
    raw = (os.environ.get("AETHER_SPEECH_FLOOR") or "").strip()
    if not raw:
        return DEFAULT_SPEECH_FLOOR
    try:
        return float(raw)
    except ValueError:
        print(f"[vad] AETHER_SPEECH_FLOOR={raw!r} is not a number; using {DEFAULT_SPEECH_FLOOR}")
        return DEFAULT_SPEECH_FLOOR


# How much silence ends an utterance.
#
# 500 ms. TRIED AT 1000 ms ON A REAL CALL AND REVERTED -- the caller reported it plainly worse, and
# the trace agrees. Giving the caller a second to think does stop mid-sentence fragmentation, but
# it also stops the detector closing between sentences, so buffers grew from 1.2-4.2 s to 6.7, 6.5
# and 8.3 s, half of them silence:
#
#     dur=6680 ms  voiced=3900 ms  ->  'Hi, can you hear me?'
#     dur=8280 ms  voiced=4400 ms  ->  rejected as noise
#
# Whisper handed six seconds of mostly-silence guesses; "Yes, can I help you with that?" came back
# for something the caller never said. Longer windows trade fragmentation for hallucination, and
# hallucination is the worse failure.
#
# The real fix for fragmentation is not a longer window -- it is not sending the silence to STT at
# all. Left as a known limitation rather than guessed at again.
#
# THE COST OF RAISING IT IS PAID ON EVERY TURN: this is dead time between the caller stopping and
# AETHER starting, so turn latency rises by the difference, and voice interruption lands at
# end-of-utterance in hands-free mode so it slows by the same amount. Tune with AETHER_ENDPOINT_MS
# rather than editing this.
DEFAULT_ENDPOINT_MS = 500.0


def _resolve_endpoint_frames(explicit: int | None, frame_ms: int) -> int:
    """Argument, then AETHER_ENDPOINT_MS, then the default. At least one frame, or speech never ends."""
    if explicit is not None:
        return int(explicit)
    raw = (os.environ.get("AETHER_ENDPOINT_MS") or "").strip()
    endpoint_ms = DEFAULT_ENDPOINT_MS
    if raw:
        try:
            endpoint_ms = float(raw)
        except ValueError:
            print(f"[vad] AETHER_ENDPOINT_MS={raw!r} is not a number; using {DEFAULT_ENDPOINT_MS}")
    return max(1, round(endpoint_ms / frame_ms))


class InputDeviceError(RuntimeError):
    """A microphone was explicitly requested and cannot be used. Never silently substituted."""


def resolve_input_device(device: int | None = None) -> int | None:
    """Decide which microphone the pipeline records from.

    Explicit argument wins, then `AETHER_INPUT_DEVICE`, then PortAudio's default (None).

    An explicitly requested device is validated and raises on failure rather than falling back.
    Falling back is exactly the failure mode this exists to prevent: the machine this was found on
    has its default input on a headset mic that captures effectively silence (RMS 1.5e-05), so a
    silent fallback looks identical to a broken VAD and costs an afternoon to diagnose.
    """
    if device is not None:
        return _validate_input_device(device, source="argument")

    raw = (os.environ.get("AETHER_INPUT_DEVICE") or "").strip()
    if not raw:
        return None                       # PortAudio default: unchanged behaviour

    try:
        index = int(raw)
    except ValueError as exc:
        raise InputDeviceError(
            f"AETHER_INPUT_DEVICE={raw!r} is not an integer device index. "
            "List devices with: python -c \"import sounddevice; print(sounddevice.query_devices())\""
        ) from exc
    return _validate_input_device(index, source="AETHER_INPUT_DEVICE")


def _validate_input_device(index: int, *, source: str) -> int:
    try:
        info = sd.query_devices(index)
    except Exception as exc:
        raise InputDeviceError(f"{source} selected device {index}, which does not exist: {exc}") from exc
    if int(info.get("max_input_channels", 0)) < 1:
        raise InputDeviceError(
            f"{source} selected device {index} ({info.get('name','?')}), "
            "which has no input channels -- that is an output device."
        )
    return index


def describe_device(index: int | None) -> str:
    """`[2] Microphone Array (...) @ 44100 Hz` -- the resolved endpoint, not the env var."""
    try:
        resolved = index if index is not None else sd.default.device[0]
        info = sd.query_devices(resolved)
        return f"[{resolved}] {info['name']} @ {int(info['default_samplerate'])} Hz"
    except Exception:
        return f"[{index}] (unavailable)"


class MicVAD:
    def __init__(
        self,
        trace: Trace,
        samplerate: int = 16000,
        frame_ms: int = 20,
        aggressiveness: int = 2,
        onset_frames: int = 2,        # 40 ms of voiced audio confirms an onset
        offset_frames: int | None = None,   # silence that ends an utterance; see DEFAULT_ENDPOINT_MS
        preroll_frames: int = 15,     # 300 ms kept before onset so STT is not clipped
        min_speech_ms: float = 250.0, # shorter than this is a blip, not an utterance
        noise_snr_margin: float = 3.0,# voiced audio must be this much louder than ambient
        speech_floor: float | None = None,  # absolute peak a voiced run must reach
        device: int | None = None,
    ):
        if frame_ms not in (10, 20, 30):
            raise ValueError("webrtcvad supports 10, 20 or 30 ms frames")
        self.trace = trace
        self.samplerate = samplerate
        self.frame_ms = frame_ms
        self.frame_samples = samplerate * frame_ms // 1000
        self.onset_frames = onset_frames
        self.offset_frames = _resolve_endpoint_frames(offset_frames, frame_ms)
        self.preroll_frames = preroll_frames
        self.min_speech_ms = min_speech_ms
        self.noise_snr_margin = noise_snr_margin
        # Absolute floor, resolved once: argument, then AETHER_SPEECH_FLOOR, then the default.
        #
        # DEFAULT_SPEECH_FLOOR is a conservative starting point, NOT a universal constant. Run
        # `python scripts/calibrate_mic.py` to derive the value for a given microphone. A previous
        # hardcoded floor of 60 rejected genuine speech on this machine because it was calibrated
        # from a single session's sample -- the exact failure this module's docstring warned about.
        self.speech_floor_abs = _resolve_speech_floor(speech_floor)

        self._vad = webrtcvad.Vad(aggressiveness)
        # Resolve BEFORE opening, and keep it: `self.device` is the index actually handed to
        # PortAudio, which is what startup reports.
        self.device = resolve_input_device(device)
        device = self.device
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

        # Rolling ambient-noise estimate, updated only from unvoiced frames while idle. The
        # rejection test below is relative to this, so a quiet room and a noisy warehouse get
        # different bars without anyone tuning an absolute threshold.
        self._ambient_rms: float | None = None
        self._voiced_rms_sum = 0.0
        self._voiced_rms_n = 0
        self._voiced_rms_peak = 0.0

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

    @property
    def listening(self) -> bool:
        return self._enabled.is_set()

    def set_listening(self, listening: bool) -> None:
        """Open or close the microphone without touching the PortAudio stream.

        The stream stays open for the whole session either way -- stopping and restarting it costs
        device-negotiation time and can fail outright, which is not something to do on every turn.
        This flips the flag the callback already checks, so a closed mic costs one boolean test per
        frame and captures nothing.

        Why this exists: in push-to-talk mode an always-open mic cannot tell "talking to the agent"
        from "talking to a colleague", and no amount of level or duration tuning fixes that -- it
        needs speaker identity, which AETHER does not have. Closing the mic answers the question
        by construction.

        Detector state is reset on the closing edge so a half-captured utterance can never be
        stitched onto the next time the mic opens.
        """
        if listening:
            self._enabled.set()
            return
        self._enabled.clear()
        self._reset_detector()

    def _reset_detector(self) -> None:
        """Drop any in-progress utterance. Callback-thread state only, no IO."""
        self._voiced_run = 0
        self._silence_run = 0
        self._speech_active = False
        self._first_voiced_t = None
        self._utterance = []
        self._preroll = []
        self._voiced_rms_sum = 0.0
        self._voiced_rms_n = 0
        self._voiced_rms_peak = 0.0

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

        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))

        if voiced:
            if self._voiced_run == 0:
                self._first_voiced_t = now_ms()
            self._voiced_run += 1
            self._silence_run = 0
            self._voiced_rms_sum += rms
            self._voiced_rms_n += 1
            # Peak matters more than the mean. Speech carries vowel peaks far above its own
            # average, while low-level noise is flat -- and a mean is dragged down by the quiet
            # voiced frames every longer utterance contains, which penalised exactly the
            # utterances worth keeping (rejected at 880-1640 ms, while shorter comparable speech
            # at the same distance was accepted).
            if rms > self._voiced_rms_peak:
                self._voiced_rms_peak = rms
        else:
            self._silence_run += 1
            if not self._speech_active:
                self._voiced_run = 0
                self._first_voiced_t = None
                # Track the room only while nobody is talking, so speech never inflates the floor.
                self._ambient_rms = (
                    rms if self._ambient_rms is None else 0.95 * self._ambient_rms + 0.05 * rms
                )

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
        #
        # Gated on credibility, and the gate has to be HERE rather than at the offset.
        #
        # `_rejection_reason` runs when the utterance ends, which is ~500 ms of trailing silence
        # after the fence has already been applied at 300 ms of voiced audio. Measured on real
        # runs: 6 of 7 rejected utterances had already ducked, stopped or fenced audio by the time
        # the gate declared them noise. So the gate decided correctly and arrived far too late --
        # background noise killed the turn, and the trace recorded a clean "too_short" over the
        # wreckage. That is the bug behind "it treats surrounding noise as an interruption".
        #
        # Duck is deliberately still unconditional (locked decision 6: duck is not stop). Ducking
        # on a door slam is cheap and self-correcting -- `should_resume_after_short_utterance`
        # puts the volume back. Fencing on a door slam destroys the answer. So the expensive,
        # irreversible half now requires the same level evidence that acceptance requires.
        if self._speech_active and self.on_voiced_progress is not None and self._first_voiced_t:
            if self._is_credible_speech():
                self.on_voiced_progress(now_ms() - self._first_voiced_t)

        # --- offset ---
        if self._speech_active and self._silence_run >= self.offset_frames:
            audio = np.concatenate(self._utterance) if self._utterance else np.zeros(0, np.int16)
            onset_t = self._first_voiced_t or now_ms()
            voiced_ms = self._voiced_rms_n * self.frame_ms
            speech_rms = (
                self._voiced_rms_sum / self._voiced_rms_n if self._voiced_rms_n else 0.0
            )
            reject = self._rejection_reason(voiced_ms, self._voiced_rms_peak)

            self.trace.emit(
                EventType.SPEECH_ENDED,
                turn_id=self._turn_id,
                gen=self._gen,
                duration_ms=round(len(audio) / self.samplerate * 1000.0, 1),
                voiced_ms=round(voiced_ms, 1),
                speech_rms=round(speech_rms, 1),
                speech_peak_rms=round(self._voiced_rms_peak, 1),
                speech_floor=round(self.speech_floor(), 1),
                ambient_rms=round(self._ambient_rms, 1) if self._ambient_rms is not None else None,
                rejected=reject or None,
            )
            self._speech_active = False
            self._voiced_run = 0
            self._silence_run = 0
            self._first_voiced_t = None
            self._utterance = []
            self._voiced_rms_sum = 0.0
            self._voiced_rms_n = 0
            self._voiced_rms_peak = 0.0
            if reject is None:
                self.utterances.put((audio, onset_t))

    def _rejection_reason(self, voiced_ms: float, peak_rms: float) -> str | None:
        """Why this utterance is not worth transcribing, or None to accept it.

        Two conservative tests, both grounded in observed traces:

        `too_short` -- webrtcvad confirms an onset after 40 ms, which is short enough that a cough
        or a door becomes an "utterance". Real traces show these reaching STT as `'You'`, `'Oh'`
        and `''`; base.en genuinely returns `'You'` for digital silence, so the blip does not
        just waste 4-9 s of STT, it invents words that were never said.

        `below_noise_floor` -- someone talking across the room is speech, so webrtcvad is right to
        flag it; it is simply not speech aimed at this microphone. Distance shows up as level, so
        the test is relative to the measured room, not an absolute number that would need retuning
        per environment.

        The floor is clamped by `ambient_floor_min`, and that clamp is what makes the relative
        test work at all on a quiet microphone. Measured on real runs: ambient tracked to
        0.5-9.7 RMS, so `ambient x 3` was a bar of ~2, and faint noise at RMS 10-40 cleared it by
        20x while real speech sat at 60-2800. A relative test against a near-zero floor is not a
        test. Clamping says: a microphone reporting near-silence is describing its own noise
        floor, not the room, so do not believe a bar derived from it.

        Still conservative in the direction that matters -- the clamp is far below observed real
        speech, and dropping genuine close-range speech remains the worse failure (RULES.md R2.3).
        """
        if voiced_ms < self.min_speech_ms:
            return "too_short"
        if peak_rms < self.speech_floor():
            return "below_noise_floor"
        return None

    def speech_floor(self) -> float:
        """The level a voiced run must clear to count as speech aimed at this microphone.

        One definition, used by BOTH the acceptance gate and the fence-time credibility check, so
        the two can never disagree about what counts as speech.
        """
        relative = (self._ambient_rms or 0.0) * self.noise_snr_margin
        return max(relative, self.speech_floor_abs)

    def _is_credible_speech(self) -> bool:
        """Is the voiced run so far loud enough to justify FENCING a generation?

        Uses the running mean of voiced frames, which is available continuously -- unlike the
        offset-time statistics, which arrive after the fence would already have been applied.

        Returns True while no voiced frames have been measured yet, so the very first frames of a
        genuine utterance are never suppressed by an empty average.
        """
        if self._voiced_rms_n == 0:
            return True
        return self._voiced_rms_peak >= self.speech_floor()
