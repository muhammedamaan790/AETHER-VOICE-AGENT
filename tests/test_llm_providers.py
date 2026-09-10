"""Provider selection for the reasoning path.

Selection is pure configuration logic, so it is tested without any network call. `resolve_provider`
exists precisely so this can be asserted without constructing a client or needing a real key.
"""

from __future__ import annotations

import pytest

from aether.llm import (
    PROVIDER_ENV,
    PROVIDER_ORDER,
    SYSTEM_PROMPT,
    LLMConfigurationError,
    StubLLM,
    build_llm,
    resolve_provider,
)

ALL_KEYS = ["LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY"] + [
    var for spec in PROVIDER_ENV.values() for var in spec[:2]
] + ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY"]


@pytest.fixture
def clean_env(monkeypatch):
    """No provider configured, and no ambient key leaking in from the developer's shell."""
    for name in set(ALL_KEYS):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# --- A: no provider ------------------------------------------------------------------

def test_no_provider_falls_back_to_stub(clean_env):
    assert resolve_provider() is None
    llm = build_llm()
    assert isinstance(llm, StubLLM)
    assert llm.name == "stub"


def test_stub_is_obviously_a_stub(clean_env):
    """It must be impossible to mistake the stub for a real answer in a demo."""
    out = StubLLM().respond("What is the capital of France?")
    assert "do not have a language model configured" in out
    assert "Paris" not in out


# --- G: explicit provider selection ---------------------------------------------------

@pytest.mark.parametrize("provider", ["groq", "anthropic", "openai", "gemini"])
def test_explicit_provider_is_selected(clean_env, provider):
    key_var = PROVIDER_ENV[provider][0]
    clean_env.setenv("LLM_PROVIDER", provider)
    clean_env.setenv(key_var, "test-key-not-real")
    assert resolve_provider() == provider


def test_explicit_provider_reads_only_its_own_key(clean_env):
    """Groq and OpenAI have compatible wire formats. That is not a reason to share credentials."""
    clean_env.setenv("LLM_PROVIDER", "openai")
    clean_env.setenv("GROQ_API_KEY", "groq-key-not-real")
    with pytest.raises(LLMConfigurationError) as exc:
        resolve_provider()
    assert "OPENAI_API_KEY" in str(exc.value)


def test_provider_selection_is_case_insensitive(clean_env):
    clean_env.setenv("LLM_PROVIDER", "GROQ")
    clean_env.setenv("GROQ_API_KEY", "x")
    assert resolve_provider() == "groq"


# --- F: explicit provider without key -------------------------------------------------

@pytest.mark.parametrize("provider", ["groq", "anthropic", "openai", "gemini"])
def test_explicit_provider_without_key_is_a_clear_error(clean_env, provider):
    clean_env.setenv("LLM_PROVIDER", provider)
    with pytest.raises(LLMConfigurationError) as exc:
        resolve_provider()
    msg = str(exc.value)
    assert PROVIDER_ENV[provider][0] in msg
    assert "No other provider will be substituted." in msg


def test_explicit_provider_never_silently_falls_back(clean_env):
    """groq selected, only a Gemini key present -> error, NOT gemini."""
    clean_env.setenv("LLM_PROVIDER", "groq")
    clean_env.setenv("GEMINI_API_KEY", "gemini-key-not-real")
    with pytest.raises(LLMConfigurationError):
        resolve_provider()


def test_unknown_provider_is_rejected(clean_env):
    clean_env.setenv("LLM_PROVIDER", "llama-cpp")
    with pytest.raises(LLMConfigurationError) as exc:
        resolve_provider()
    assert "not supported" in str(exc.value)


# --- H: automatic selection order -----------------------------------------------------

def test_auto_selection_order_is_groq_anthropic_openai_gemini(clean_env):
    assert PROVIDER_ORDER == ("groq", "anthropic", "openai", "gemini")


def test_auto_prefers_groq_when_all_keys_present(clean_env):
    for provider in PROVIDER_ORDER:
        clean_env.setenv(PROVIDER_ENV[provider][0], "x")
    assert resolve_provider() == "groq"


