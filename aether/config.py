"""Configuration placeholders for AETHER.

No value here is invented. Anything unverified is None and says so. Rime endpoint/model/voice/
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

    VERIFIED against Rime's live public catalogue, twice (RIME_EVIDENCE.md Parts 1, 1a and 1c):
    `mistv3`/`astra`/`eng` for English and Spanish (`isa`), `coda`/`nadi`/`hin` for Hindi. Re-check
    before a demo -- the catalogue is not stable: Rime deleted the whole `arcana` model overnight on
    2026-09-09, taking the Hindi voice this project had been using with it.

    Still outstanding and NOT verified: organizer preflight and account rate limits, which are a
    human task.
    """

    # repr=False: a dataclass repr prints every field, so a traceback, a log line or a
    # config object landing in a trace field would render the key in plaintext. PHASES.md
    # Day 6: "Never show credentials, not even for one frame." Presence is still
    # reportable -- `missing_config()` answers by NAME, never by value.
    api_key: str | None = field(default=None, repr=False)
    # All four verified against the live catalogue; see the class docstring. They stay
    # configuration rather than literals so a voice can be changed without a code change (R9.4).
    api_url: str | None = None
    model: str | None = None
    voice: str | None = None
    language: str | None = None

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
    """faster-whisper, running locally on CPU. Chosen and in use.

    `base.en` for English -- monolingual, and measurably faster and more accurate on English than
    the multilingual model of the same size ("aisle 9" vs "IL-9"). Hindi and Spanish need the
    multilingual `base`, which `aether/lang` selects and `aether/prewarm` warms.
    """

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
    """Reasoning and the general-knowledge path. (Classification is deterministic and uses no model.)

    Provider CHOSEN: Gemini, `gemini-flash-lite-latest`, by measurement rather than preference --
    see `aether/llm.py`, where the alternatives and their failure rates are recorded. Four providers
    remain implemented behind one interface so the choice is reversible.
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

    # DEAD, and kept only so an existing `.env` naming it does not look like it does something.
    # The classifier turned out to need no threshold: it is closed-set and whole-utterance, so it
    # either matches a table or falls through to REPLACEMENT, which fences. The fail-safe is the
    # default branch rather than a comparison (RULES.md R2.1, amended).
    classifier_confidence_threshold: float | None = None

    # WebRTC VAD, chosen and in use. These stay None so the code's own defaults win unless an
    # operator overrides them; the speech floor that actually needed tuning is AETHER_SPEECH_FLOOR,
    # re-derived from real telephony audio (RIME_EVIDENCE Part 6).
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
