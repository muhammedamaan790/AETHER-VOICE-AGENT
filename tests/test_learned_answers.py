"""Remembering what the model made up, and being honest that it is still made up.

A caller asks something no row covers. The model answers plausibly -- it is allowed to -- and the
answer is written down so the NEXT caller is not told something different. That is the feature.

The thing these tests guard is the line the feature must not cross: a remembered answer is
**consistent, not true**. It never becomes a hotel fact, never outranks the database, and stays
flagged as a guess until a person says otherwise.
"""

from __future__ import annotations

import pytest

from aether.hotel.learned import LearnedAnswers, normalise


@pytest.fixture
def store(tmp_path):
    made = LearnedAnswers(tmp_path / "learned.db")
    yield made
    made.close()


# --- remembering ------------------------------------------------------------------------------

def test_a_question_asked_twice_gets_the_same_answer(store):
    """The whole point. Two callers, one question, one answer -- the hotel does not contradict
    itself about a terrace it has no row for."""
    assert store.recall("Do you have a rooftop terrace?", "eng") is None
    assert store.remember("Do you have a rooftop terrace?", "Yes, on the top floor.", "eng")
    assert store.recall("Do you have a rooftop terrace?", "eng") == "Yes, on the top floor."


def test_the_first_answer_wins_and_later_guesses_do_not_overwrite_it(store):
    """A second call would produce a second plausible answer. Letting it overwrite would restore
    exactly the drift this exists to stop."""
    store.remember("Is there a pool?", "Yes, there is.", "eng")
    assert store.remember("Is there a pool?", "No, there is not.", "eng") is False
    assert store.recall("Is there a pool?", "eng") == "Yes, there is."


def test_punctuation_and_case_do_not_make_it_a_different_question(store):
    store.remember("Do you have a rooftop terrace?", "Yes.", "eng")
    assert store.recall("do you have a ROOFTOP terrace", "eng") == "Yes."


def test_a_genuinely_different_question_is_not_answered_from_a_near_miss(store):
    """Matching is EXACT on purpose. A fuzzy hit would answer a question the caller did not ask --
    the same trade the router makes when two keywords could both apply."""
    store.remember("Do you have a rooftop terrace?", "Yes, on the top floor.", "eng")
    assert store.recall("Is the rooftop terrace heated?", "eng") is None


def test_each_language_remembers_separately(store):
    """An English answer must not be replayed to a Hindi caller -- it would be the right fact in
    the wrong language, which on a phone is no answer at all."""
    store.remember("Do you have a pool?", "Yes, we do.", "eng")
    assert store.recall("Do you have a pool?", "hin") is None
    store.remember("Do you have a pool?", "जी हाँ।", "hin")
    assert store.recall("Do you have a pool?", "eng") == "Yes, we do."
    assert store.recall("Do you have a pool?", "hin") == "जी हाँ।"


def test_nothing_is_remembered_from_an_empty_question_or_answer(store):
    assert store.remember("", "something", "eng") is False
    assert store.remember("something", "", "eng") is False
    assert store.remember("   ", "  ", "eng") is False


# --- staying honest about what it is ------------------------------------------------------------

def test_a_remembered_answer_is_a_guess_until_a_person_agrees(store):
    """The line the feature must not cross. Nothing verified the terrace exists; storing the sentence
    must not quietly promote it to hotel policy."""
    store.remember("Do you have a rooftop terrace?", "Yes, on the top floor.", "eng")
    row = store.all()[0]
    assert row.confirmed is False
    assert store.all(unconfirmed_only=True), "it must show up for review"


def test_confirming_keeps_the_answer_and_clears_the_flag(store):
    store.remember("Do you have a pool?", "Yes.", "eng")
    assert store.confirm("Do you have a pool?", "eng")
    row = store.all(unconfirmed_only=False)[0]
    assert row.confirmed is True
    assert store.recall("Do you have a pool?", "eng") == "Yes."
    assert not store.all(unconfirmed_only=True)


def test_forgetting_sends_the_next_caller_back_to_the_model(store):
    store.remember("Do you have a pool?", "Yes.", "eng")
    assert store.forget("Do you have a pool?", "eng")
    assert store.recall("Do you have a pool?", "eng") is None