@pytest.mark.parametrize(
    "present,expected",
    [
        (["groq", "anthropic", "openai", "gemini"], "groq"),
        (["anthropic", "openai", "gemini"], "anthropic"),
        (["openai", "gemini"], "openai"),
        (["gemini"], "gemini"),
        ([], None),
    ],
)
def test_auto_selection_walks_the_order(clean_env, present, expected):
    for provider in present:
        clean_env.setenv(PROVIDER_ENV[provider][0], "x")
    assert resolve_provider() == expected


def test_auto_with_no_keys_gives_stub(clean_env):
    assert isinstance(build_llm(), StubLLM)


# --- models ---------------------------------------------------------------------------

def test_model_env_var_overrides_default(clean_env, monkeypatch):
    clean_env.setenv("LLM_PROVIDER", "groq")
    clean_env.setenv("GROQ_API_KEY", "x")
    clean_env.setenv("GROQ_MODEL", "llama-3.1-8b-instant")

    captured = {}

    class FakeGroq:
        def __init__(self, api_key, timeout):
            captured["api_key"] = api_key

    import groq as groq_sdk
    monkeypatch.setattr(groq_sdk, "Groq", FakeGroq)

    llm = build_llm()
    assert llm.name == "groq:llama-3.1-8b-instant"


def test_every_provider_has_a_default_model():
    for provider in PROVIDER_ORDER:
        _, _, default_model = PROVIDER_ENV[provider]
        assert default_model, f"{provider} has no default model"


# --- I: concise, voice-friendly instruction -------------------------------------------

def test_system_prompt_demands_short_spoken_answers():
    p = SYSTEM_PROMPT.lower()
    assert "one short sentence" in p
    assert "spoken aloud" in p
    for banned in ("markdown", "lists"):
        assert banned in p, f"system prompt should forbid {banned!r}"


def test_the_prompt_forbids_text_assistant_self_description():
    """Reported live: AETHER introduced itself as "a text-based assistant".

    The prompt already said "a voice assistant... spoken aloud" at the time, and the model reached
    for its default self-description anyway. A positive role statement was not enough, so the
    denial is now explicit -- and this pins each prohibition individually rather than one phrasing.
    """
    p = SYSTEM_PROMPT.lower()
    for denial in ("text-based", "chatbot", "language model"):
        assert denial in p, f"the prompt must explicitly deny being {denial!r}"
    assert "never" in p, "stated as a prohibition, not merely as a preferred role"


def test_the_prompt_gives_a_concrete_identity_to_fall_back_on():
    """Forbidding an answer without supplying one leaves the model to improvise."""
    p = SYSTEM_PROMPT.lower()
    assert "hotel" in p and "manager" in p


def test_the_prompt_still_forbids_guessing_about_allergens():
    """The ONE thing the model may not improvise, and it is a safety rule rather than an accuracy one.

    The product deliberately allows the model to answer plausibly about hotel details the database
    does not hold -- a rooftop pool, a gym -- because a duty manager who says "that is not in my
    records" is useless on a phone. Allergens are the exception: a guessed "no, that has no nuts"
    can put somebody in hospital, and unlike a wrong opening time it cannot be corrected afterwards.
    """
    p = SYSTEM_PROMPT.lower()
    assert "never guess whether something contains nuts" in p
    assert "check with the kitchen" in p, "a refusal without a next step leaves the caller stuck"
    for allergen in ("nuts", "dairy", "gluten", "shellfish", "fish", "eggs"):
        assert allergen in p


def test_the_prompt_does_not_forbid_answering_about_unlisted_hotel_details():
    """A deliberate reversal, pinned so it is not undone by accident.

    An earlier prompt said "never invent a hotel fact", and the agent answered "I do not have that
    information" to anything absent from the database. That is not the product: unsupported
    questions are meant to reach the model and be answered naturally. This test fails if the old
    blanket prohibition comes back.
    """
    p = SYSTEM_PROMPT.lower()
    assert "never invent a hotel fact" not in p, "the blanket prohibition was deliberately removed"
    assert "answer naturally and helpfully as the duty manager" in p
    assert "do not refuse" in p
    assert "not in the database" in p, "the prompt should name the phrasing it is banning"


