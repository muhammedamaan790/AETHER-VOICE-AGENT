"""Rime TTS client -- the primary and sole speech provider in the judged path (RULES.md R9).

NOTHING about Rime's catalog is invented here. Endpoint, model, voice and language all come from
the environment and this module refuses to run without them. There is deliberately **no fallback
TTS provider**: R9.4 forbids silent fallback, and the cleanest way to honour that is to have
nothing to fall back to. If Rime is unconfigured, the agent does not speak and says so loudly.

VERIFIED CONTRACT (confirmed by a human against a live 200 response that played real audio):

    POST https://users.rime.ai/v1/rime-tts
    Authorization: Bearer <RIME_API_KEY>
    Content-Type: application/json
    Accept: audio/mp3
    body: {"text": ..., "modelId": "mistv2", "speaker": "astra", "lang": "eng"}
    response: raw MP3 bytes in the body

The four non-secret values stay configuration rather than literals (RULES.md R9.5); the verified
values live in .env / .env.example. No other field or endpoint is sent.
"""

from __future__ import annotations

import io

import av
import httpx
import numpy as np

from ..config import RimeConfig
from ..trace import Trace, now_ms

# Rime returns MP3. The container carries its own sample rate, so nothing is assumed here --
# audio is decoded and resampled to whatever rate the AudioGate is running at.
ACCEPT_AUDIO = "audio/mp3"


class RimeNotConfigured(RuntimeError):
    """Raised when Rime config is absent. Never silently substituted with another provider."""


class RimeTTS:
    name = "rime"

    def __init__(self, trace: Trace, config: RimeConfig | None = None, timeout: float = 30.0):
        self.trace = trace
        self.config = config or RimeConfig.from_env()
        self.timeout = timeout
        self.last_latency_ms: float | None = None
        self.last_source_samplerate: int | None = None

    # --- configuration ---------------------------------------------------------------

    def missing_config(self) -> list[str]:
        c = self.config
        pairs = {
            "RIME_API_KEY": c.api_key,
            "RIME_API_URL": c.api_url,
            "RIME_MODEL": c.model,
            "RIME_VOICE": c.voice,
            "RIME_LANGUAGE": c.language,
        }
        return [k for k, v in pairs.items() if not v]

    @property
    def configured(self) -> bool:
        return not self.missing_config()

    def require(self) -> None:
        missing = self.missing_config()
        if missing:
            raise RimeNotConfigured(
                "Rime is not configured; missing " + ", ".join(missing) + ". "
                "Values must come from a human verifying the live Rime catalog "
                "(RIME_EVIDENCE.md Part 2). They must not be guessed."
            )

    # --- synthesis --------------------------------------------------------------------

    def _build_payload(self, text: str) -> dict:
        """The verified request body. Exactly these four fields -- nothing else is sent."""
        return {
            "text": text,
            "modelId": self.config.model,
            "speaker": self.config.voice,
            "lang": self.config.language,
        }

    def synthesize(
        self,
        text: str,
        *,
        target_samplerate: int,
        turn_id: int | None = None,
        gen: str | None = None,
    ) -> np.ndarray:
        """text -> int16 mono PCM at target_samplerate. Raises RimeNotConfigured if unconfigured.

        Deliberately emits NO event. `ResponseSpoken` means audio was committed to the speaker, and
        ARCHITECTURE.md section 7 puts that emission at the Output Gate -- synthesising audio that
        is then discarded as stale must never look like speech in the trace. The caller emits it.
        `last_latency_ms` carries the synthesis timing for whoever does.
        """
        self.require()
        t0 = now_ms()
        resp = httpx.post(
            str(self.config.api_url),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": ACCEPT_AUDIO,
            },
            json=self._build_payload(text),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        pcm, source_sr = decode_mp3(resp.content, target_samplerate)
        self.last_source_samplerate = source_sr
        self.last_latency_ms = round(now_ms() - t0, 1)
        return pcm


# --- audio helpers --------------------------------------------------------------------

def decode_mp3(data: bytes, target_samplerate: int) -> tuple[np.ndarray, int]:
    """Raw MP3 bytes -> (int16 mono PCM at target_samplerate, source sample rate).

    Uses PyAV, which ships its own FFmpeg libraries -- no external ffmpeg binary is needed, and it
    is already a faster-whisper dependency. Resampling happens inside FFmpeg rather than through
    the crude linear interpolator used for test fixtures.
    """
    with av.open(io.BytesIO(data), mode="r") as container:
        stream = container.streams.audio[0]
        source_sr = int(stream.rate)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=target_samplerate)
        chunks: list[np.ndarray] = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):   # flush
            chunks.append(out.to_ndarray().reshape(-1))

    pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    return pcm.astype(np.int16), source_sr


def resample_int16(pcm: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Linear resample. Adequate for a spike; revisit if quality matters."""
    if src_sr == dst_sr or len(pcm) == 0:
        return pcm.astype(np.int16)
    n_out = int(round(len(pcm) * dst_sr / src_sr))
    x_in = np.linspace(0.0, 1.0, num=len(pcm), endpoint=False)
    x_out = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_out, x_in, pcm.astype(np.float32)).astype(np.int16)
