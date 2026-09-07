"""Audio adapters: carry someone else's audio transport into and out of AETHER's pipeline.

Written for LiveKit's phone path, but **this module does not import LiveKit** and never will. It
speaks a tiny frame shape (`AudioChunk`) that mirrors `rtc.AudioFrame` — int16 mono plus a sample
rate — so the whole bridge is testable in the main environment, where LiveKit is not installed. A
thin converter at the call site turns `rtc.AudioFrame` into `AudioChunk` and back; every telephony
assumption lives there, on the far side of this boundary.

The point of the boundary is that AETHER's correctness does **not** move. VAD, endpointing, the
noise gates, generation tagging, ducking and fencing all live inside two functions that know nothing
about where audio came from:

    inbound   AudioChunk ──resample──rebuffer──▶ MicVAD.process_frame   (unchanged)
    outbound  AudioGate._callback ──resample──▶ AudioChunk              (unchanged)

Nothing here reimplements any of it. If a fence works on the local microphone it works on a phone
call, because it is the same code either side of the swap.

Two traps this module exists to absorb, both found by reading the code rather than by debugging a
call at 2am:

**Frame size.** `MicVAD.process_frame` returns *silently* when handed anything other than exactly
`frame_samples` (320 at 16 kHz/20 ms). LiveKit delivers whatever its encoder produces — commonly
480 or 960 samples — so feeding frames straight through would drop every one of them with no error,
no exception and no log line. `InboundBridge` rebuffers into exact frames and carries the remainder.

**The listening gate.** `MicVAD._callback` checks `self._enabled` before calling `process_frame`;
`process_frame` itself does not. Calling it directly therefore *bypasses* STANDBY / Stop Listening
entirely. `InboundBridge` checks `mic.listening` itself, so the control means the same thing on a
phone call as it does on a laptop.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..audio.player import AudioGate
from ..audio.rime import resample_int16
from ..audio.vad import MicVAD


@dataclass(frozen=True)
class AudioChunk:
    """One block of mono 16-bit PCM, plus the rate it is at.

    Deliberately the smallest thing that can stand in for `livekit.rtc.AudioFrame` without
    importing it: `data` is what `AudioFrame.data` holds as int16, and `sample_rate` matches.
    Channel count is not carried because AETHER is mono end to end -- a stereo source is mixed
    down at the call site, not here.
    """

    data: np.ndarray          # int16, shape (n,)
    sample_rate: int

    @property
    def samples(self) -> int:
        return int(len(self.data))

    @property
    def duration_ms(self) -> float:
        return len(self.data) / self.sample_rate * 1000.0


class InboundBridge:
    """Transport audio → `MicVAD`. Resamples, rebuffers into exact frames, honours the mic gate."""

    def __init__(self, mic: MicVAD, *, source_rate: int = 48000):
        self.mic = mic
        self.source_rate = source_rate
        # Samples left over when a source block does not divide evenly into VAD frames. Carrying
        # them is what keeps the audio continuous: dropping the remainder would clip a few
        # milliseconds off every block and shred the speech the VAD is trying to measure.
        self._carry = np.zeros(0, dtype=np.int16)
        self.frames_delivered = 0
        self.frames_dropped_not_listening = 0

        # --- level metering ------------------------------------------------------------
        # Cheap enough to run on every block (one numpy pass over ~1000 samples) and the only way
        # to answer the question that will actually be asked after the first call: was there any
        # audio at all, and if there was, how loud was it compared with the floor the VAD is
        # using? Without this, "AETHER did not respond" has half a dozen indistinguishable causes.
        #
        # Thresholds are NOT touched here. This measures; recalibration is a separate, deliberate
        # act performed against real call audio.
        self.chunks_received = 0
        self.samples_received = 0
        self.peak_rms = 0.0             # loudest block seen -- the speech level
        self.floor_rms: float | None = None   # quietest block seen -- the ambient level
        self._rms_sum = 0.0

    def push(self, chunk: AudioChunk) -> int:
        """Feed one transport block in. Returns how many VAD frames it produced.

        Returns 0 while the mic is in standby, and counts those separately, so "no speech was
        detected" and "we were not listening" never look the same in diagnostics.
        """
        self.chunks_received += 1
        self.samples_received += int(len(chunk.data))
        if len(chunk.data):
            block = np.asarray(chunk.data, dtype=np.float32)
            rms = float(np.sqrt(np.mean(block * block)))
            self._rms_sum += rms
            self.peak_rms = max(self.peak_rms, rms)
            self.floor_rms = rms if self.floor_rms is None else min(self.floor_rms, rms)

        if not self.mic.listening:
            # Drop the carry too: audio from before a mute must not be stitched onto audio from
            # after it, exactly as `MicVAD.set_listening(False)` resets the detector.
            self._carry = np.zeros(0, dtype=np.int16)
            self.frames_dropped_not_listening += 1
            return 0

        pcm = np.asarray(chunk.data, dtype=np.int16).reshape(-1)
        if chunk.sample_rate != self.mic.samplerate:
            pcm = resample_int16(pcm, chunk.sample_rate, self.mic.samplerate)

        buf = np.concatenate((self._carry, pcm)) if len(self._carry) else pcm

        size = self.mic.frame_samples
        n_full = len(buf) // size
        for i in range(n_full):
            self.mic.process_frame(buf[i * size:(i + 1) * size])
        self._carry = buf[n_full * size:].copy()
        self.frames_delivered += n_full
        return n_full

    @property
    def mean_rms(self) -> float:
        """Average block level over the call. Between `floor_rms` and `peak_rms` by construction."""
        return self._rms_sum / self.chunks_received if self.chunks_received else 0.0

    def diagnostics(self) -> dict:
        """What actually arrived. Measurements only -- no verdict, no threshold, no advice."""
        return {
            "chunks_received": self.chunks_received,
            "samples_received": self.samples_received,
            "audio_ms": round(self.samples_received / self.source_rate * 1000.0, 1),
            "frames_delivered": self.frames_delivered,
            "frames_dropped_not_listening": self.frames_dropped_not_listening,
            "peak_rms": round(self.peak_rms, 1),
            "mean_rms": round(self.mean_rms, 1),
            "floor_rms": None if self.floor_rms is None else round(self.floor_rms, 1),
            "source_rate": self.source_rate,
            "vad_rate": self.mic.samplerate,
            "speech_floor": round(float(self.mic.speech_floor()), 1),
        }

    def reset(self) -> None:
        """Drop buffered audio. Call on call teardown so a new call starts clean."""
        self._carry = np.zeros(0, dtype=np.int16)


class OutboundBridge:
    """`AudioGate` → transport audio. Drives the real callback, so fencing is preserved.

    The generation tag on every queued chunk, the duck/stop handling and the flush-on-fence all
    happen inside `AudioGate._callback`. Calling that callback -- rather than reading the queue --
    is what makes stale audio impossible to leak onto a phone call: a fenced generation's samples
    are dropped in exactly the place they are dropped for a local speaker.
    """

    def __init__(self, gate: AudioGate, *, sink_rate: int = 48000):
        self.gate = gate
        self.sink_rate = sink_rate
        self.blocks_pulled = 0
        # A pump that runs perfectly while publishing nothing but silence looks identical to a
        # pump that is not running at all, unless this is counted separately.
        self.blocks_with_audio = 0
        self.samples_audible = 0

    @property
    def block_ms(self) -> float:
        """How much audio one `pull()` represents. The caller paces on this."""
        return self.gate.blocksize / self.gate.samplerate * 1000.0

    def pull(self) -> AudioChunk:
        """Produce one block of outbound audio. Silence when nothing is queued.

        Always returns a full block, never None: a phone call needs a continuous stream, and a gap
        in the audio is heard as a dropout rather than as silence.
        """
        out = np.zeros((self.gate.blocksize, 1), dtype=np.int16)
        self.gate._callback(out, self.gate.blocksize, None, None)
        self.blocks_pulled += 1

        pcm = out.reshape(-1)
        audible = int(np.count_nonzero(pcm))
        if audible:
            self.blocks_with_audio += 1
            self.samples_audible += audible

        if self.sink_rate != self.gate.samplerate:
            pcm = resample_int16(pcm, self.gate.samplerate, self.sink_rate)
        return AudioChunk(data=pcm, sample_rate=self.sink_rate)


    def diagnostics(self) -> dict:
        """What actually left. `blocks_with_audio == 0` after a spoken turn is an outbound fault."""
        return {
            "blocks_pulled": self.blocks_pulled,
            "blocks_with_audio": self.blocks_with_audio,
            "samples_audible": self.samples_audible,
            "audible_ms": round(self.samples_audible / self.gate.samplerate * 1000.0, 1),
            "sink_rate": self.sink_rate,
            "gate_rate": self.gate.samplerate,
        }


def wav_to_chunks(pcm: np.ndarray, sample_rate: int, *, block_ms: float = 20.0) -> list[AudioChunk]:
    """Slice PCM into transport-shaped blocks, for driving the bridge without a call.

    Used by the tests and by the offline harness so the plumbing can be debugged before a phone is
    in the loop. The final partial block is kept rather than discarded — a real transport delivers
    whatever is left when the far end stops talking, and the bridge must cope with it.
    """
    size = max(1, int(round(sample_rate * block_ms / 1000.0)))
    pcm = np.asarray(pcm, dtype=np.int16).reshape(-1)
    return [AudioChunk(data=pcm[i:i + size], sample_rate=sample_rate)
            for i in range(0, len(pcm), size)]
