"""Two latency fixes from the calls of 2026-09-19, each pinned to the measurement that motivated it.

Neither touches the deterministic path, which measured 550-700 ms per turn on both 2026-09-18 and
2026-09-19 -- unchanged. Today's slow turns came from two places this file covers, and two it does
not (see the bottom of the file).

1. A LANGUAGE SWITCH PAID A COLD RIME HANDSHAKE. The first Spanish turn after a switch took 3341 ms
   to first audio against ~265 ms warm, because each voice's socket opened only when that voice was
   first needed. The other voices are now opened during call setup, off the caller's path.

2. A STALLED GEMINI STREAM WAS ALLOWED 30 SECONDS. One turn produced its first token in 1.36 s and
   its first complete sentence in 17.1 s, inside the old limit. The ceiling is now 8 s.
"""

from __future__ import annotations

import types

from aether.lang import ENGLISH, HINDI, LANGUAGES, SPANISH
from aether.llm import REQUEST_TIMEOUT_S


class _FakeSpeaker:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.warm_calls = 0

    def warm(self) -> bool:
        self.warm_calls += 1
        return self.ok


def _spike_with(built: dict, active=ENGLISH):
    """Just enough of Day1Spike to exercise prewarm_voices without audio, network or Rime."""
    from aether.spike import Day1Spike

    spike = Day1Spike.__new__(Day1Spike)
    spike.language = active
    spike._speakers = {active.code: object()}
    spike._speaker_for = lambda lang: built.setdefault(lang.code, _FakeSpeaker())
    spike.prewarm_voices = types.MethodType(Day1Spike.prewarm_voices, spike)
    return spike


# --- 1. voices are warm before anyone switches ----------------------------------------------------

def test_every_other_voice_is_warmed_and_the_active_one_is_left_alone():
    """The active voice is skipped on purpose: the greeting is reading its socket, and two readers
    on one socket is the ConcurrencyError seen on a real call the same day."""
    built: dict = {}
    spike = _spike_with(built, active=ENGLISH)
    warmed = spike.prewarm_voices(tuple(LANGUAGES.values()))

    assert set(warmed) == {HINDI.code, SPANISH.code}
    assert ENGLISH.code not in warmed, "the active voice must not be touched while it is speaking"
    assert all(warmed.values())
    assert all(s.warm_calls == 1 for s in built.values())


def test_a_voice_that_cannot_warm_is_reported_and_does_not_raise():
    """Best-effort: a failed warm-up leaves that voice to open on demand, as it always did."""
    built = {SPANISH.code: _FakeSpeaker(ok=False)}
    spike = _spike_with(built)
    warmed = spike.prewarm_voices((HINDI, SPANISH))
    assert warmed[SPANISH.code] is False
    assert warmed[HINDI.code] is True


def test_a_voice_that_cannot_even_be_built_does_not_stop_the_others():
    from aether.spike import Day1Spike

    spike = Day1Spike.__new__(Day1Spike)
    spike.language = ENGLISH

    def explode(lang):
        if lang is HINDI:
            raise RuntimeError("no Rime for you")
        return _FakeSpeaker()

    spike._speaker_for = explode
    warmed = Day1Spike.prewarm_voices(spike, (HINDI, SPANISH))
    assert warmed == {HINDI.code: False, SPANISH.code: True}


def test_the_rime_client_can_warm_without_speaking():
    """`warm()` opens the socket and nothing else -- no synthesis, no audio, no generation."""
    from aether.audio.rime_ws import RimeStreamingTTS

    client = RimeStreamingTTS.__new__(RimeStreamingTTS)
    opened = []
    client._ws = None
    client._get_ws = lambda: opened.append(True) or "socket"
    client._drop_ws = lambda: None
    assert client.warm() is True and opened == [True]


def test_a_rime_warm_up_failure_is_swallowed():
    from aether.audio.rime_ws import RimeStreamingTTS

    client = RimeStreamingTTS.__new__(RimeStreamingTTS)
    dropped = []

    def refuse():
        raise OSError("handshake refused")

    client._get_ws = refuse
    client._drop_ws = lambda: dropped.append(True)
    assert client.warm() is False
    assert dropped == [True], "a half-open socket must be discarded, not cached"


def test_the_call_handler_starts_the_warm_up_on_its_own_thread():
    """Asserted at the source: the warm-up must never run on the call's own path."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "aether" / "telephony" / "agent.py").read_text(
        encoding="utf-8")
    assert "target=spike.prewarm_voices" in src
    assert 'name="aether-prewarm-voices", daemon=True' in src


# --- 2. a stalled model cannot hold the line -----------------------------------------------------

def test_a_stalled_model_is_cut_off_well_before_a_caller_gives_up():
    """17.1 s was observed inside the old 30 s limit. Healthy fallback turns took 1.1-3.0 s."""
    assert REQUEST_TIMEOUT_S <= 10.0, (
        f"a {REQUEST_TIMEOUT_S} s ceiling lets a stalled stream hold a phone line silent"
    )


def test_the_ceiling_is_never_below_what_gemini_accepts():
    """THIS TEST EXISTS BECAUSE THE FIX WAS FIRST SHIPPED WRONG.

    The ceiling was set to 8 s and every unit test passed -- none of them call the real API. The
    first real request came back 400 INVALID_ARGUMENT: "Manually set deadline 8s is too short.
    Minimum allowed deadline is 10s." Below 10 s the timeout does not shorten stalls, it fails EVERY
    model turn, which on a call means every question the database cannot answer goes silent.
    """
    GEMINI_MINIMUM_DEADLINE_S = 10.0
    assert REQUEST_TIMEOUT_S >= GEMINI_MINIMUM_DEADLINE_S, (
        f"Gemini rejects deadlines under {GEMINI_MINIMUM_DEADLINE_S} s -- at "
        f"{REQUEST_TIMEOUT_S} s every Gemini request fails with 400 INVALID_ARGUMENT"
    )


# --- not covered here, and why --------------------------------------------------------------------
#
# * STT occasionally took ~1370 ms instead of ~300 ms (four turns across two calls). Tested and
#   ruled out: idle keep-alive expiry (at most ~50 ms), rate limiting (29 of 2000 daily requests
#   used, Mumbai region). Cause not yet identified, so nothing is changed for it.
# * The Rime ConcurrencyError when the 8 s line-check collides with an answer. Deliberately left
#   as it is; see the session notes.
