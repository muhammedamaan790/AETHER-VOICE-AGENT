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
from collections.abc import Iterator, Sequence
from typing import Protocol

from .conversation import Message
from .hotel import menu_for_prompt as _menu_for_prompt
from .sentences import SentenceAccumulator
from .trace import now_ms

# Voice-first. The model is instructed to be brief rather than having its output truncated after
# the fact: truncation cuts mid-sentence, which sounds broken when spoken aloud.
SYSTEM_PROMPT = (
    "You are AETHER, the duty manager of a hotel, speaking with a guest on the telephone. "
    "You are a person doing a job on a phone call. "
    # Stated as a prohibition, not just a role. The previous prompt already said "a voice
    # assistant... spoken aloud", and Gemini still introduced itself live as "a text-based
    # assistant" -- a positive description of the role was not enough to stop it reaching for
    # the model's default self-description. The denial has to be explicit.
    "You are NEVER a text-based assistant, a chatbot, a language model or an AI. "
    "Never say or imply that you are text-based, that you cannot hear, that you cannot speak, "
    "or that you are software. If asked what you are, you are the hotel's duty manager. "
    "Everything you say is spoken aloud down a phone line and is never displayed. "
    "Answer in ONE short sentence. Use two only if a single sentence would be wrong or unclear. "
    "Be warm, direct and natural, the way a good hotel manager speaks on the phone. "
    "Never use markdown, lists, headings, tables, emoji or code blocks. "
    "Spell numbers as words, because your words are read aloud: say 'four hundred and twenty "
    "rupees', never '420'. Say a room number digit by digit: 'three oh five', never '305'. "
    "Do not restate the question and do not preface your answer. "
    # ANSWER WHAT WAS ASKED, AND NOTHING ELSE.
    #
    # Live test: "Hello, how are you doing today?" came back as "...how can I help make your stay
    # comfortable today?" -- pleasant, and wrong for this product. Nobody had mentioned a stay.
    # A duty manager answering the phone does not assume the caller is a resident, is booking a
    # room, or is asking about a restaurant; volunteering any of that guesses at the caller's
    # business and makes the agent sound like a reservation bot.
    #
    # There is exactly ONE hotel and ONE menu here, so asking which restaurant, which branch or
    # which outlet is always wrong -- the caller has already reached the only one.
    "Answer only what was asked. Do not bring up rooms, reservations, bookings, stays, check-in, "
    "check-out or restaurants unless the caller mentions them first. "
    "Never ask which restaurant, which outlet or which branch: there is one hotel and one menu, "
    "and the caller has already reached it. "
    "For a greeting or small talk, reply briefly and hand the turn back, like this: "
    "\"I'm doing well, thank you. What can I help you with?\" "
    "Menu prices and dishes are looked up for you and given to you when relevant. "
    # Widened from dishes/prices/allergens after a live probe. Asked "what time do you close?",
    # the model answered "our main dining room closes at eleven, but room service is available
    # twenty-four hours" -- two specific facts, neither of them anywhere in this system, and it
    # introduced room service unprompted. Invented facts are worse than an admission for a judged
    # demo: ask twice and you get two different opening times.
    # THE LISTED FACTS ARE AUTHORITATIVE; ANYTHING ELSE, ANSWER LIKE A MANAGER WHO KNOWS THE HOTEL.
    #
    # This is a deliberate product decision, and it reverses an earlier one. The prompt used to say
    # "never invent a hotel fact", which made the agent answer "I do not have that information" to
    # anything the database did not hold -- correct, and useless on a phone. A duty manager asked
    # whether there is a rooftop pool does not say "that is not in my records".
    #
    # The safety that mattered is preserved by ARCHITECTURE rather than by prompt: any question the
    # database CAN answer never reaches this model at all. `aether.hotel.router` intercepts it and a
    # template renders the row. So the model is only ever asked about things the hotel has no record
    # of, and there is no stored value for it to contradict. What it must not do is disagree with
    # the facts it HAS been given, which is what the next sentence pins.
    "The hotel facts listed below are authoritative and complete for what they cover. Never "
    "contradict them, never change a price, a time or a name that appears there. "
    "If a caller asks about something this hotel has no record of -- a facility, a policy, a "
    "service we have not listed -- answer naturally and helpfully as the duty manager, the way you "
    "would if you knew the place. Do not say that it is 'not in the database', do not refuse, and "
    "do not tell the caller you cannot confirm things. Keep such an answer short and plausible, and "
    "consistent with the kind of hotel described below. "
    # THE ONE EXCEPTION, and it is a safety exception rather than an accuracy one. Everything above
    # is about being useful; this is about not hurting somebody. A guessed "no, that has no nuts"
    # can put a caller in hospital, and unlike a wrong opening time it cannot be corrected later.
    # Allergen and dietary questions the database can answer never reach here anyway (`safe_for`
    # and `check_allergens` intercept them), so this only covers dishes the hotel does not have.
    "ONE EXCEPTION: allergens and dietary safety. Never guess whether something contains nuts, "
    "dairy, gluten, shellfish, fish or eggs, and never guess whether a dish is vegetarian or vegan. "
    "If you were not given that information, say you will check with the kitchen before they order. "
    "For anything that is NOT about this hotel -- general knowledge, arithmetic, a definition, "
    "small talk, a joke, the weather in another city -- answer normally and briefly, the way a "
    "well-informed person would. Do not refuse a general question just because it is not in the "
    "hotel's records, and do not redirect every such question back to the menu. "
    # Live call: asked to "place me order of chocolate buds", AETHER replied "I have added the
    # chocolate fudge cake for three hundred and fifty rupees to your order". There is no order
    # system, nothing was added, and the dish does not exist. Claiming a completed action is worse
    # than declining one, because the caller then believes it happened.
    "You cannot complete transactions. Never say you have added, placed, booked, confirmed or "
    "arranged anything. You may note what the caller wants and say you will pass it on."
)

