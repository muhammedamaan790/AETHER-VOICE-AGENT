"""Choosing a language over a real telephone line, pinned to a call that failed.

THE CALL. 2026-09-10, inbound over LiveKit SIP. AETHER asked "English, Hindi, or Spanish?" and the
caller said "Hindi" three times. The recogniser returned "And it's...", "in the" and "Indeed.";
AETHER asked again each time; the caller hung up. The captured audio is the evidence, and it was
replayed afterwards through the same `base.en` model:

    no hint                          -> "in the, in the, in the"
    initial_prompt = the question    -> "Hindi, Hindi, Hindi."

So the model heard the sound correctly all along. It had no reason to expect a word it rarely
sees, and nothing told it that the only three plausible answers were language names.

Three fixes, one test group each, plus the Hinglish booking that arrived in the same message:

1. **The recogniser is told what it is listening for** -- during selection only.
2. **The mishearings a phone line produces are accepted** -- during selection only, and only for
   short answers, because "in the" is one of the commonest phrases in English.
3. **The loop has an exit.** Two misses, then English, with the switch still offered.
"""

from __future__ import annotations

import pytest

from aether.lang import (
    ENGLISH,
    HINDI,
    LANGUAGE_HINT,
    SELECT_FALLBACK,
    SELECT_RETRY,
    SPANISH,
    heard_as_language,
    names_language,
)
from tests.test_menu_routing import AUDIO, build

# Exactly what the recogniser returned on the failed call, in order.
THE_CALL = ("And it's...", "in the", "Indeed.")


def _selecting(monkeypatch, first: str):
    spike, trace, rime, llm = build(monkeypatch, first)
    spike.begin_language_selection()
    return spike, trace, rime, llm


def _say(spike, monkeypatch, text: str) -> None:
    monkeypatch.setattr(spike.stt, "text", text, raising=False)
    spike.handle_utterance(AUDIO, 0.0)


# --- 1. the hint ----------------------------------------------------------------------------------

def test_the_recogniser_is_told_the_question_while_a_language_is_owed(monkeypatch) -> None:
    spike, _t, _r, _l = _selecting(monkeypatch, "Hindi")
    _say(spike, monkeypatch, "Hindi")
    assert spike.stt.prompt is None or spike.stt.prompt == LANGUAGE_HINT


def test_the_hint_is_on_for_the_selection_turn_and_off_afterwards(monkeypatch) -> None:
    """Biasing every turn towards language names would be its own bug -- "indeed" really is a word
    people say to a hotel."""
    spike, _t, _r, _l = _selecting(monkeypatch, "Hindi")
    seen: list[object] = []
    original = spike.stt.transcribe

    def spy(audio, **kwargs):
        seen.append(getattr(spike.stt, "prompt", None))
        return original(audio, **kwargs)

    monkeypatch.setattr(spike.stt, "transcribe", spy)
    _say(spike, monkeypatch, "Hindi")
    _say(spike, monkeypatch, "what starters do you have")

    assert seen == [LANGUAGE_HINT, None]


def test_the_hint_is_the_question_actually_asked() -> None:
    """The prompt that fixed the replayed call was the question itself. Drifting from it would be
    tuning a number nobody measured."""
    for name in ("English", "Hindi", "Spanish"):
        assert name in LANGUAGE_HINT


# --- 2. what a phone line turns the names into -------------------------------------------------

@pytest.mark.parametrize("heard", THE_CALL)
def test_every_mishearing_from_the_failed_call_selects_hindi(heard: str) -> None:
    assert heard_as_language(heard) is HINDI, f"{heard!r} still does not select Hindi"


@pytest.mark.parametrize("heard", THE_CALL)
def test_the_failed_call_would_now_succeed_on_its_first_answer(heard, monkeypatch) -> None:
    """End to end: the caller answers once and is greeted in Hindi, instead of being asked three
    times and hanging up."""
    from aether.lang import HOTEL_GREETING

    spike, _t, rime, _l = _selecting(monkeypatch, heard)
    _say(spike, monkeypatch, heard)

    assert spike.language is HINDI
    assert not spike.awaiting_language
    assert rime.spoken[-1] == HOTEL_GREETING["hin"]


@pytest.mark.parametrize("sentence", [
    "I am calling about a room in the hotel",
    "is there parking in the basement",
    "indeed I would like to book a table for four",
])
def test_a_real_sentence_is_never_read_as_a_misheard_language_name(sentence: str) -> None:
    """The risk the length guard exists for. "in the" is one of the commonest phrases in English, and
    every mishearing on the real call was one or two words long."""
    assert heard_as_language(sentence) is None


