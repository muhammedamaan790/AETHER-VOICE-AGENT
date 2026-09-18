"""Choosing a recogniser, and the contract both of them must keep.

Two recognisers exist now: local `faster-whisper` (the default, and what the committed evidence was
recorded with) and hosted Groq Whisper. The turn loop holds one and must not be able to tell which,
so what is tested here is the SEAM -- selection, the shared attribute surface, the shared event --
rather than transcription accuracy, which needs a network and belongs in a measurement script.

No test here calls Groq. They exercise the wiring with a stubbed client, so the suite stays offline
and free.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from aether.events import EventType
from aether.lang import ENGLISH, HINDI
from aether.stt_groq import DEFAULT_MODEL, GroqSTT, build_stt
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)          # one second of silence, shaped like a real utterance


class _FakeTranscription:
    def __init__(self, text):
        self.text = text


class _FakeAudio:
    def __init__(self, owner):
        self._owner = owner

    @property
    def transcriptions(self):
        return self

    def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if self._owner.raises is not None:
            raise self._owner.raises
        return _FakeTranscription(self._owner.text)


class _FakeGroq:
    """Stands in for the SDK client. Records what it was asked for."""

    def __init__(self, text="hello", raises=None):
        self.text, self.raises, self.calls = text, raises, []

    @property
    def audio(self):
        return _FakeAudio(self)


def _groq(monkeypatch, **fake):
    """A `GroqSTT` whose client is a stub, so nothing leaves the machine."""
    stt = GroqSTT(Trace(), api_key="test-key-not-real")
    monkeypatch.setattr(stt, "_client", _FakeGroq(**fake))
    return stt


# --- selection --------------------------------------------------------------------------------

def test_the_default_recogniser_is_still_the_local_one(monkeypatch):
    """The committed evidence, the recorded demo and every offline run depend on this default.

    Adding a hosted provider must not quietly move the baseline: a recogniser that is not the one
    you configured is exactly the difference that stays invisible until a demo.
    """
    monkeypatch.delenv("STT_PROVIDER", raising=False)
    assert type(build_stt(Trace())).__name__ == "WhisperSTT"


@pytest.mark.parametrize("value", ["faster-whisper", "whisper", "local", ""])
def test_the_local_recogniser_answers_to_its_usual_names(value, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", value)
    assert type(build_stt(Trace())).__name__ == "WhisperSTT"


@pytest.mark.parametrize("value", ["groq", "GROQ", "groq-whisper"])
def test_groq_is_selected_by_name(value, monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", value)
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    assert isinstance(build_stt(Trace()), GroqSTT)


def test_an_unknown_provider_raises_rather_than_falling_back(monkeypatch):
    """Silent substitution is how you demo the wrong thing. `build_llm` refuses too."""
    monkeypatch.setenv("STT_PROVIDER", "deepgram")
    with pytest.raises(RuntimeError, match="not a recogniser"):
        build_stt(Trace())


def test_groq_without_a_key_says_so_by_name(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("STT_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        build_stt(Trace())


def test_the_local_model_name_is_not_forwarded_to_a_hosted_provider(monkeypatch):
    """Every CLI defaults `--stt-model` to "base.en", which is a LOCAL model name. Passing it to
    Groq would ask for a model that does not exist there."""
    monkeypatch.setenv("STT_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    assert build_stt(Trace(), model_size=None).model_size == DEFAULT_MODEL


# --- the shared contract ----------------------------------------------------------------------

def test_both_recognisers_present_the_same_attributes():
    """The turn loop writes `language` and `prompt` as state and reads `samplerate`. A recogniser
    missing any of them fails at the call site, not here, so it is pinned here."""
    import inspect

    from aether.stt import WhisperSTT

    for attr in ("transcribe", "language", "prompt", "samplerate", "model_size"):
        assert hasattr(GroqSTT(Trace(), api_key="test-key-not-real"), attr), attr
    # ...and the same call signature, so a caller cannot tell them apart.
    assert (inspect.signature(GroqSTT.transcribe).parameters.keys()
            == inspect.signature(WhisperSTT.transcribe).parameters.keys())


def test_a_transcript_is_returned_and_recorded(monkeypatch):
    stt = _groq(monkeypatch, text="  What starters do you have?  ")
    said = stt.transcribe(AUDIO, turn_id=3, gen="G4")
    assert said == "What starters do you have?", "the transcript should arrive stripped"

    event = stt.trace.last(EventType.TRANSCRIPT_FINAL)
    assert event.fields["text"] == said
    assert event.fields["provider"] == "groq"
    assert event.fields["model"] == DEFAULT_MODEL
    assert event.fields["audio_ms"] == 1000.0
    assert event.fields["stt_error"] is None
    assert event.turn_id == 3 and event.gen == "G4"


def test_the_active_language_selects_the_code_sent_to_the_api(monkeypatch):
    """One multilingual model, so unlike the local path the MODEL does not change -- but the
    language code must, or Hindi audio is decoded as English."""
    stt = _groq(monkeypatch)
    stt.language = HINDI
    stt.transcribe(AUDIO, turn_id=1)
    assert stt._client.calls[-1]["language"] == "hi"

    stt.transcribe(AUDIO, turn_id=2, language=ENGLISH)
    assert stt._client.calls[-1]["language"] == "en", "an explicit argument must win"


def test_the_vocabulary_hint_is_forwarded_when_one_is_set(monkeypatch):
    """The hint is what made a real caller's "Hindi" transcribe as "Hindi" rather than "in the".
    It has to survive the change of recogniser or that fix is undone for Groq users."""
    stt = _groq(monkeypatch)
    stt.transcribe(AUDIO, turn_id=1)
    assert "prompt" not in stt._client.calls[-1], "no hint means no prompt field"
    assert stt.trace.last(EventType.TRANSCRIPT_FINAL).fields["prompted"] is False

    stt.prompt = "English, Hindi, or Spanish?"
    stt.transcribe(AUDIO, turn_id=2)
    assert stt._client.calls[-1]["prompt"] == "English, Hindi, or Spanish?"
    assert stt.trace.last(EventType.TRANSCRIPT_FINAL).fields["prompted"] is True


def test_the_audio_sent_is_a_playable_wav_at_the_pipeline_rate(monkeypatch):
    """The API takes a container, not raw PCM. Sending the wrong rate would not fail -- it would
    transcribe a chipmunk, which is far harder to diagnose than an error."""
    stt = _groq(monkeypatch)
    stt.samplerate = 16000
    stt.transcribe(AUDIO, turn_id=1)

    _name, payload = stt._client.calls[-1]["file"]
    with wave.open(io.BytesIO(payload), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == len(AUDIO)


# --- failing safely ---------------------------------------------------------------------------

def test_a_provider_failure_loses_one_turn_rather_than_the_call(monkeypatch):
    """A hosted recogniser adds a failure mode the local one does not have: the network.

    An exception here would unwind past the run loop, which catches only KeyboardInterrupt, and end
    the session mid-call. Returning "" hands the turn loop something it already handles -- it treats
    an empty transcript as "nothing was said" -- so the caller simply repeats themselves.
    """
    stt = _groq(monkeypatch, raises=RuntimeError("connection reset"))
    assert stt.transcribe(AUDIO, turn_id=7) == ""

    event = stt.trace.last(EventType.TRANSCRIPT_FINAL)
    assert event.fields["text"] == ""
    assert "connection reset" in event.fields["stt_error"]
    assert "RuntimeError" in event.fields["stt_error"], "the type names the failure class"


def test_a_failure_is_visible_in_the_trace_rather_than_silent(monkeypatch):
    """Degrading quietly would make a bad network look like a caller who said nothing. The trace
    has to be able to tell those apart afterwards."""
    stt = _groq(monkeypatch, raises=RuntimeError("boom"))
    stt.transcribe(AUDIO, turn_id=1)
    quiet = _groq(monkeypatch, text="")
    quiet.transcribe(AUDIO, turn_id=1)

    assert stt.trace.last(EventType.TRANSCRIPT_FINAL).fields["stt_error"] is not None
    assert quiet.trace.last(EventType.TRANSCRIPT_FINAL).fields["stt_error"] is None


def test_an_error_message_cannot_carry_the_api_key(monkeypatch):
    """The trace is committed as evidence, and an auth error can quote back what it was sent.

    This test found a real defect: the first implementation truncated the message to 120 characters
    and called that safe. Truncation is not redaction -- a 401 that echoes the key puts it well
    inside the first hundred characters. `Trace.redact` does not cover it either, because it
    redacts by field NAME and this is the text of a field named `stt_error`.
    """
    secret = "sk-this-must-never-be-written-down"
    stt = GroqSTT(Trace(), api_key=secret)
    monkeypatch.setattr(stt, "_client",
                        _FakeGroq(raises=RuntimeError(f"401 invalid key {secret} rejected")))
    stt.transcribe(AUDIO, turn_id=1)

    recorded = str(stt.trace.last(EventType.TRANSCRIPT_FINAL).fields)
    assert secret not in recorded, "the key reached the trace"
    assert "<redacted>" in recorded, "it should be visible that something was removed"
    assert "RuntimeError" in recorded, "redaction must not destroy the diagnosis"


def test_a_credential_shaped_string_is_scrubbed_even_if_it_is_not_our_key(monkeypatch):
    """Defence in depth: a relayed error can carry someone else's credential."""
    stt = _groq(monkeypatch, raises=RuntimeError("upstream rejected gsk_ABCDEFGH12345678 at proxy"))
    stt.transcribe(AUDIO, turn_id=1)
    recorded = str(stt.trace.last(EventType.TRANSCRIPT_FINAL).fields)
    assert "gsk_ABCDEFGH12345678" not in recorded
    assert "<redacted>" in recorded