# THE MENU ITSELF, appended so a router miss cannot become a fabrication.
#
# Menu questions are supposed to be answered deterministically, without the model, and that is
# still what happens: `aether.hotel.router` is faster and cannot be wrong. But on a real call the
# router missed once -- `base.en` transcribed "dessert" as "Desert" -- the model answered from
# nothing, and invented three desserts and three prices. None of them exist.
#
# The instruction "never invent a dish" was already in the prompt above and did not hold. A model
# asked a menu question with no menu in front of it will produce a plausible menu. So it now has
# the real one: a router miss costs a slower answer instead of a fabricated one.
SYSTEM_PROMPT = SYSTEM_PROMPT + (
    "\n\nTHE HOTEL'S OWN RECORDS, read from its database. Every price, dish, allergen, room, "
    "service and time here is exact and authoritative: quote them as written and never contradict "
    "them. They are not, however, the whole of the hotel -- if a caller asks about something not "
    "listed, answer as the duty manager would rather than telling them it is missing from a record."
    "\n\n" + _menu_for_prompt()
)

# WHICH LANGUAGE TO ANSWER IN. The prompt above is entirely English, and left to itself the model
# is inconsistent about this: asked three Hindi questions it answered the first in Hindi, the second
# and third in English. It also used masculine verb forms (`बताता हूँ`) while the deterministic
# templates use feminine ones to match a female voice -- so a caller could hear the agent change
# gender between one answer and the next.
#
# Nothing here relaxes the facts. The hotel is still injected above, and the instruction is about
# the LANGUAGE of the reply, not its content.
# EACH DIRECTIVE ENDS WITH A WORKED EXAMPLE, and that is the part that does the work.
#
# Instructions alone were measured and found unreliable on `gemini-flash-lite-latest`: with a plain
# English instruction, Hindi adhered about two times in three and Spanish two times in five --
# asked in Spanish who Einstein was, it answered in English. Making the instruction longer and
# blunter did not help (2/5 again). Small models follow a demonstration far better than a rule, so
# each directive now shows one question and one answer in the target language, and the instruction
# itself is written in that language rather than about it.
LANGUAGE_DIRECTIVE = {
    "hin": (
        "\n\nभाषा: हिन्दी। आपको हर जवाब सिर्फ़ हिन्दी में, देवनागरी लिपि में देना है। "
        "ऊपर दी गई जानकारी अंग्रेज़ी में है, वह सिर्फ़ आपके पढ़ने के लिए है -- जवाब हिन्दी में ही दीजिए। "
        "व्यंजनों, कमरों और सेवाओं के नाम अंग्रेज़ी में ही रखिए, क्योंकि होटल के मेन्यू पर वही लिखा है। "
        "आप एक महिला हैं, इसलिए स्त्रीलिंग क्रिया रूप इस्तेमाल कीजिए (कर सकती हूँ, बताऊँगी)।"
        "\n\nउदाहरण:\n"
        "मेहमान: अल्बर्ट आइंस्टीन कौन थे?\n"
        "आप: अल्बर्ट आइंस्टीन एक महान भौतिक वैज्ञानिक थे जिन्होंने सापेक्षता का सिद्धांत दिया।\n"
        "मेहमान: क्या आपके यहाँ छत पर स्विमिंग पूल है?\n"
        "आप: जी हाँ, हमारी छत पर एक पूल है जो सुबह से शाम तक खुला रहता है।"
    ),
    "spa": (
        "\n\nIDIOMA: ESPAÑOL. Debe responder siempre en español, en todas sus respuestas, "
        "incluidas las preguntas generales que no tengan nada que ver con el hotel. "
        "La información anterior está en inglés solo para su referencia; NO es el idioma en el que "
        "debe contestar. Mantenga en inglés los nombres de los platos, los tipos de habitación y "
        "los servicios, porque así aparecen en la carta y en las puertas del hotel."
        "\n\nEjemplo:\n"
        "Huésped: ¿Quién fue Albert Einstein?\n"
        "Usted: Fue un físico teórico, conocido sobre todo por la teoría de la relatividad.\n"
        "Huésped: ¿Tienen piscina en la azotea?\n"
        "Usted: Sí, tenemos una piscina en la azotea, abierta desde la mañana hasta el atardecer."
    ),
    "eng": "",
}


