"""AudioGate: playback with immediate duck and confirmed full stop.

RULES.md R6 -- duck and stop are different actions with different triggers and different events:
  duck  : immediate, reversible, taken on SpeechOnset before any understanding exists
  stop  : irreversible, taken only once the interruption is confirmed meaningful

Threading contract:
  request_duck() / request_stop() / request_resume() are realtime-safe (a bool assignment). They
  are called directly from the VAD's input callback so nothing is queued between detection and
  action.
  The PortAudio *output* callback applies the change and stamps the moment it took effect.
  A watcher thread emits the trace event using that stamped time.

So the timestamp on AudioDucked / AudioStopped is when the callback actually changed the outgoing
sample block -- not when we asked, and not when we got round to logging it.

What is still NOT included: audio already buffered inside the OS/driver past the callback
boundary. That residual is reported as `stream_output_latency_ms` on the AudioStopped event so the
gap is visible rather than hidden. See MEMORY.md for this limitation.
"""

from __future__ import annotations

import os
import threading
from collections import deque

import numpy as np
import sounddevice as sd

from ..events import EventType
from ..trace import Trace, now_ms

DUCK_GAIN = 0.15  # attenuation on speech onset; not silence -- the user still hears context


def preferred_output_device() -> int | None:
    """Prefer WASAPI on Windows: measured 22 ms device latency vs 100 ms on MME.

    That residual is audio already queued past the callback, so it directly bounds how quickly the
    user actually stops hearing the agent. Returns None (PortAudio default) if WASAPI is absent.

    `AETHER_OUTPUT_DEVICE` overrides this. That matters: WASAPI's default endpoint is not
    necessarily the one Windows is sending your audio to, and picking it silently is a good way to
    play a perfect reply into a speaker nobody is listening to.
    """
    override = (os.environ.get("AETHER_OUTPUT_DEVICE") or "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            return None
    try:
        for host in sd.query_hostapis():
            if host["name"] == "Windows WASAPI" and host["default_output_device"] >= 0:
                return int(host["default_output_device"])
    except Exception:
        pass
    return None


def negotiate_output(device: int | None, preferred_rate: int | None = None) -> tuple[int | None, int]:
    """Find a (device, samplerate) pair the machine will actually accept.

    A hardcoded rate is a real hazard here. WASAPI shared mode only opens at the endpoint's own
    native rate -- measured on this machine, the WASAPI speaker accepts 48000 and REFUSES 44100 --
    so a device whose native rate is 44100 (headphones commonly are) would make a fixed 48000 raise
    at startup and take the whole agent down with it.

    So: ask the device what it wants, try that, and fall back to the PortAudio default rather than
    failing. Rime is then asked for whatever rate we settled on -- 44100 and 48000 are both in its
    verified supported list -- which keeps the format matched end to end instead of resampling.
    """
    candidates: list[tuple[int | None, int]] = []
    for dev in (device, None):
        rates: list[int] = []
        if preferred_rate:
            rates.append(preferred_rate)
        try:
            native = int(round(sd.query_devices(dev if dev is not None else sd.default.device[1])
                               ["default_samplerate"]))
            rates.append(native)
        except Exception:
            pass
        rates += [48000, 44100]
        for rate in dict.fromkeys(rates):          # de-dup, order preserved
            candidates.append((dev, rate))

    for dev, rate in candidates:
        try:
            probe = sd.OutputStream(samplerate=rate, channels=1, dtype="int16", device=dev,
                                    blocksize=rate // 100, latency="low")
            probe.close()
            return dev, rate
        except Exception:
            continue
    return device, preferred_rate or 48000


class AudioGate:
    """One always-open output stream for the whole session.

    Kept open so duck/stop latency measures the gate, not PortAudio stream startup.
    """

    def __init__(
        self,
        trace: Trace,
        samplerate: int = 48000,   # WASAPI shared mode requires the device's native rate
        device: int | None = None,
        blocksize: int = 480,      # 10 ms at 48 kHz
        duck_gain: float = DUCK_GAIN,
    ):
        if device is None:
            device = preferred_output_device()
        # Negotiate rather than assert: a device that cannot open at `samplerate` would otherwise
        # raise on open() and kill the session before a single word is spoken.
        device, samplerate = negotiate_output(device, samplerate)
        blocksize = max(1, samplerate // 100)      # keep the 10 ms block regardless of rate
        self.trace = trace
        self.samplerate = samplerate
        self.device = device
        self.blocksize = blocksize
        self.duck_gain = duck_gain

        # Every queued chunk carries the generation that produced it. A bare PCM queue cannot
        # distinguish "audio the user still wants" from "audio the user already retracted".
        self._queue: deque[tuple[str | None, np.ndarray]] = deque()
        self._cursor = 0

        # The only generation permitted to reach the speaker. Revoked synchronously on a fence,
        # BEFORE the flush is requested, so no producer can slip a chunk in behind the flush.
        self._active_gen: str | None = None
        # Chunks the callback refused, drained by the watcher into ResultDiscarded events.
        self._drops: deque[tuple[str | None, int]] = deque()
        # WHEN AUDIO ACTUALLY LEFT, as opposed to when Rime's first chunk arrived.
        #
        # `turn_latency_ms` used to end at "the TTS client accepted a chunk", which is upstream of
        # this queue and of the device buffer -- so it measured time-to-first-chunk-received and was
        # described as time-to-first-sound. This is the last point the application can observe: the
        # moment the callback hands real samples to PortAudio.
        #
        # It is NOT the moment the caller hears them. The device buffer still sits after it, and its
        # size is reported separately as `device_latency_ms` rather than folded in, because adding
        # an unobserved constant to a measured number makes it look measured.
        self._first_out: tuple[str | None, float] | None = None

        # control flags read by the audio callback
        self._ducked = False
        self._duck_pending = False
        self._stop_pending = False
        self._resume_pending = False

        # (applied_t, requested_t, reason) slots filled by the callback, drained by the watcher
        self._duck_applied: tuple[float, float, str] | None = None
        self._stop_applied: tuple[float, float, str] | None = None
        self._resume_applied: tuple[float, float, str] | None = None
        self._duck_req_t = 0.0
        self._stop_req_t = 0.0
        self._resume_req_t = 0.0
        self._duck_reason = ""
        self._stop_reason = ""

        self._turn_id: int | None = None
        self._gen: str | None = None

        self._watching = False
        self._watcher: threading.Thread | None = None
        self._emitted = threading.Event()

        self._stream = sd.OutputStream(
            samplerate=samplerate,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            device=device,
            latency="low",
            callback=self._callback,
        )

    # --- lifecycle ------------------------------------------------------------------

    def open(self) -> None:
        self._stream.start()
        self._watching = True
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="audiogate-watch")
        self._watcher.start()

    def close(self) -> None:
        self._watching = False
        if self._watcher is not None:
            self._watcher.join(timeout=1.0)
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    @property
    def stream_output_latency_ms(self) -> float:
        return float(self._stream.latency) * 1000.0

    @property
    def is_playing(self) -> bool:
        return bool(self._queue)

    def first_output_ms(self, gen: str | None) -> float | None:
        """When the callback first handed real samples for `gen` to the device, or None.

        The closest point the application can observe to "the caller started hearing this". None
        when the generation never reached the speaker at all -- which is the correct answer for a
        fenced turn, and is why this returns None rather than 0.
        """
        if self._first_out is None or self._first_out[0] != gen:
            return None
        return self._first_out[1]

    @property
    def device_latency_ms(self) -> float | None:
        """The output device's own buffer, in ms, as PortAudio reports it.

        Reported ALONGSIDE the measured latency, never added to it: it is the tail this process
        cannot observe, and folding an unobserved constant into a measured number would make the
        result look more precise than it is.
        """
        stream = self._stream
        latency = getattr(stream, "latency", None) if stream is not None else None
        if isinstance(latency, (int, float)):
            return round(float(latency) * 1000.0, 1)
        return None

    @property
    def is_ducked(self) -> bool:
        return self._ducked

    # --- audio callback (realtime thread: no allocation-heavy work, no IO, no locks) --

    def _callback(self, outdata, frames, time_info, status):  # noqa: ARG002
        if self._stop_pending:
            self._stop_pending = False
            # A stop supersedes any duck/resume still pending: applying them afterwards would
            # emit AudioDucked *after* AudioStopped and make the trace read as though the agent
            # ducked audio it had already cut.
            self._duck_pending = False
            self._resume_pending = False
            self._queue.clear()
            self._cursor = 0
            self._ducked = False
            self._stop_applied = (now_ms(), self._stop_req_t, self._stop_reason)
            outdata[:] = 0
            return

        if self._duck_pending:
            self._duck_pending = False
            self._ducked = True
            self._duck_applied = (now_ms(), self._duck_req_t, self._duck_reason)

        if self._resume_pending:
            self._resume_pending = False
            self._ducked = False
            self._resume_applied = (now_ms(), self._resume_req_t, "")

        gain = self.duck_gain if self._ducked else 1.0

        need = frames
        out = np.zeros(frames, dtype=np.int16)
        pos = 0
        while need > 0 and self._queue:
            gen, chunk = self._queue[0]
            # Defence in depth: enqueue() already refuses fenced generations and a fence flushes
            # the queue, but with streaming a chunk can still be in flight across that boundary.
            # A fenced generation's audio must never reach the speaker, so drop it here too.
            if gen is not None and gen != self._active_gen:
                self._queue.popleft()
                self._cursor = 0
                self._drops.append((gen, len(chunk)))
                continue
            avail = len(chunk) - self._cursor
            take = min(avail, need)
            out[pos:pos + take] = chunk[self._cursor:self._cursor + take]
            self._cursor += take
            pos += take
            need -= take
            if self._cursor >= len(chunk):
                self._queue.popleft()
                self._cursor = 0

        # `pos > 0` means real samples were copied this block, not silence padding. Stamped once per
        # generation, on the realtime thread, with the same plain-assignment discipline the duck and
        # stop marks already use -- no lock, no allocation.
        if pos > 0 and (self._first_out is None or self._first_out[0] != self._active_gen):
            self._first_out = (self._active_gen, now_ms())

        if gain != 1.0:
            out = (out.astype(np.float32) * gain).astype(np.int16)
        outdata[:, 0] = out

    # --- watcher thread: turns applied timestamps into trace events -------------------

    def _watch(self) -> None:
        while self._watching:
            while self._drops:
                gen, samples = self._drops.popleft()
                self.trace.emit(
                    EventType.RESULT_DISCARDED,
                    gen=gen,
                    active_gen=self._active_gen,
                    reason="stale_audio_chunk",
                    samples=samples,
                    audio_ms=round(samples / self.samplerate * 1000.0, 1),
                )

            slot = self._duck_applied
            if slot is not None:
                self._duck_applied = None
                applied_t, req_t, reason = slot
                self.trace.emit(
                    EventType.AUDIO_DUCKED,
                    turn_id=self._turn_id,
                    gen=self._gen,
                    t=applied_t,
                    reason=reason,
                    gain=self.duck_gain,
                    request_to_apply_ms=round(applied_t - req_t, 3),
                )
                self._emitted.set()

            slot = self._resume_applied
            if slot is not None:
                self._resume_applied = None
                applied_t, req_t, _ = slot
                self.trace.emit(
                    EventType.AUDIO_RESUMED,
                    turn_id=self._turn_id,
                    gen=self._gen,
                    t=applied_t,
                    request_to_apply_ms=round(applied_t - req_t, 3),
                )
                self._emitted.set()

            slot = self._stop_applied
            if slot is not None:
                self._stop_applied = None
                applied_t, req_t, reason = slot
                self.trace.emit(
                    EventType.AUDIO_STOPPED,
                    turn_id=self._turn_id,
                    gen=self._gen,
                    t=applied_t,
                    reason=reason,
                    request_to_apply_ms=round(applied_t - req_t, 3),
                    stream_output_latency_ms=round(self.stream_output_latency_ms, 2),
                )
                self._emitted.set()

            sd.sleep(1)

    # --- control --------------------------------------------------------------------

    def set_active_generation(self, gen: str | None, *, turn_id: int | None = None) -> None:
        """Declare which generation may now reach the speaker. Call before feeding its chunks."""
        self._active_gen = gen
        self._gen = gen
        self._turn_id = turn_id

    def fence_generation(self, gen: str | None, *, reason: str = "fenced") -> None:
        """Atomically stop a generation from ever being heard again.

        Order matters and is the whole point:
          1. revoke the active generation SYNCHRONOUSLY, on this thread, so from this instant no
             producer can enqueue another chunk for it (closes the enqueue-after-fence race);
          2. request the flush -- the callback discards everything still queued and emits silence;
          3. anything that slips through anyway is dropped by the callback's generation check.
        Only after this should the caller set the new generation active and start feeding it.
        """
        if self._active_gen == gen or gen is None:
            self._active_gen = None
        self.request_stop(reason=reason)

    def enqueue(
        self, pcm: np.ndarray, *, turn_id: int | None = None, gen: str | None = None
    ) -> bool:
        """Queue int16 mono PCM for playback. Returns False if the audio was refused as stale.

        This is the audio-side expression of the golden invariant: a fenced generation's audio is
        discarded here rather than played. A refusal is recorded as ResultDiscarded, so a caller
        must not emit ResponseSpoken when this returns False -- nothing was spoken.
        """
        if pcm.dtype != np.int16:
            raise TypeError(f"AudioGate expects int16 PCM, got {pcm.dtype}")

        if gen is not None and gen != self._active_gen:
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=turn_id,
                gen=gen,
                active_gen=self._active_gen,
                reason="stale_generation",
                samples=len(pcm),
                audio_ms=round(len(pcm) / self.samplerate * 1000.0, 1),
            )
            return False

        self._turn_id = turn_id
        self._gen = gen
        self._queue.append((gen, pcm))
        return True

    # realtime-safe requests -- callable directly from the VAD input callback
    def request_duck(self, reason: str = "speech_onset") -> None:
        if self._ducked or self._duck_pending:
            return
        self._duck_reason = reason
        self._duck_req_t = now_ms()
        self._duck_pending = True

    def request_resume(self) -> None:
        if not self._ducked or self._resume_pending:
            return
        self._resume_req_t = now_ms()
        self._resume_pending = True

    def request_stop(self, reason: str = "meaningful_interruption") -> None:
        if self._stop_pending:
            return
        self._stop_reason = reason
        self._stop_req_t = now_ms()
        self._stop_pending = True

    # blocking convenience wrappers (tests / main thread)
    def duck(self, reason: str = "speech_onset", timeout: float = 0.5) -> bool:
        self._emitted.clear()
        self.request_duck(reason)
        return self._emitted.wait(timeout)

    def resume(self, timeout: float = 0.5) -> bool:
        self._emitted.clear()
        self.request_resume()
        return self._emitted.wait(timeout)

    def stop(self, reason: str = "meaningful_interruption", timeout: float = 0.5) -> bool:
        self._emitted.clear()
        self.request_stop(reason)
        return self._emitted.wait(timeout)

    def wait_until_drained(self, timeout: float = 60.0) -> bool:
        deadline = now_ms() + timeout * 1000.0
        while now_ms() < deadline:
            if not self.is_playing:
                sd.sleep(int(self.blocksize / self.samplerate * 1000) + 5)
                return True
            sd.sleep(5)
        return False