def test_the_heard_as_table_is_never_consulted_mid_call(monkeypatch) -> None:
    """"Indeed." must not switch an English conversation into Hindi. The table is for a closed
    question with three answers; mid-call, it would be absurd."""
    spike, _t, _r, _l = _selecting(monkeypatch, "English")
    _say(spike, monkeypatch, "English")
    assert spike.language is ENGLISH

    _say(spike, monkeypatch, "Indeed.")
    assert spike.language is ENGLISH, "a mishearing table switched the language mid-call"


def test_the_exact_names_are_still_preferred() -> None:
    assert names_language("Hindi") is HINDI
    assert names_language("Spanish please") is SPANISH
    assert heard_as_language("spinach") is SPANISH       # what a phone line does to "Spanish"


# --- 3. the loop has an exit ---------------------------------------------------------------------

def test_one_miss_asks_again(monkeypatch) -> None:
    spike, _t, rime, _l = _selecting(monkeypatch, "xyzzy")
    _say(spike, monkeypatch, "xyzzy")
    assert rime.spoken[-1] == SELECT_RETRY
    assert spike.awaiting_language


def test_two_misses_continue_in_english_instead_of_asking_forever(monkeypatch) -> None:
    """The failed call asked three times and the caller hung up. A selection loop with no exit is a
    demo that ends in silence."""
    spike, _t, rime, _l = _selecting(monkeypatch, "xyzzy")
    _say(spike, monkeypatch, "xyzzy")
    _say(spike, monkeypatch, "plugh")

    assert rime.spoken[-1] == SELECT_FALLBACK
    assert not spike.awaiting_language
    assert spike.language is ENGLISH


def test_after_the_fallback_the_caller_can_still_switch(monkeypatch) -> None:
    """The fallback sentence promises this, so it had better be true."""
    spike, _t, _r, _l = _selecting(monkeypatch, "xyzzy")
    _say(spike, monkeypatch, "xyzzy")
    _say(spike, monkeypatch, "plugh")
    _say(spike, monkeypatch, "Hindi")
    assert spike.language is HINDI


def test_a_miss_then_a_hit_does_not_carry_the_miss_forward(monkeypatch) -> None:
    """A later call must not inherit the previous one's misses and fall back on its first answer."""
    spike, _t, _r, _l = _selecting(monkeypatch, "xyzzy")
    _say(spike, monkeypatch, "xyzzy")
    _say(spike, monkeypatch, "Spanish")
    assert spike.language is SPANISH
    assert spike._selection_misses == 0

    spike.begin_language_selection()
    _say(spike, monkeypatch, "xyzzy")
    assert spike.awaiting_language, "the new call fell back after a single miss"


# --- the Hinglish booking from the same message -------------------------------------------------

@pytest.mark.parametrize(("said", "tool", "param", "value"), [
    ("mujhe do raat ke liye room book krna hai", "reserve_room", "nights", 2),
    ("mujhe do raat ke liye room book karna hai", "reserve_room", "nights", 2),
    ("kamra chahiye do raat ke liye", "reserve_room", "nights", 2),
    ("मुझे दो रात के लिए कमरा चाहिए", "reserve_room", "nights", 2),
    ("char logon ke liye table book karna hai", "reserve_table", "party_size", 4),
    ("table book karna hai char logon ke liye", "reserve_table", "party_size", 4),
    ("mujhe deluxe king book karna hai", "reserve_room", "room_type", "Deluxe King"),
])
def test_romanised_hindi_takes_the_booking_it_asks_for(said, tool, param, value) -> None:
    """How an Indian hotel line actually sounds, transcribed by an English-language session. Every
    one of these matched nothing before: the intent words were Devanagari-only, the party-size rule
    was Devanagari-only, and Hindi puts the number BEFORE the noun where "for N" cannot see it."""
    from aether.hotel.router import route

    decision = route(said)
    assert decision is not None, f"{said!r} fell through"
    assert decision.tool == tool, f"{said!r} -> {decision.tool}"
    assert decision.params.get(param) == value, f"{said!r} -> {decision.params}"


@pytest.mark.parametrize("said", ["room ka rate chahiye", "mujhe room ka price chahiye",
                                  "i want to know the room rate"])
def test_wanting_to_know_something_is_not_asking_to_book_it(said: str) -> None:
    """"chahiye" means "want", and wanting a price is not reserving a room. The want-shortcut only
    books when a stay or a party is attached -- the most conservative rule in the router, because it
    is the only one that writes."""
    from aether.hotel.router import route

    decision = route(said)
    assert decision is None or decision.tool not in ("reserve_room", "reserve_table")


def test_the_launcher_names_the_capture_beside_the_log() -> None:
    """`AETHER_CALL_CAPTURE=call3.wav` overwrote every call's audio, including the failed one this
    file is built from. Read as source because the name is chosen inside `main()`."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "run_call.py").read_text(
        encoding="utf-8")
    assert 'log.with_suffix(".wav")' in source
    assert 'env["AETHER_CALL_CAPTURE"]' in source
