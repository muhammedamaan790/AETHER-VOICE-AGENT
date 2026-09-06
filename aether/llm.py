"""Day-1 reasoning path.

Provider is not yet chosen (MEMORY.md section 10) and no LLM credential is present in this
environment. So this module offers two responders behind one interface:

  AnthropicLLM  -- real path, used when LLM_PROVIDER/LLM_API_KEY are configured
  StubLLM       -- deterministic local responder, used when they are not

The stub exists so the Day-1 audio/VAD/TTS slice can be exercised end to end without a
credential. It is labelled in the trace (`llm_provider`) so no run can quietly look like it used a
real model when it did not. It is NOT a general-knowledge path and must not be presented as one.
"""

from __future__ import annotations

import os
from typing import Protocol

SYSTEM_PROMPT = (
    "You are AETHER, a voice agent. Answer in one or two short spoken sentences. "
    "No markdown, no lists, no formatting -- your text is read aloud."
)


class LLM(Protocol):
    name: str

    def respond(self, user_text: str) -> str: ...


class StubLLM:
    """Deterministic placeholder. Speaks back what it heard, plus a fixed acknowledgement.

    Chosen over a canned-answer lookup table because a lookup table could be mistaken for real
    question answering in a demo. This cannot be mistaken for anything.
    """

    name = "stub"

    def respond(self, user_text: str) -> str:
        if not user_text.strip():
            return "I did not catch that."
        return f"You said: {user_text.strip()} I do not have a language model configured yet."


class AnthropicLLM:
    """Real reasoning path. Untested in this environment -- no ANTHROPIC_API_KEY is present."""

    def __init__(self, api_key: str, model: str):
        import anthropic  # optional dependency; only imported on the real path

        self.name = f"anthropic:{model}"
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def respond(self, user_text: str) -> str:
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_text}],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()


def build_llm() -> LLM:
    """Pick the real path when configured, otherwise the labelled stub."""
    provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    model = (os.environ.get("LLM_MODEL") or "").strip()

    if provider == "anthropic" and api_key and model:
        return AnthropicLLM(api_key=api_key, model=model)
    return StubLLM()