# --- the provider default that went stale ------------------------------------------------------

def test_the_groq_llm_default_is_not_the_decommissioned_model():
    """REGRESSION. `llama-3.3-70b-versatile` stopped being served to this account and returns 404,
    so `LLM_PROVIDER=groq` with nothing else set failed every turn.

    It hid because `.env` pins gemini -- but `PROVIDER_ORDER` puts groq FIRST, so any machine with a
    Groq key and no explicit provider would have selected a model that no longer exists.
    """
    from aether.llm import PROVIDER_ENV

    _key_var, _model_var, default = PROVIDER_ENV["groq"]
    assert default != "llama-3.3-70b-versatile", "the decommissioned model is back"
    assert default, "a provider must have some default model"


# --- what the round trip decided --------------------------------------------------------------

def test_the_spanish_greeting_does_not_run_the_hotels_name_into_the_next_word():
    """REGRESSION, found by round-tripping the greeting through Rime and back.

    `mistv3/isa` ran "AETHER, la" together: "Ha llamado a AETHER, la gerente" came back as "un
    vitro blanco" -- the hotel garbling its own name in its opening sentence. A full stop after the
    name isolates it and the same round trip returns "Aiter".

    Pinned as structure rather than as wording: what matters is that the name is not followed by a
    comma-and-article, whatever the rest of the sentence becomes.
    """
    from aether.lang import HOTEL_GREETING

    spanish = HOTEL_GREETING["spa"]
    assert "AETHER" in spanish
    after = spanish.split("AETHER", 1)[1]
    assert after.lstrip().startswith("."), f"the name must end a sentence, got {after[:20]!r}"
    assert "AETHER, la" not in spanish