# A reminder attached to the CALLER'S OWN TURN, not to the system prompt.
#
# Distance from the generation point turned out to matter more than emphasis. The system prompt is
# several thousand characters of English; a Spanish instruction buried in it was followed 1-2 times
# in 5, and making it longer made it worse. The same instruction sitting immediately before the text
# being answered is the last thing the model reads.
#
# Hindi does not need this -- Devanagari is its own signal and adherence measured 4/4 from the
# system directive alone -- but it is applied uniformly rather than special-cased, because a rule
# that holds for one language and not another is the kind that rots.
_REPLY_IN = {
    "hin": "(हिन्दी में जवाब दीजिए।)",
    "spa": "(Responda en español.)",
}


def localised(user_text: str, language=None) -> str:
    """The caller's words, with a short reply-in-this-language note appended.

    Returns the text unchanged for English and for an unknown language, so the English path is
    byte-for-byte what it was.
    """
    note = _REPLY_IN.get(getattr(language, "code", None))
    return f"{user_text}\n{note}" if note else user_text


def system_prompt_for(language=None) -> str:
    """The system prompt, with a language directive when one is called for.

    Read at call time rather than baked in, so switching language mid-call takes effect on the very
    next turn without rebuilding the client.

    The directive appears **twice, at both ends**, and that is not belt-and-braces for its own sake.
    Measured on `gemini-flash-lite-latest`: with no directive, three Hindi questions came back as
    one Hindi answer and two English ones; with the directive appended once after the whole hotel
    dump, two of three. The prompt is several thousand tokens of hotel facts, and a single line
    buried at the end of it is not salient enough for a small model. Leading with it as well is the
    cheapest thing that raises adherence, and it is measured rather than assumed -- see
    RIME_EVIDENCE.md.
    """
    code = getattr(language, "code", None)
    directive = LANGUAGE_DIRECTIVE.get(code, "")
    if not directive:
        return SYSTEM_PROMPT
    return directive.strip() + "\n\n" + SYSTEM_PROMPT + directive


# Voice replies are one or two sentences, so a small cap is a deliberate output-shape choice,
# not a cost hack. Large enough that a legitimate two-sentence answer never truncates.
MAX_OUTPUT_TOKENS = 200

# Never let a wedged provider hang the voice loop.
REQUEST_TIMEOUT_S = 30.0

# Intentional: Groq first for latency during testing.
PROVIDER_ORDER = ("groq", "anthropic", "openai", "gemini")

