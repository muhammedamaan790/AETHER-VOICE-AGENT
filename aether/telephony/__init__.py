"""Telephony boundary: everything that knows AETHER is on a phone lives under here.

Three files, three jobs:

* `frames.py`   — `rtc.AudioFrame` ⇄ `AudioChunk`. No LiveKit import; fully tested.
* `agent.py`    — the LiveKit worker entrypoint. The only module that imports the SDK.
* this file     — configuration, read from the environment, never hardcoded.

Nothing above this package changes when the transport changes. The pipeline underneath —
`MicVAD`, `WhisperSTT`, the LLM, `RimeStreamingTTS`, `AudioGate`, `GenerationRegistry`,
`BargeInCoordinator` — is used exactly as the local-microphone path uses it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# The agent's registered name. A NAMED agent only ever runs when something explicitly dispatches
# it, which is what a SIP dispatch rule does -- an unnamed worker would pick up every room in the
# project, including ones that have nothing to do with the hotel.
AGENT_NAME = "aether-hotel"

# LiveKit delivers and accepts 48 kHz; AETHER's VAD wants 16 kHz and the AudioGate runs at its own
# negotiated rate. The bridge resamples both ways, so these are the transport's numbers only.
TRANSPORT_SAMPLE_RATE = 48000
TRANSPORT_CHANNELS = 1


@dataclass(frozen=True)
class LiveKitConfig:
    """Credentials for one LiveKit project, read from the environment.

    Swapping projects is three environment variables and nothing else -- no code change, no
    rebuild. `api_secret` is `repr=False` for the same reason the Rime key is: a dataclass repr
    prints every field, and a traceback during a demo must not put a secret on screen.
    """

    url: str | None = None
    api_key: str | None = None
    api_secret: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "LiveKitConfig":
        return cls(
            url=_env("LIVEKIT_URL"),
            api_key=_env("LIVEKIT_API_KEY"),
            api_secret=_env("LIVEKIT_API_SECRET"),
        )

    def missing(self) -> list[str]:
        """Which variables are absent. Reports NAMES only -- never a value, not even a prefix."""
        pairs = {
            "LIVEKIT_URL": self.url,
            "LIVEKIT_API_KEY": self.api_key,
            "LIVEKIT_API_SECRET": self.api_secret,
        }
        return [name for name, value in pairs.items() if not value]

    @property
    def configured(self) -> bool:
        return not self.missing()

    @property
    def project_host(self) -> str:
        """`my-project.livekit.cloud` — safe to print, and the fastest way to catch the classic
        mistake of pointing a worker at the previous project's credentials."""
        if not self.url:
            return "(unset)"
        return self.url.split("//")[-1].split("/")[0]


def _env(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip()
    return value or None
