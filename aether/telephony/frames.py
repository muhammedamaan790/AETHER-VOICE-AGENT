"""`rtc.AudioFrame` ⇄ `AudioChunk`. The whole of AETHER's LiveKit type dependency, in one file.

This module **does not import LiveKit**, on purpose, and that is not a stylistic choice:

* it is testable in the main environment, where the SDK is not installed;
* the conversion — the part most likely to be subtly wrong about layout, dtype or channel count —
  gets covered by tests long before a phone is involved;
* if LiveKit's frame type ever changes, exactly one file needs revisiting.

Inbound is duck-typed: anything exposing `data`, `sample_rate`, `num_channels` and
`samples_per_channel` converts, which is precisely `rtc.AudioFrame`'s public surface (confirmed
against livekit 1.1.17). Outbound returns the positional arguments `rtc.AudioFrame(...)` takes,
so the caller constructs the SDK object and this file never names it.

Layout note that matters: LiveKit carries **interleaved** int16. For stereo that is
`[L0, R0, L1, R1, …]`, so a naive reshape-free read of a 2-channel frame yields alternating samples
at double the apparent rate — audible as a chipmunk buzz, and easy to ship without noticing.
Telephony is mono in practice, but a caller on a stereo device is not our problem to be surprised
by, so stereo is downmixed rather than assumed away.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..bridge import AudioChunk


def to_chunk(frame: Any) -> AudioChunk:
    """`rtc.AudioFrame` → `AudioChunk`. Downmixes to mono; never copies more than it must.

    Accepts any object with LiveKit's frame attributes, so it needs no import and no isinstance.
    """
    pcm = np.frombuffer(bytes(frame.data), dtype=np.int16)

    channels = int(getattr(frame, "num_channels", 1) or 1)
    if channels > 1:
        # Interleaved -> (samples, channels) -> mono. Averaged in int32 so a loud stereo frame
        # cannot overflow int16 halfway through the mean.
        usable = (len(pcm) // channels) * channels
        pcm = pcm[:usable].reshape(-1, channels).astype(np.int32).mean(axis=1).astype(np.int16)

    return AudioChunk(data=pcm, sample_rate=int(frame.sample_rate))


def to_frame_args(chunk: AudioChunk, *, num_channels: int = 1) -> tuple[bytes, int, int, int]:
    """`AudioChunk` → the positional arguments of `rtc.AudioFrame(data, rate, channels, spc)`.

    Returning arguments rather than a frame is what keeps this file SDK-free. The call site does:

        rtc.AudioFrame(*to_frame_args(chunk))

    `samples_per_channel` is frames-per-channel, not total samples — LiveKit rejects a mismatch,
    and getting it wrong on mono is invisible until the first stereo sink.
    """
    pcm = np.ascontiguousarray(chunk.data, dtype=np.int16)
    if num_channels > 1:
        pcm = np.repeat(pcm, num_channels)          # mono -> interleaved duplicate channels
    samples_per_channel = len(pcm) // num_channels
    return pcm.tobytes(), int(chunk.sample_rate), int(num_channels), int(samples_per_channel)