# provider -> (api key env var, model env var, default model)
#
# The Gemini default is a MEASURED choice, not a preference. Measured 2026-09-07 against this
# adapter's own `respond_stream`, 3 calls per model, same prompt:
#
#     gemini-3.8-flash          ttft   n/a     total 13809 ms   2/3 ServerError   <- previous default
#     gemini-3.5-flash-lite     ttft   978 ms  total   978 ms   0/3 errors
#     gemini-flash-lite-latest  ttft   800 ms  total   800 ms   0/3 errors        <- chosen
#     gemini-2.5-flash-lite     3/3 ClientError
#     gemini-2.5-flash          3/3 ClientError
#
# `gemini-3.8-flash` was not merely slow, it was failing the majority of calls: a full-pipeline
# bench turn on it recorded llm_ttft_ms = 23112 and the next turn died with ServerError. For a
# realtime voice agent that is not a latency regression, it is an outage.
#
# Overridable with GEMINI_MODEL as before -- this changes only what happens when nothing is set.
PROVIDER_ENV: dict[str, tuple[str, str, str]] = {
    "groq": ("GROQ_API_KEY", "GROQ_MODEL", "llama-3.3-70b-versatile"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-opus-5"),
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4o-mini"),
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", "gemini-flash-lite-latest"),
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


def supports_streaming(llm: object) -> bool:
    """Whether this provider can stream sentences.

    A capability check, not a provider check: the pipeline asks the object, so a provider that
    gains or loses streaming needs no change anywhere else. `respond()` remains the contract every
    provider must satisfy; streaming is strictly additional.
    """
    return callable(getattr(llm, "respond_stream", None))


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
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt_for(getattr(self, "language", None))}
        ]
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
            system=system_prompt_for(getattr(self, "language", None)),
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
        from google.genai import types

        self.name = f"gemini:{model}"
        self.model = model
        self._genai = genai
        # Groq, OpenAI and Anthropic all bound their requests; Gemini alone did not, so a wedged
        # call could block the voice loop indefinitely. HttpOptions.timeout is in milliseconds.
        # Construction is timed because it is a plausible per-turn cost if anything ever
        # recreates the client. It is built ONCE per session here; `transport_reused` on each
        # stream reports whether that stayed true.
        _t0 = now_ms()
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_S * 1000)),
        )
        self.client_init_ms = round(now_ms() - _t0, 3)
        # Identity of the SDK's persistent httpx transport. httpx pools keep-alive connections,
        # so a stable id across turns means no new TCP/TLS handshake per request.
        self._transport_id = self._httpx_transport_id()
        self.requests_made = 0
        self.last_stream_timing: dict[str, float | int | bool | None] | None = None
        # Measured token usage from the last call, kept for production diagnosis.
        # No thinking_config is set, and that is a measured decision: an interleaved A/B with
        # history (3 questions per arm, all correct in both) gave thinking-default median 2270 ms
        # vs thinking_budget=0 median 2236 ms -- a 34 ms gap with overlapping ranges. Latency is
        # dominated by server-side variance, not by thinking.
        self.last_usage: dict[str, int] | None = None

    def _httpx_transport_id(self) -> int | None:
        """Identity of the SDK's underlying sync httpx client, or None if it is not exposed."""
        try:
            return id(self._client._api_client._httpx_client)
        except Exception:
            return None

    def _build_contents(self, user_text: str, history: Sequence[Message] | None):
        from google.genai import types

        # Gemini names the assistant role "model"; everything else is the same role/text pairs.
        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part(text=m["content"])],
            )
            for m in _prior_turns(history)
        ]
        contents.append(types.Content(
            role="user",
            parts=[types.Part(text=localised(user_text, getattr(self, "language", None)))],
        ))
        return contents

    def respond(self, user_text: str, history: Sequence[Message] | None = None) -> str:
        from google.genai import types

        contents = self._build_contents(user_text, history)
        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt_for(getattr(self, "language", None)),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
        self._record_usage(getattr(resp, "usage_metadata", None))
        return (resp.text or "").strip()

    def respond_stream(
        self, user_text: str, history: Sequence[Message] | None = None
    ) -> Iterator[str]:
        """Yield sentences as the model produces them.

        Deliberately separate from `respond()`: that path keeps the retry wrapper and the settled
        reliability behaviour, and nothing about it changes. This one trades retry for the chance
        to start speaking before generation finishes -- a stream cannot be transparently retried
        once its first sentence has already been sent to the speaker.

        Measured caveat, recorded so nobody expects more than it gives: on 2026-09-06 streaming a
        one-sentence reply showed first_text 2889 ms vs total 2891 ms. Gemini does not stream
        thinking as text, so a short answer still arrives as a single chunk after thinking ends.
        This pays off for multi-sentence replies, not for the current one-sentence voice prompt.
        """
        from google.genai import types

        contents = self._build_contents(user_text, history)

        # TTFT is measured on the RAW provider chunk, not on the first assembled sentence. The
        # accumulator deliberately holds text back until a boundary, so timing it there would
        # blame the accumulator for the provider's latency (or vice versa). Both are recorded.
        request_sent_ms = now_ms()
        self.requests_made += 1
        stream = self._client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt_for(getattr(self, "language", None)),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )

        accumulator = SentenceAccumulator()
        last = None
        ttft_ms: float | None = None
        raw_chunks = 0
        try:
            for chunk in stream:
                text = chunk.text or ""
                if text and ttft_ms is None:
                    ttft_ms = round(now_ms() - request_sent_ms, 3)
                if text:
                    raw_chunks += 1
                last = getattr(chunk, "usage_metadata", None) or last
                yield from accumulator.feed(text)
            tail = accumulator.flush()
            if tail:
                yield tail
        finally:
            # Recorded even when the consumer stops early (a fence closes the generator), so a
            # fenced turn still reports how long the provider actually took.
            self.last_stream_timing = {
                "request_sent_ms": round(request_sent_ms, 3),
                "ttft_ms": ttft_ms,
                "total_ms": round(now_ms() - request_sent_ms, 3),
                "raw_chunks": raw_chunks,
                "transport_reused": self._httpx_transport_id() == self._transport_id,
                "client_init_ms": self.client_init_ms,
                "requests_made": self.requests_made,
            }
        self._record_usage(last)

    def _record_usage(self, usage) -> None:
        if usage is not None:
            self.last_usage = {
                "prompt_tokens": usage.prompt_token_count or 0,
                "thoughts_tokens": usage.thoughts_token_count or 0,
                "output_tokens": usage.candidates_token_count or 0,
            }