def test_brevity_is_instructed_not_truncated():
    """Truncating after generation cuts mid-sentence, which sounds broken when spoken."""
    import inspect

    import aether.llm as llm_mod

    src = inspect.getsource(llm_mod)
    assert "MAX_OUTPUT_TOKENS = 200" in src
    # no post-hoc sentence chopping
    assert ".split('.')[0]" not in src and '.split(".")[0]' not in src


# ============================ conversational register ============================
#
# Reported from a live web test: "Hello, how are you doing today?" was answered with
# "...how can I help make your stay comfortable today?" -- pleasant, and wrong. Nobody had
# mentioned a stay. AETHER is the duty manager answering the phone, not a reservation desk, and
# guessing at the caller's business is what makes an agent sound like a bot.

RESERVATION_WORDS = ("reservation", "booking", "book a", "your stay", "check-in", "check in",
                     "check-out", "check out", "room", "suite")


def test_the_prompt_forbids_volunteering_rooms_and_reservations():
    p = SYSTEM_PROMPT.lower()
    assert "only what was asked" in p, "the instruction must be to answer the question asked"
    for topic in ("rooms", "reservations", "stays", "check-in"):
        assert topic in p, f"the prompt must name {topic!r} as something not to volunteer"
    assert "unless the caller mentions them first" in p, (
        "these topics are legitimate when the caller raises them -- the prohibition is on "
        "introducing them unprompted"
    )


def test_the_prompt_forbids_asking_which_restaurant():
    """There is one hotel and one menu. Asking the caller to choose an outlet is always wrong."""
    p = SYSTEM_PROMPT.lower()
    assert "never ask which restaurant" in p
    assert "one hotel and one menu" in p


def test_the_prompt_models_the_greeting_reply_it_wants():
    """A prohibition without an example leaves the model to improvise the replacement."""
    p = SYSTEM_PROMPT.lower()
    assert "greeting" in p
    assert "i'm doing well, thank you. what can i help you with?" in p


def test_the_prompt_does_not_itself_frame_the_caller_as_a_resident():
    """The prompt must not seed the assumption it is trying to prevent.

    The only occurrences of these words may be the prohibition itself, so each is checked to sit
    inside the sentence that forbids volunteering them.
    """
    p = SYSTEM_PROMPT.lower()
    forbidding = p.split("answer only what was asked", 1)[1]
    before = p.split("answer only what was asked", 1)[0]
    for word in ("your stay", "reservation", "check-in", "guest room"):
        assert word not in before, (
            f"{word!r} appears before the prohibition, framing the caller as a resident"
        )
    assert "rooms" in forbidding


def test_the_identity_survives_the_new_restraint():
    """Being concise must not cost the hotel-manager identity the earlier fix established."""
    p = SYSTEM_PROMPT.lower()
    assert "duty manager of a hotel" in p
    for denial in ("text-based", "chatbot", "language model"):
        assert denial in p


def test_the_prompt_forbids_inventing_hours_rates_and_services():
    """Widened after a live probe, not from theory.

    Asked "what time do you close?", the model answered "our main dining room closes at eleven in
    the evening, but room service is available twenty-four hours a day" -- two specific facts that
    exist nowhere in this system, and room service introduced unprompted. For a judged demo an
    invented fact is worse than an admission: ask twice, get two different opening times.
    """
    p = SYSTEM_PROMPT.lower()
    # What replaced the blanket prohibition: the facts the model IS given are authoritative and may
    # never be contradicted. That is the property that actually matters, because a question the
    # database can answer never reaches the model at all -- the router intercepts it -- so the only
    # way the model could contradict a stored price is by disagreeing with the injected copy.
    assert "authoritative" in p
    assert "never contradict them" in p
    assert "never change a price, a time or a name" in p


def test_the_prompt_still_allows_ordinary_general_knowledge():
    """The other half, and it was a real defect: scoping the restriction too widely made a duty
    manager who could not answer "what is the capital of France?" -- careful about nothing, and
    useless about everything else."""
    p = SYSTEM_PROMPT.lower()
    assert "not about this hotel" in p
    assert "answer normally" in p
    assert "do not refuse a general question" in p