def test_use_counts_rise_so_review_can_start_with_what_callers_actually_ask(store):
    """A question asked six times that the database cannot answer is a missing row, and the count
    is what makes that visible."""
    store.remember("Do you have a pool?", "Yes.", "eng")
    for _ in range(3):
        store.recall("Do you have a pool?", "eng")
    assert store.all()[0].times_used == 3


def test_learned_answers_live_in_their_own_table_and_touch_no_hotel_fact(store):
    """Structural, because this is the guarantee that matters most: a guess must never land in
    `menu_items`, `rooms`, `hotel_policies` or anything else the deterministic path reads."""
    import sqlite3

    store.remember("Do you have a pool?", "Yes.", "eng")
    db = sqlite3.connect(str(store.path))
    try:
        tables = {r[0] for r in db.execute(
            "select name from sqlite_master where type='table'")}
    finally:
        db.close()
    assert tables == {"learned_answers"}, (
        f"the learned store created or touched other tables: {tables}"
    )


def test_the_learned_store_could_not_reprice_the_menu_even_pointed_at_the_hotel(tmp_path):
    """Belt and braces, staged as the mistake it guards against.

    Its own file already means a guess cannot reach a price. But `AETHER_LEARNED_DB` is a single
    environment variable, and pointing it at the hotel database is a one-line mistake with no other
    symptom -- the store would open it, create its table, and work. So the connection carries an
    authorizer permitting only `learned_answers`, exactly as `bookings.py` narrows its own.

    Pointed at a COPY of the real hotel database, because the store's own file has no `menu_items`
    for the authorizer to refuse and the test would pass without one installed at all.
    """
    import shutil
    import sqlite3

    from aether.hotel.db import default_db_path

    hotel = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), hotel)

    store = LearnedAnswers(hotel)
    try:
        forbidden = [
            # Real column names, deliberately: `menu_items.price` does not exist, so that statement
            # would raise on its own and pass this test with no authorizer installed at all.
            ("UPDATE menu_items SET price_inr = 1", "reprice the menu"),
            ("UPDATE rooms SET status = 'available'", "free every room"),
            ("DELETE FROM hotel_policies", "delete the policies"),
            ("INSERT INTO guests (full_name) VALUES ('x')", "invent a guest"),
            ("CREATE TABLE sneaky (a TEXT)", "create a table to write through"),
            ("DROP TABLE menu_items", "drop the menu"),
        ]
        for sql, what in forbidden:
            with pytest.raises(sqlite3.DatabaseError):
                store._db.execute(sql)
                pytest.fail(f"the learned connection was able to {what}")
        assert store.remember("is there a pool", "Yes.", "eng"), "its own table still works"
    finally:
        store.close()

    db = sqlite3.connect(str(hotel))
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM menu_items WHERE price_inr = 1").fetchone()[0] == 0, (
            "a hotel fact was changed"
        )
    finally:
        db.close()


# --- the database still wins --------------------------------------------------------------------

def test_the_examples_the_documentation_uses_really_have_no_row():
    """The docs teach this feature with one worked example, and it has to be a true one.

    It was not. Every document said "is there a rooftop pool?" -- and the database answers that,
    from the `swimming_pool` policy, without ever calling the model. A judge following DEMO.md would
    have asked it on camera expecting `llm_ms` to be non-zero and watched it come back zero.

    This is a known trap here, not a novel one: `test_hotel_db.py` records that "swimming pool" and
    "gym" were moved out of its own no-row list on 2026-09-10 for exactly this reason, because the
    database grew a policies table underneath the examples. So the examples are pinned, and a
    policy added later that happens to cover one of them fails here rather than on camera.
    """
    from aether.hotel.router import route

    for said in ("Do you have a rooftop terrace?", "is there a rooftop terrace"):
        assert route(said) is None, (
            f"{said!r} is used in README.md, DEMO.md and the docstrings as the example of a "
            "question the database CANNOT answer -- it now can, so the examples are wrong"
        )


def test_a_question_the_database_answers_never_reaches_the_learned_store():
    """Precedence, asserted where it is decided rather than assumed.

    The turn loop tries the router first and only falls through to recall. If a priced question ever
    started being answered from a remembered sentence, a stale guess could outrank the live row.
    """
    from aether.hotel.router import route

    for said in ("How much is the chicken kebab?", "Is room three zero five free?",
                 "What time is check in?", "Do you have parking?"):
        assert route(said) is not None, f"{said!r} must be answered from the database"