_BUILDERS = {
    "groq": _build_groq,
    "anthropic": AnthropicLLM,
    "openai": _build_openai,
    "gemini": GeminiLLM,
}


# Transient provider failures, retried. Measured against gemini-3.8-flash on 2026-09-06: 2 of 6
# calls returned 503 UNAVAILABLE ("this model is currently experiencing high demand"). That is
# server-side capacity, not a bug here, and a turn lost to it is a turn the user has to repeat.
TRANSIENT_STATUS = (429, 500, 502, 503, 504)

# NOTE on 429 RESOURCE_EXHAUSTED "you exceeded your current quota": Google uses that same wording
# for a per-minute rate limit as for a hard plan quota, and measurement showed it IS recoverable --
# calls succeeded again at +0 s, +35 s and +70 s after a burst exhausted the window. So a 429 stays
# retryable. An earlier reading of "6 of 6 failed within seconds" was wrong: those six calls were
# one burst inside a single one-minute window, not proof of a permanent failure.
#
# Caveat worth knowing: the backoff below (0.5 s / 1.0 s) is far shorter than a one-minute window,
# so a retry will not rescue an RPM-limited turn. Lengthening it trades against a caller sitting in
# silence, and that trade has not been measured -- left alone deliberately.
TRANSIENT_MARKERS = ("unavailable", "overloaded", "timeout", "timed out", "temporarily",
                     "try again", "connection reset", "connection aborted", "rate limit")

# Deliberately short: this is a voice loop, and a caller waiting in silence is the thing being
# traded against. Worst case adds ~1.5 s before the turn is given up on.
RETRY_BACKOFF_S = (0.5, 1.0)


def _is_transient(exc: Exception) -> bool:
    """Transient == worth retrying. Auth and malformed-request failures are not.

    Status code first (every SDK here exposes one somewhere), message text only as a fallback, so
    a provider that words its errors differently still degrades to "don't retry" rather than
    retrying something hopeless.
    """
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value in TRANSIENT_STATUS
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    text = str(exc).lower()
    if any(str(code) in text for code in TRANSIENT_STATUS):
        return True
    return any(marker in text for marker in TRANSIENT_MARKERS)


