"""Configuration placeholders for AETHER.

No value here is invented. Anything unverified is None with a TODO. Rime endpoint/model/voice/
language in particular have NOT been checked against the live catalog -- see RIME_EVIDENCE.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class RimeConfig:
    """Rime is the primary and sole TTS in the judged path (RULES.md R9).

    TODO: verify against Rime live catalog + organizer preflight (human task, Day 1).
    """

    # repr=False: a dataclass repr prints every field, so a traceback, a log line or a
    # config object landing in a trace field would render the key in plaintext. PHASES.md
    # Day 6: "Never show credentials, not even for one frame." Presence is still
    # reportable -- `missing_config()` answers by NAME, never by value.
    api_key: str | None = field(default=None, repr=False)
    api_url: str | None = None      # TODO: verify against Rime live catalog
    model: str | None = None        # TODO: verify against Rime live catalog
    voice: str | None = None        # TODO: verify against Rime live catalog
    language: str | None = None     # TODO: verify against Rime live catalog

    @classmethod
    def from_env(cls) -> "RimeConfig":
        return cls(
            api_key=_env("RIME_API_KEY"),
            api_url=_env("RIME_API_URL"),
            model=_env("RIME_MODEL"),
            voice=_env("RIME_VOICE"),
            language=_env("RIME_LANGUAGE"),
        )


@dataclass(frozen=True)
class SttConfig:
    """TODO: provider not yet chosen (Day 1/2 decision)."""

    provider: str | None = None
    # repr=False: a dataclass repr prints every field, so a traceback, a log line or a
    # config object landing in a trace field would render the key in plaintext. PHASES.md
    # Day 6: "Never show credentials, not even for one frame." Presence is still
    # reportable -- `missing_config()` answers by NAME, never by value.
    api_key: str | None = field(default=None, repr=False)
    model: str | None = None

    @classmethod
    def from_env(cls) -> "SttConfig":
        return cls(
            provider=_env("STT_PROVIDER"),
            api_key=_env("STT_API_KEY"),
            model=_env("STT_MODEL"),
        )


@dataclass(frozen=True)
class LlmConfig:
    """Reasoning, classification, and the general-knowledge path.

    TODO: provider not yet chosen (Day 1 decision).
    """

    provider: str | None = None
    # repr=False: a dataclass repr prints every field, so a traceback, a log line or a
    # config object landing in a trace field would render the key in plaintext. PHASES.md
    # Day 6: "Never show credentials, not even for one frame." Presence is still
    # reportable -- `missing_config()` answers by NAME, never by value.
    api_key: str | None = field(default=None, repr=False)
    model: str | None = None

    @classmethod
    def from_env(cls) -> "LlmConfig":
        return cls(
            provider=_env("LLM_PROVIDER"),
            api_key=_env("LLM_API_KEY"),
            model=_env("LLM_MODEL"),
        )


@dataclass(frozen=True)
class RuntimeConfig:
    trace_dir: str = "traces"
    log_level: str = "INFO"

    # Below this classifier confidence, the supervisor takes the safe branch (fence).
    # TODO: tune from measured classifier behaviour. None until chosen -- not a guessed default.
    classifier_confidence_threshold: float | None = None

    # TODO(Day 2): set once the VAD implementation is chosen.
    vad_aggressiveness: int | None = None
    vad_onset_ms: int | None = None

    # TEST-ONLY. Disables the Output Gate generation check. Never enabled in the judged path.
    # See RULES.md R11.
    unsafe_mode: bool = False

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        threshold = _env("AETHER_CLASSIFIER_CONFIDENCE_THRESHOLD")
        aggressiveness = _env("AETHER_VAD_AGGRESSIVENESS")
        onset_ms = _env("AETHER_VAD_ONSET_MS")
        return cls(
            trace_dir=_env("AETHER_TRACE_DIR") or "traces",
            log_level=_env("AETHER_LOG_LEVEL") or "INFO",
            classifier_confidence_threshold=float(threshold) if threshold else None,
            vad_aggressiveness=int(aggressiveness) if aggressiveness else None,
            vad_onset_ms=int(onset_ms) if onset_ms else None,
            unsafe_mode=_env("AETHER_UNSAFE_MODE") == "1",
        )
