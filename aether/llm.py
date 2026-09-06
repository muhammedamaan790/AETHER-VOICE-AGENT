"""Reasoning path — multi-provider, behind one unchanged interface.

The rest of AETHER talks to `LLM.respond(user_text) -> str` and knows nothing about providers.
That indirection is the whole point: swapping Groq for Gemini must not touch the voice pipeline,
the generation model, or fencing.

    AETHER LLM interface  ->  provider adapter  ->  Groq | Anthropic | OpenAI | Gemini

Provider selection is explicit and never silently substituted:

  LLM_PROVIDER set    -> use exactly that provider, read only its key, raise
                         LLMConfigurationError if the key is missing.
  LLM_PROVIDER unset  -> first configured provider in PROVIDER_ORDER (groq first, for latency).
  none configured     -> StubLLM, which is labelled and cannot be mistaken for real answers.

FENCING IS NOT THIS MODULE'S JOB. `respond()` is synchronous and returns a string; whether that
string is allowed to be spoken is decided afterwards, against the generation that requested it
(RULES.md R1). A provider that ignores cancellation and answers late is therefore harmless — the
caller re-checks the fence before anything reaches Rime.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol

from .conversation import Message

# Voice-first. The model is instructed to be brief rather than having its output truncated after
# the fact: truncation cuts mid-sentence, which sounds broken when spoken aloud.
SYSTEM_PROMPT = (
    "You are AETHER, a voice assistant. Your words are spoken aloud, never displayed. "
    "Answer in ONE short sentence. Use two only if a single sentence would be wrong or unclear. "
    "Be direct and natural, the way a person would answer out loud. "
    "Never use markdown, lists, headings, tables, emoji or code blocks. "
    "Do not restate the question, do not preface your answer, and do not say 'As an AI'. "
    "If you do not know, say so briefly."
)

# Voice replies are one or two sentences, so a small cap is a deliberate output-shape choice,
# not a cost hack. Large enough that a legitimate two-sentence answer never truncates.
MAX_OUTPUT_TOKENS = 200

# Never let a wedged provider hang the voice loop.
REQUEST_TIMEOUT_S = 30.0

# Intentional: Groq first for latency during testing.
PROVIDER_ORDER = ("groq", "anthropic", "openai", "gemini")

# provider -> (api key env var, model env var, default model)
PROVIDER_ENV: dict[str, tuple[str, str, str]] = {
    "groq": ("GROQ_API_KEY", "GROQ_MODEL", "llama-3.3-70b-versatile"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-opus-5"),
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4o-mini"),
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", "gemini-3.8-flash"),
}


def _prior_turns(history: Sequence[Message] | None) -> list[dict[str, str]]:
    """Normalise committed history into clean role/content pairs for a provider request.

    Every adapter goes through here, so this is the single place that decides what the model is
    allowed to see. Anything that is not a `user`/`assistant` string pair is dropped: no generation
    IDs, timestamps, trace events, fencing state, latency or provider metadata can reach the model,
    even if a caller passes a richer object by mistake.
    """
    if not history:
        return []

    turns: list[dict[str, str]] = []
    for entry in history:
        role = str(entry.get("role", "")).strip()
        content = str(entry.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})
    return turns


class LLMConfigurationError(RuntimeError):
    """Provider explicitly selected but unusable. Never triggers a silent fallback."""


class LLM(Protocol):
    name: str

    def respond(self, user_text: str, history: Sequence[Message] | None = None) -> str: ...


class StubLLM:
    """Deterministic placeholder used only when no provider is configured.

    Chosen over a canned-answer lookup table because a lookup table could be mistaken for real
    question answering in a demo. This cannot be mistaken for anything.
    """

    name = "stub"

    def respond(self, user_text: str, history: Sequence[Message] | None = None) -> str:
        if not user_text.strip():
            return "I did not catch that."
        return f"You said: {user_text.strip()} I do not have a language model configured yet."


class _OpenAICompatibleLLM:
    """Shared adapter for the two chat-completions providers.

    Groq and OpenAI expose the same request shape, so the call code is shared — but they remain
    SEPARATE providers with separate SDKs, separate keys and separate selection. Compatible wire
    formats are not a reason to share credentials.
    """

    def __init__(self, *, provider: str, client, model: str):
        self.name = f"{provider}:{model}"
        self.model = model
        self._client = client

    def respond(self, user_text: str, history: Sequence[Message] | None = None) -> str:
        messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(_prior_turns(history))
        messages.append({"role": "user", "content": user_text})
        completion = self._client.chat.completions.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=messages,
        )
        return (completion.choices[0].message.content or "").strip()


def _build_groq(api_key: str, model: str) -> LLM:
    from groq import Groq  # imported lazily: only the selected provider's SDK is needed

    return _OpenAICompatibleLLM(
        provider="groq", client=Groq(api_key=api_key, timeout=REQUEST_TIMEOUT_S), model=model
    )


def _build_openai(api_key: str, model: str) -> LLM:
    from openai import OpenAI

    return _OpenAICompatibleLLM(
        provider="openai", client=OpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_S), model=model
    )


class AnthropicLLM:
    def __init__(self, api_key: str, model: str):
        import anthropic

        self.name = f"anthropic:{model}"
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_S)

    def respond(self, user_text: str, history: Sequence[Message] | None = None) -> str:
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            # Low effort keeps latency down for one-sentence spoken answers. Thinking is left at
            # its default rather than disabled -- disabling it on Opus 5 can leak reasoning into
            # the visible text, which would then be spoken aloud.
            output_config={"effort": "low"},
            messages=[
                *_prior_turns(history),
                {"role": "user", "content": user_text},
            ],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()


class GeminiLLM:
    def __init__(self, api_key: str, model: str):
        from google import genai

        self.name = f"gemini:{model}"
        self.model = model
        self._genai = genai
        self._client = genai.Client(api_key=api_key)

    def respond(self, user_text: str, history: Sequence[Message] | None = None) -> str:
        from google.genai import types

        # Gemini names the assistant role "model"; everything else is the same role/text pairs.
        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part(text=m["content"])],
            )
            for m in _prior_turns(history)
        ]
        contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
        return (resp.text or "").strip()


_BUILDERS = {
    "groq": _build_groq,
    "anthropic": AnthropicLLM,
    "openai": _build_openai,
    "gemini": GeminiLLM,
}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def resolve_provider() -> str | None:
    """Which provider would be used, without constructing a client.

    Returns None when nothing is configured (the stub case). Raises LLMConfigurationError when a
    provider was named explicitly but its key is absent — an explicit choice must fail loudly
    rather than quietly becoming a different provider.
    """
    requested = _env("LLM_PROVIDER").lower()

    if requested:
        if requested not in PROVIDER_ENV:
            raise LLMConfigurationError(
                f"LLM_PROVIDER={requested!r} is not supported. "
                f"Choose one of: {', '.join(PROVIDER_ORDER)}."
            )
        key_var, _, _ = PROVIDER_ENV[requested]
        if not _env(key_var):
            raise LLMConfigurationError(
                f"LLM_PROVIDER={requested!r} was selected but {key_var} is not set. "
                f"Set {key_var}, or unset LLM_PROVIDER to auto-select. "
                "No other provider will be substituted."
            )
        return requested

    for provider in PROVIDER_ORDER:
        key_var, _, _ = PROVIDER_ENV[provider]
        if _env(key_var):
            return provider
    return None


def build_llm() -> LLM:
    """Construct the configured provider, or the labelled stub when none is configured."""
    provider = resolve_provider()
    if provider is None:
        return StubLLM()

    key_var, model_var, default_model = PROVIDER_ENV[provider]
    model = _env(model_var) or default_model
    try:
        return _BUILDERS[provider](_env(key_var), model)
    except ImportError as exc:
        raise LLMConfigurationError(
            f"LLM_PROVIDER={provider!r} needs its SDK installed: {exc}. "
            "See requirements.txt."
        ) from exc
