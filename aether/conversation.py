"""Session conversation history — Level 1.

Enables contextual follow-ups ("just tell me the first one") by giving the LLM the completed turns
that came before.

**Level 1 means: only a turn that actually reached the completed/spoken boundary is remembered.**
A fenced generation contributes nothing at all — not its assistant answer, and not its user text.
The user moved on before hearing a reply, so as far as the conversation is concerned that exchange
never happened.

This module holds no fencing logic of its own. It cannot: it is only ever written to from the one
place in the pipeline that a fenced generation can never reach (`aether/spike.py`, immediately
after the Output Gate accepts the audio). Fencing stays authoritative; history just rides behind it.

What goes to the model is role/content string pairs and nothing else. No generation IDs, no
timestamps, no trace events, no fencing state, no provider metadata. The model gets conversation,
not telemetry.
"""

from __future__ import annotations

# Model-facing message. Deliberately a plain dict of two strings so nothing internal can ride along.
Message = dict[str, str]

USER = "user"
ASSISTANT = "assistant"

# A demo session should not grow context without bound. Turns beyond this are dropped oldest-first.
# A flat cap is the simplest thing that works; there is no token-budget management here, and that
# limitation is deliberate rather than overlooked.
DEFAULT_MAX_TURNS = 12


class ConversationHistory:
    """Session-scoped, in-memory, ephemeral. Owned by the pipeline, never by a provider object.

    Each session constructs its own instance, so two sessions can never see each other's history.
    """

    def __init__(self, max_turns: int = DEFAULT_MAX_TURNS):
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        self.max_turns = max_turns
        self._messages: list[Message] = []

    # --- reading ----------------------------------------------------------------------

    def messages(self) -> list[Message]:
        """A copy of the committed history, oldest first. Callers cannot mutate our state."""
        return [dict(m) for m in self._messages]

    def context_for(self, user_text: str) -> list[Message]:
        """History plus the message being asked right now.

        The current user message is appended for the request but NOT stored: if this generation
        gets fenced, nothing about it may survive. It is only written down once the turn completes.
        """
        return self.messages() + [{"role": USER, "content": str(user_text).strip()}]

    # --- writing ----------------------------------------------------------------------

    def commit_turn(self, user_text: str, assistant_text: str) -> None:
        """Record one completed exchange.

        Call this ONLY at the completed/spoken lifecycle boundary. Committing on "the LLM
        returned" would be wrong — an answer that was never spoken is not part of the
        conversation.
        """
        self._messages.append({"role": USER, "content": str(user_text).strip()})
        self._messages.append({"role": ASSISTANT, "content": str(assistant_text).strip()})
        self._trim()

    def _trim(self) -> None:
        """Drop the oldest turns past the cap. The ONLY removal in this class.

        This is a session-length bound, not a correctness mechanism, and it is never driven by
        fencing. There is deliberately no `clear()` and no way for anything outside this class to
        delete a committed turn: history is append-only, and fencing operates on audio and results
        only (RULES.md R5 — fencing invalidates output, it does not rewrite the past).
        """
        max_messages = self.max_turns * 2
        if len(self._messages) > max_messages:
            del self._messages[: len(self._messages) - max_messages]

    # --- introspection ----------------------------------------------------------------

    def turn_count(self) -> int:
        return len(self._messages) // 2

    def __len__(self) -> int:
        return len(self._messages)