class RetryingLLM:
    """Retries transient provider failures. Wraps any adapter without knowing which one.

    Applied once in `build_llm()`, so no adapter and no provider-selection logic changes. It never
    substitutes a different provider and never invents a response: if every attempt fails, the
    exception propagates and the caller's existing `ResultDiscarded` path handles it.
    """

    def __init__(self, inner: LLM, backoff: tuple[float, ...] = RETRY_BACKOFF_S):
        self._inner = inner
        self._backoff = backoff
        self.name = inner.name
        self.last_attempts = 0
        # Capability MIRRORING, and it has to be per-instance rather than a method on the class.
        #
        # `supports_streaming()` is deliberately a duck-type check -- it asks the object it was
        # handed, so a provider that gains or loses streaming needs no change anywhere else. A
        # wrapper that unconditionally defines `respond_stream` breaks exactly that contract: it
        # answers "yes" on behalf of an adapter that cannot stream, the pipeline commits to the
        # streaming path, and the turn dies on the first token.
        #
        # That was not theoretical. Every non-Gemini provider (groq, anthropic, openai) failed
        # 100% of turns with `llm_stream_error` (AttributeError) because this wrapper claimed a
        # capability the adapter underneath did not have. Binding the attribute only when the
        # inner adapter really streams makes the wrapper honest by construction.
        if callable(getattr(inner, "respond_stream", None)):
            self.respond_stream = self._respond_stream

    # THE ACTIVE LANGUAGE HAS TO REACH THE ADAPTER, and for a while it did not.
    #
    # `build_llm()` returns this wrapper, so `spike._set_language` was setting `.language` HERE
    # while every adapter read `getattr(self, "language", None)` on ITSELF and saw nothing. The
    # language directive and the per-turn reminder were therefore never applied to a single live
    # call. It went unnoticed because a Devanagari question elicits a Hindi answer regardless --
    # Hindi appeared to work while Spanish, which shares an alphabet with the English prompt,
    # answered in English and looked like a model limitation rather than a missing assignment.
    #
    # Forwarded as a property so there is exactly one place the value can live.
    @property
    def language(self):
        return getattr(self._inner, "language", None)

    @language.setter
    def language(self, value) -> None:
        self._inner.language = value

    def _respond_stream(
        self, user_text: str, history: Sequence[Message] | None = None
    ) -> Iterator[str]:
        """Delegate streaming, WITHOUT retrying it.

        Retrying a stream is not transparent: by the time a failure appears, earlier sentences may
        already have been spoken, and replaying them would repeat audio the user has heard. A
        failed stream is surfaced to the caller, which discards the turn exactly as it discards any
        other failure. Non-streaming `respond()` keeps its retry behaviour untouched.

        Only reachable when `__init__` bound it, so the inner adapter is known to stream.
        """
        return self._inner.respond_stream(user_text, history)

    @property
    def last_stream_timing(self):
        """Pass through the wrapped adapter's stream measurements, if it takes any."""
        return getattr(self._inner, "last_stream_timing", None)

    @property
    def last_usage(self) -> dict[str, int] | None:
        """Pass through whatever the wrapped adapter measured, if anything."""
        return getattr(self._inner, "last_usage", None)

    def respond(self, user_text: str, history: Sequence[Message] | None = None) -> str:
        import time

        last: Exception | None = None
        for attempt in range(len(self._backoff) + 1):
            self.last_attempts = attempt + 1
            try:
                return self._inner.respond(user_text, history)
            except Exception as exc:
                last = exc
                if attempt >= len(self._backoff) or not _is_transient(exc):
                    raise
                time.sleep(self._backoff[attempt])
        raise last  # unreachable; keeps the type checker honest


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
        # Retry wraps the chosen provider; it never changes which provider was chosen.
        return RetryingLLM(_BUILDERS[provider](_env(key_var), model))
    except ImportError as exc:
        raise LLMConfigurationError(
            f"LLM_PROVIDER={provider!r} needs its SDK installed: {exc}. "
            "See requirements.txt."
        ) from exc