def test_normalise_is_the_lookup_key_and_is_stable():
    assert normalise("  Do YOU have a Rooftop Terrace??  ") == "do you have a rooftop terrace"
    assert normalise("") == ""


def test_learned_answers_are_not_kept_in_the_hotel_s_database():
    """Two regressions in one, both of which bit during this suite's own first runs.

    The store first opened the `LIVE_DB_PATH` constant, which ignores `AETHER_HOTEL_DB`, so
    `pytest` wrote learned answers into the developer's real working copy -- and since a remembered
    answer is recalled, run two could be answered from run one's model output. Pointing it at
    `default_db_path()` fixed that and exposed the deeper one: sharing the hotel's file meant
    remembering an answer contended with taking a booking for the same SQLite lock. The suite spent
    up to 7.5 s per teardown waiting out the busy timeout and one test failed with
    `database is locked` -- and the same contention would cost silence on a live call.

    So it has its own file, which also makes "a guess is not a hotel fact" structural rather than
    enforced.
    """
    from aether.hotel.db import SHIPPED_DB_PATH, default_db_path
    from aether.hotel.learned import default_learned_path

    store = LearnedAnswers()
    try:
        assert store.path == default_learned_path()
        assert store.path != default_db_path(), "sharing the hotel's file is what caused the lock"
        assert store.path != SHIPPED_DB_PATH, "the shipped database must never be written to"
        # It still FOLLOWS the hotel database, so a scratch run gets its own learned answers
        # beside its own hotel rather than writing the developer's.
        assert store.path.parent == default_db_path().parent
    finally:
        store.close()


# --- wired into the turn loop -------------------------------------------------------------------
#
# The store above can be perfect and the feature still not exist. These go through the real
# `Day1Spike`, because the only thing that matters is what a second caller actually hears.

def test_a_second_caller_hears_the_first_caller_s_answer_without_a_model_call(monkeypatch):
    from tests.test_menu_routing import AUDIO, build

    question = "do you have a helipad on the roof"

    spike, _t, rime, llm = build(monkeypatch, question)
    spike.handle_utterance(AUDIO, 0.0)
    assert llm.calls == [question], "nothing is remembered yet, so the model answers"
    first = rime.spoken[-1]

    again, _t2, rime2, llm2 = build(monkeypatch, question)
    again.handle_utterance(AUDIO, 0.0)
    assert rime2.spoken[-1] == first, "the hotel must not change its story between callers"
    assert llm2.calls == [], "the remembered answer costs no model call"


def test_an_answer_the_caller_never_heard_is_not_remembered(monkeypatch):
    """The boundary this rides on, and it is worth being precise about which failure is staged.

    The model composes an answer and the turn survives fencing right up to the speaker -- then no
    audio reaches the gate. The caller heard nothing. That answer must not become the hotel's
    position on anything, which is exactly the rule `history.commit_turn` already follows; the
    remember call sits on that same line so it cannot drift away from it.

    Staged at the GATE rather than by fencing the generation, because a generation fenced while the
    model is thinking never returns an answer at all -- it would pass this test without exercising
    anything. `history` is asserted alongside as the control: if the turn were committed, both
    would be, and the test would be staging the wrong thing.
    """
    from tests.test_menu_routing import AUDIO, build

    question = "is there a squash court"
    spike, _t, rime, llm = build(monkeypatch, question)
    monkeypatch.setattr(spike.gate, "enqueue", lambda *a, **k: False)
    spike.handle_utterance(AUDIO, 0.0)

    assert llm.calls == [question], "the model must have answered, or nothing is being tested"
    assert rime.spoken == ["I can help with that."], "it reached the speaker"
    assert not spike.history.messages(), "control: the turn was not committed as heard"

    store = spike.learned
    assert store is None or store.recall(question, spike.language.code) is None, (
        "an answer no caller heard was written down as the hotel's position"
    )


def test_a_database_answer_is_never_written_down_as_a_guess(monkeypatch):
    """Precedence again, this time through the assembled pipeline: a deterministic answer must not
    land in the learned table, or a live row would later be shadowed by a frozen copy of itself."""
    from tests.test_menu_routing import AUDIO, build

    question = "how much is the chicken kebab"
    spike, _t, _rime, llm = build(monkeypatch, question)
    spike.handle_utterance(AUDIO, 0.0)

    assert llm.calls == []
    store = spike.learned
    assert store is None or store.recall(question, spike.language.code) is None