def test_switching_to_a_non_english_language_on_the_local_recogniser_warns(capsys, monkeypatch):
    """Hindi on the local multilingual `base` scored 21.7% against the hosted 89.2% -- it returned
    Urdu script and romanised transliteration. Degrading that far in silence would leave nothing in
    the logs to explain a nonsense transcript."""
    from tests.test_menu_routing import build

    spike, _t, _rime, _llm = build(monkeypatch, "Hindi")

    class _Local:
        """Named as the local class, because that is what the check looks at."""

        language = None
        prompt = None
        samplerate = 16000

    _Local.__name__ = "WhisperSTT"
    monkeypatch.setattr(spike, "stt", _Local())
    spike._set_language(HINDI)

    warning = capsys.readouterr().out
    assert "WARNING" in warning
    assert "STT_PROVIDER=groq" in warning, "the warning should say what to do about it"


def test_english_on_the_local_recogniser_does_not_warn(capsys, monkeypatch):
    """English is a genuine choice: identical words, and the local path works offline. Warning
    about it would train the reader to ignore the warning that matters."""
    from tests.test_menu_routing import build

    spike, _t, _rime, _llm = build(monkeypatch, "English")

    class _Local:
        language = None
        prompt = None
        samplerate = 16000

    _Local.__name__ = "WhisperSTT"
    monkeypatch.setattr(spike, "stt", _Local())
    spike.language = HINDI          # so the switch to English is a real change
    spike._set_language(ENGLISH)

    assert "WARNING" not in capsys.readouterr().out


# --- not paying for a recogniser you are not using --------------------------------------------

def test_prewarm_skips_the_local_recogniser_when_the_hosted_one_is_selected(monkeypatch):
    """Measured waste, not tidiness: importing faster-whisper and loading both sets of weights was
    2335 ms of a 3491 ms prewarm -- two thirds of it, for models nothing would ask. On a machine
    without them cached it is far worse; the multilingual download was timed at 331 seconds.

    Asserted on the step names rather than on timings, which vary by machine.
    """
    import aether.prewarm as prewarm_mod

    monkeypatch.setattr(prewarm_mod, "_import_audio", lambda: None)
    monkeypatch.setattr(prewarm_mod, "_import_llm", lambda: None)
    monkeypatch.setattr(prewarm_mod, "_import_stt", lambda: None)
    monkeypatch.setattr(prewarm_mod, "_warm_weights", lambda size: None)

    monkeypatch.setenv("STT_PROVIDER", "groq")
    hosted = prewarm_mod.prewarm()
    assert "import_stt" not in hosted
    assert not [k for k in hosted if k.startswith("whisper_weights")]

    monkeypatch.setenv("STT_PROVIDER", "faster-whisper")
    local = prewarm_mod.prewarm()
    assert "import_stt" in local
    assert [k for k in local if k.startswith("whisper_weights")], (
        "the local path must still warm its weights, or the first turn pays for the download"
    )


def test_the_llm_is_still_warmed_whichever_recogniser_is_chosen(monkeypatch):
    """The saving is scoped to STT. Skipping the LLM import too would trade one cold start for
    another."""
    import aether.prewarm as prewarm_mod

    monkeypatch.setattr(prewarm_mod, "_import_audio", lambda: None)
    monkeypatch.setattr(prewarm_mod, "_import_llm", lambda: None)
    monkeypatch.setattr(prewarm_mod, "_import_stt", lambda: None)
    monkeypatch.setattr(prewarm_mod, "_warm_weights", lambda size: None)

    for provider in ("groq", "faster-whisper"):
        monkeypatch.setenv("STT_PROVIDER", provider)
        assert "import_llm" in prewarm_mod.prewarm(), provider
        assert "import_audio" in prewarm_mod.prewarm(), provider
