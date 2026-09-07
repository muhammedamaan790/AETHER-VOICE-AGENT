"""Secrets must never be renderable, not merely never printed on purpose.

PHASES.md Day 6: *"Never show credentials, not even for one frame."* That is a promise about a
live recording, and a promise a code review cannot keep on its own — the dangerous paths are the
ones nobody writes deliberately: a traceback during the demo, a config object landing in a trace
field, a `print()` added while debugging.

The leak this file was written for was real. All three config dataclasses held `api_key` as an
ordinary field, so `repr(RimeConfig.from_env())` rendered the live Rime key in plaintext, and any
exception carrying a config would have put it on screen. The fix is `repr=False`; these tests are
what stop it coming back.

The approach is a sentinel: put a known fake secret into the environment, exercise the paths that
build, describe and fail on configuration, and assert the sentinel appears nowhere in the output.
No real credential is read, and nothing here prints a value even on failure — assertions report
*whether* a secret leaked, never what it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aether.config import LlmConfig, RimeConfig, SttConfig
from aether.events import EventType
from aether.trace import Trace

# Distinctive enough that a substring match cannot be a coincidence. Not a real key.
SENTINEL = "aether-test-sentinel-DO-NOT-USE-1234567890"

SECRET_ENV_VARS = [
    "RIME_API_KEY", "STT_API_KEY", "LLM_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
]

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def sentinel_env(monkeypatch):
    """Every secret-bearing variable set to the same known fake value."""
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, SENTINEL)
    return SENTINEL


# ============================ repr / str ============================

@pytest.mark.parametrize("cls", [RimeConfig, SttConfig, LlmConfig])
def test_config_repr_does_not_render_the_key(cls, sentinel_env):
    """The regression: a dataclass repr prints every field unless told not to."""
    cfg = cls.from_env()

    assert SENTINEL not in repr(cfg), f"{cls.__name__}.__repr__ leaked the api key"
    assert SENTINEL not in str(cfg), f"{cls.__name__}.__str__ leaked the api key"


@pytest.mark.parametrize("cls", [RimeConfig, SttConfig, LlmConfig])
def test_the_key_is_still_usable_after_redaction(cls, sentinel_env):
    """Redaction must hide the value from rendering, not break reading it."""
    assert cls.from_env().api_key == SENTINEL, (
        "repr=False must not stop the program using the key"
    )


def test_config_in_an_f_string_does_not_leak(sentinel_env):
    """The most likely accidental path: someone interpolates the config into a log line."""
    cfg = RimeConfig.from_env()
    assert SENTINEL not in f"rime config: {cfg}"
    assert SENTINEL not in "{}".format(cfg)  # noqa: UP032 - the point is the format path


# ============================ error paths ============================

def test_rime_not_configured_error_names_variables_not_values(monkeypatch):
    """A missing-config error must be diagnosable without being a disclosure."""
    from aether.audio.rime_ws import RimeStreamingTTS

    for var in SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RIME_MODEL", "mistv2")
    monkeypatch.setenv("RIME_VOICE", "astra")
    monkeypatch.setenv("RIME_LANGUAGE", "eng")

    tts = RimeStreamingTTS(Trace(), config=RimeConfig.from_env())
    missing = tts.missing_config()

    assert "RIME_API_KEY" in missing, "presence is reported by NAME"
    assert all(SENTINEL not in m for m in missing)


def test_a_configured_client_reports_presence_without_the_value(sentinel_env):
    """`missing_config()` is the sanctioned way to answer 'is the secret set?'."""
    from aether.audio.rime_ws import RimeStreamingTTS

    tts = RimeStreamingTTS(Trace(), config=RimeConfig.from_env())
    assert tts.missing_config() == [] or all(SENTINEL not in m for m in tts.missing_config())
    assert SENTINEL not in repr(tts.config)


def test_an_llm_configuration_error_does_not_quote_the_key(monkeypatch):
    from aether.llm import LLMConfigurationError, resolve_provider

    for var in SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    with pytest.raises(LLMConfigurationError) as exc:
        resolve_provider()

    message = str(exc.value)
    assert "GEMINI_API_KEY" in message, "it names the variable, which is the useful part"
    assert SENTINEL not in message


# ============================ the trace ============================

def test_no_secret_reaches_the_trace_file(tmp_path, sentinel_env):
    """The trace is written to disk and read back by the evaluator, so it is a disclosure path.

    Emits the fields the pipeline actually stamps on a spoken turn, including the config objects
    that carry keys, and then reads the file back as text.
    """
    path = tmp_path / "run.jsonl"
    trace = Trace(path)
    cfg = RimeConfig.from_env()

    trace.emit(
        EventType.RESPONSE_SPOKEN,
        turn_id=1,
        gen="G1",
        provider="rime",
        model=cfg.model,
        voice=cfg.voice,
        config=cfg,                      # the dangerous case: an object, not a string
        text="the capital of France is Paris",
    )
    trace.close()

    written = path.read_text(encoding="utf-8")
    assert SENTINEL not in written, "a secret reached the trace file"
    # And it is still valid JSONL that the evaluator can read.
    for line in written.splitlines():
        if line.strip():
            json.loads(line)


# ============================ the repository ============================

def test_dotenv_is_ignored_and_untracked():
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    entries = {line.strip() for line in gitignore}
    assert ".env" in entries, ".env must be gitignored"
    assert "!.env.example" in entries, "the template stays tracked"


def test_no_source_file_contains_a_key_shaped_literal():
    """Structural guard: catches the obvious regression of pasting a key into source.

    Honest about its limits — this greps for known key shapes. It cannot catch a secret assembled
    at runtime, and it is not a substitute for the pre-recording sweep in PHASES.md Day 6.
    """
    import re

    patterns = re.compile(
        r"AIza[0-9A-Za-z_\-]{30,}|sk-[A-Za-z0-9]{30,}|gsk_[A-Za-z0-9]{30,}"
        r"|sk-ant-[A-Za-z0-9\-]{30,}"
    )
    checked = 0
    for path in list(REPO.glob("aether/**/*.py")) + list(REPO.glob("scripts/*.py")) \
            + list(REPO.glob("tests/*.py")) + list(REPO.glob("*.md")):
        if "__pycache__" in str(path):
            continue
        checked += 1
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not patterns.search(text), f"a key-shaped literal appears in {path.name}"
    assert checked > 20, "the sweep should actually be looking at the repository"


def test_env_example_carries_no_values():
    """The committed template must document variables, never populate them."""
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip().endswith("_KEY"):
            assert value.strip() in ("", '""', "''"), (
                f"{name.strip()} must be blank in the committed template"
            )
