"""Rime streaming TTS over the /ws3 WebSocket, plus the HTTP client kept as a fallback transport.

Rime remains the sole TTS provider (RULES.md R9.1). This changes only the *transport*: instead of
waiting for one complete MP3 before any sound exists, audio chunks are pushed into the existing
AudioGate as they arrive, so the agent starts speaking while Rime is still synthesising.

VERIFIED /ws3 protocol (docs.rime.ai/docs/websockets + the mistv3 websockets-json reference; the
two agree, and a third-party working client matches):

    connect   wss://users-ws.rime.ai/ws3?speaker=&modelId=&audioFormat=pcm&samplingRate=&lang=
              header: Authorization: Bearer <RIME_API_KEY>
    send      {"text": "...", "contextId": "..."}
    interrupt {"operation": "clear"}
    (eos)     {"operation": "eos"} exists but is NOT used: measured 2026-09-06, it makes the
              server close the connection, forcing a fresh ~920 ms handshake every turn. Without
              it the utterance still synthesises and still ends with "done", so the connection is
              reused across turns instead.
    recv      {"type": "chunk", "data": <base64>, "contextId": ...}
              {"type": "timestamps", "word_timestamps": {...}}
              {"type": "done", "contextId": ...}
              {"type": "error", "message": "..."}

`audioFormat=pcm` at the AudioGate's own sample rate means no MP3 decode and no resample -- the
bytes go straight to the speaker. Rime chops the PCM stream on arbitrary byte boundaries, so a
chunk can end mid-sample; the trailing odd byte is carried into the next chunk.

FENCING IS UNCHANGED. This module never decides what may be heard. It asks `is_valid()` before
every chunk and hands each chunk to `AudioGate.enqueue(gen=...)`, which is still the final
authority and still refuses audio from a non-active generation. `clear` is a courtesy to Rime --
it stops synthesis work -- not a safety mechanism.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import numpy as np

from ..config import RimeConfig
from ..trace import now_ms
from .rime import RimeNotConfigured, RimeTTS

# VERIFIED endpoint; overridable so it stays configuration, not a literal (RULES.md R9.5).
DEFAULT_WS_URL = "wss://users-ws.rime.ai/ws3"

# Verified supported: 8000, 16000, 22050, 24000, 44100, 48000, 96000.
SUPPORTED_SAMPLE_RATES = (8000, 16000, 22050, 24000, 44100, 48000, 96000)


@dataclass
class SpeakResult:
    """Outcome of one spoken turn."""

    accepted: bool          # did any audio actually reach the gate?
    completed: bool         # did Rime finish the utterance?
    samples: int = 0
    first_audio_ms: float | None = None   # send -> first chunk accepted by the gate
    reason: str = ""

    @property
    def audio_ms(self) -> float:
        return 0.0


class RimeStreamingTTS:
    """Streams Rime audio into the AudioGate chunk by chunk."""

    name = "rime"           # the provider is still Rime (R9.3)
    transport = "ws3"

    def __init__(
        self,
        trace,
        config: RimeConfig | None = None,
        samplerate: int = 48000,
        timeout: float = 30.0,
    ):
        self.trace = trace
        self.config = config or RimeConfig.from_env()
        self.samplerate = samplerate
        self.timeout = timeout
        self.ws_url = (os.environ.get("RIME_WS_URL") or "").strip() or DEFAULT_WS_URL
        self.last_latency_ms: float | None = None
        self.last_first_audio_ms: float | None = None
        self.last_reused_connection = False
        self._ws = None          # cached across turns; see _get_ws()

    # --- configuration (same contract as the HTTP client) -----------------------------

    def missing_config(self) -> list[str]:
        c = self.config
        pairs = {
            "RIME_API_KEY": c.api_key,
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

    def _url(self) -> str:
        params = {
            "speaker": self.config.voice,
            "modelId": self.config.model,
            "audioFormat": "pcm",
            "samplingRate": self.samplerate,
            "lang": self.config.language,
        }
        return f"{self.ws_url}?{urlencode(params)}"

    # --- speaking ----------------------------------------------------------------------

    def speak(
        self,
        text: str,
        *,
        gate,
        gen: str | None,
        turn_id: int | None = None,
        is_valid: Callable[[], bool] | None = None,
    ) -> SpeakResult:
        """Stream `text` into `gate`, stopping early if `is_valid()` turns False.

        Returns without raising for protocol/transport failures: a TTS failure must not end the
        voice session. Genuine misconfiguration still raises RimeNotConfigured.
        """
        self.require()

        still_valid = is_valid or (lambda: True)
        t0 = now_ms()
        samples = 0
        first_audio_ms: float | None = None
        carry = b""
        completed = False
        reason = ""

        try:
            ws = self._get_ws()
            try:
                ws.send(json.dumps({"text": text}))
            except Exception:
                # The cached socket died while idle. Reconnect once and send again -- a stale
                # cache must cost a reconnect, never a lost turn.
                self._drop_ws()
                ws = self._get_ws()
                ws.send(json.dumps({"text": text}))

            # Deliberately NO {"operation": "eos"}: measured 2026-09-06, eos makes the server
            # close the connection (1005), which is what forced a fresh handshake every turn.
            # Without it the utterance still synthesises and still ends with "done".
            if True:
                while True:
                    if not still_valid():
                        # Fenced mid-speech. Tell Rime to stop synthesising, then stop feeding.
                        # The gate would refuse these chunks anyway; this just saves the work.
                        self._clear(ws)
                        self._drop_ws()
                        reason = "fenced_midstream"
                        break

                    try:
                        raw = ws.recv(timeout=self.timeout)
                    except TimeoutError:
                        reason = "recv_timeout"
                        break

                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue    # binary/unparseable frame: not part of the JSON protocol

                    kind = msg.get("type")

                    if kind == "chunk":
                        buf = carry + base64.b64decode(msg.get("data") or "")
                        usable = len(buf) - (len(buf) % 2)   # int16: never split a sample
                        carry = buf[usable:]
                        if usable == 0:
                            continue
                        pcm = np.frombuffer(buf[:usable], dtype="<i2")

                        if not still_valid():
                            self._clear(ws)
                            self._drop_ws()
                            reason = "fenced_midstream"
                            break
                        # The gate is the final authority; it refuses a stale generation.
                        if not gate.enqueue(pcm, turn_id=turn_id, gen=gen):
                            self._clear(ws)
                            self._drop_ws()
                            reason = "gate_refused"
                            break

                        samples += len(pcm)
                        if first_audio_ms is None:
                            first_audio_ms = now_ms() - t0

                    elif kind == "done":
                        completed = True
                        break
                    elif kind == "error":
                        reason = f"rime_error: {str(msg.get('message'))[:120]}"
                        self._drop_ws()
                        break
                    # "timestamps" carries word timing only -- nothing to play.

        except RimeNotConfigured:
            raise
        except Exception as exc:
            self._drop_ws()
            reason = f"{type(exc).__name__}"

        self.last_latency_ms = round(now_ms() - t0, 1)
        self.last_first_audio_ms = round(first_audio_ms, 1) if first_audio_ms is not None else None
        return SpeakResult(
            accepted=samples > 0,
            completed=completed,
            samples=samples,
            first_audio_ms=self.last_first_audio_ms,
            reason=reason,
        )

    def _get_ws(self):
        """Cached connection, opened on demand.

        Measured 2026-09-06: the handshake is ~920 ms median while synthesis to first chunk is
        ~520 ms -- so a fresh connection per turn cost nearly twice as much as the speech itself.
        Reuse across turns removes that from every turn after the first.
        """
        if self._ws is not None:
            self.last_reused_connection = True
            return self._ws
        from websockets.sync.client import connect

        self.last_reused_connection = False
        self._ws = connect(
            self._url(),
            additional_headers={"Authorization": f"Bearer {self.config.api_key}"},
            open_timeout=self.timeout,
            close_timeout=2.0,
        )
        return self._ws

    def _drop_ws(self) -> None:
        """Discard the cached connection. Used whenever its state is no longer trustworthy."""
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self) -> None:
        self._drop_ws()

    @staticmethod
    def _clear(ws) -> None:
        """VERIFIED interrupt command. Best-effort: a dead socket must not raise here."""
        try:
            ws.send(json.dumps({"operation": "clear"}))
        except Exception:
            pass


class RimeHttpSpeaker:
    """The original blocking HTTP client behind the same `speak()` interface.

    Kept as an explicit, disclosed fallback transport (RULES.md R9.4). Still Rime, still the only
    TTS provider -- it simply waits for the whole MP3 before any audio exists.
    """

    name = "rime"
    transport = "http"

    def __init__(self, trace, config: RimeConfig | None = None, samplerate: int = 48000):
        self._http = RimeTTS(trace, config=config)
        self.trace = trace
        self.samplerate = samplerate
        self.last_latency_ms: float | None = None
        self.last_first_audio_ms: float | None = None

    @property
    def config(self) -> RimeConfig:
        return self._http.config

    def missing_config(self) -> list[str]:
        return self._http.missing_config()

    @property
    def configured(self) -> bool:
        return self._http.configured

    def require(self) -> None:
        self._http.require()

    def speak(self, text, *, gate, gen, turn_id=None, is_valid=None) -> SpeakResult:
        self.require()
        still_valid = is_valid or (lambda: True)
        t0 = now_ms()
        try:
            pcm = self._http.synthesize(
                text, target_samplerate=self.samplerate, turn_id=turn_id, gen=gen
            )
        except RimeNotConfigured:
            raise
        except Exception as exc:
            self.last_latency_ms = round(now_ms() - t0, 1)
            return SpeakResult(accepted=False, completed=False, reason=type(exc).__name__)

        if not still_valid():
            self.last_latency_ms = round(now_ms() - t0, 1)
            return SpeakResult(accepted=False, completed=False, reason="fenced_midstream")

        accepted = gate.enqueue(pcm, turn_id=turn_id, gen=gen)
        self.last_latency_ms = round(now_ms() - t0, 1)
        # With HTTP there is no partial audio: first sound is only possible once it all arrived.
        self.last_first_audio_ms = self.last_latency_ms if accepted else None
        return SpeakResult(
            accepted=bool(accepted),
            completed=bool(accepted),
            samples=len(pcm) if accepted else 0,
            first_audio_ms=self.last_first_audio_ms,
            reason="" if accepted else "gate_refused",
        )


def build_tts(trace, samplerate: int = 48000):
    """Streaming /ws3 by default; HTTP only when explicitly requested.

    `RIME_TRANSPORT=http` selects the fallback. The choice is recorded on `ResponseSpoken` as
    `transport`, so which path spoke is observable rather than assumed (RULES.md R9.3/R9.4).
    """
    transport = (os.environ.get("RIME_TRANSPORT") or "").strip().lower()
    if transport == "http":
        return RimeHttpSpeaker(trace, samplerate=samplerate)
    return RimeStreamingTTS(trace, samplerate=samplerate)
