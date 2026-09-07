"""Gemini latency diagnosis: quota handling, request timeout, and token instrumentation.

Measured 2026-09-06 against gemini-3.8-flash, which is what motivates each of these:

  request build   0.2-5.8 ms      negligible
  API wait        1263-6186 ms    ~100% of the delay
  response parse  0.05-2.4 ms     negligible

  thinking tokens 50-57 with no history -> api_wait 1263-2427 ms; 188 with four turns of
                  history -> 6186 ms (output stayed at 7 tokens). That correlation looked
                  promising, but the controlled A/B below did NOT bear it out.

  thinking A/B    interleaved, with history, 3 questions per arm, all 6 succeeded, both arms 3/3
                  correct: thinking default median 2270 ms (1991-4364) vs thinking_budget=0
                  median 2236 ms (2012-5022). A 34 ms difference with overlapping ranges is
                  noise, and the no-thinking arm produced the single slowest call. Disabling
                  thinking buys nothing.

  streaming       first_text 2889 ms vs total 2891 ms -- no benefit, the one-sentence answer
                  arrives as a single chunk after thinking completes

  429 handling    a burst exhausted the per-minute window, but calls succeeded again at
                  +0 s / +35 s / +70 s -- so 429 is recoverable and stays retryable

Nothing about the generation config is changed here, and that is now a measured conclusion rather
than a deferral: neither streaming nor disabling thinking helps. Latency is dominated by variable
server-side response time (roughly 2-5 s for the same request under different load) plus retries
on 429/503.
"""

from __future__ import annotations

import pytest

from aether.llm import (
    REQUEST_TIMEOUT_S,
    RetryingLLM,
    _is_transient,
)


# --- 429 handling: measured to be recoverable ---------------------------------------------

@pytest.mark.parametrize("message", [
    "429 RESOURCE_EXHAUSTED. You exceeded your current quota, please check your plan and billing",
    "429 Too Many Requests: rate limit exceeded, please slow down",
    "503 UNAVAILABLE. This model is currently experiencing high demand.",
    "502 Bad Gateway",
])
def test_429_and_5xx_stay_retryable(message):
    """Google words a per-minute rate limit identically to a plan quota, and it recovers:
    measured, calls succeeded again at +0 s, +35 s and +70 s after a burst exhausted the window.
    Treating that as permanent would abandon turns a short wait would have served."""
    assert _is_transient(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "400 Bad Request",
    "401 invalid api key",
    "404 model not found",
])
def test_client_errors_are_still_not_retried(message):
    assert _is_transient(RuntimeError(message)) is False


def test_a_recoverable_failure_is_retried_and_succeeds():
    class FlakyLLM:
        name = "gemini:fake"

        def __init__(self):
            self.calls = 0

        def respond(self, user_text, history=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED: exceeded your current quota")
            return "recovered"

    inner = FlakyLLM()
    assert RetryingLLM(inner, backoff=(0.0, 0.0)).respond("hi") == "recovered"
    assert inner.calls == 2


# --- the missing request timeout ----------------------------------------------------------

def test_gemini_client_is_constructed_with_a_timeout(monkeypatch):
    """Groq/OpenAI/Anthropic all bound their requests; Gemini alone did not, so a wedged call
    could block the voice loop indefinitely."""
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    from google import genai
    monkeypatch.setattr(genai, "Client", FakeClient)

    from aether.llm import GeminiLLM
    GeminiLLM("key-not-real", "gemini-3.8-flash")

    http = captured.get("http_options")
    assert http is not None, "Gemini must be given an http timeout like every other provider"
    assert http.timeout == int(REQUEST_TIMEOUT_S * 1000), "HttpOptions.timeout is milliseconds"


def test_every_provider_bounds_its_requests():
    """Guards the inconsistency that let Gemini hang: no adapter may be unbounded."""
    import inspect

    import aether.llm as llm_mod

    src = inspect.getsource(llm_mod)
    assert src.count("REQUEST_TIMEOUT_S") >= 4, (
        "groq, openai, anthropic and gemini must each apply the shared request timeout"
    )


# --- token instrumentation ------------------------------------------------------------------

def test_gemini_records_token_usage(monkeypatch):
    """Latency tracked thinking tokens, so the turn must record them for later confirmation."""
    class FakeUsage:
        prompt_token_count = 138
        thoughts_token_count = 188
        candidates_token_count = 7

    class FakeResp:
        text = "The capital of France is Paris."
        usage_metadata = FakeUsage()

    class FakeModels:
        def generate_content(self, **kwargs):
            return FakeResp()

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    from google import genai
    monkeypatch.setattr(genai, "Client", FakeClient)

    from aether.llm import GeminiLLM
    llm = GeminiLLM("key-not-real", "gemini-3.8-flash")
    assert llm.last_usage is None, "nothing measured before the first call"

    text = llm.respond("What is the capital of France?")

    assert text == "The capital of France is Paris."
    assert llm.last_usage == {
        "prompt_tokens": 138, "thoughts_tokens": 188, "output_tokens": 7,
    }


def test_retry_wrapper_passes_usage_through():
    """Instrumentation must survive the wrapper, or the pipeline sees nothing."""
    class Inner:
        name = "gemini:fake"
        last_usage = {"prompt_tokens": 1, "thoughts_tokens": 2, "output_tokens": 3}

        def respond(self, user_text, history=None):
            return "ok"

    wrapped = RetryingLLM(Inner())
    assert wrapped.last_usage == {"prompt_tokens": 1, "thoughts_tokens": 2, "output_tokens": 3}


def test_usage_is_absent_rather_than_invented_for_providers_that_do_not_report_it():
    class Inner:
        name = "stub"

        def respond(self, user_text, history=None):
            return "ok"

    assert RetryingLLM(Inner()).last_usage is None


# --- the generation config is deliberately unchanged -------------------------------------------

def test_thinking_config_is_not_set_because_it_was_measured_not_to_help():
    """Measured A/B: thinking default median 2270 ms vs thinking_budget=0 median 2236 ms, both
    arms 3/3 correct. A 34 ms gap with overlapping ranges does not justify constraining the
    model's reasoning."""
    import inspect

    import aether.llm as llm_mod

    src = inspect.getsource(llm_mod.GeminiLLM)
    # Check for actual use, not the word: the rationale comment mentions it by name.
    code = " ".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "thinking_config=" not in code, "no thinking config may be passed to the model"
    assert "ThinkingConfig" not in code, "no thinking budget may be constructed"
    assert "MAX_OUTPUT_TOKENS" in code, "the existing token limit is unchanged"


def test_streaming_is_additive_and_does_not_replace_the_reliability_path():
    """Streaming was added in Phase 2, but the measurement that shaped it still stands:
    first_text 2889 ms vs total 2891 ms for a one-sentence reply, because Gemini does not stream
    thinking as text. It pays off for multi-sentence replies, not for the current voice prompt --
    so `respond()` keeps the retry path and streaming is strictly additional."""
    import inspect

    import aether.llm as llm_mod

    src = inspect.getsource(llm_mod.GeminiLLM)
    assert "generate_content_stream" in src, "streaming capability exists"
    assert "generate_content(" in src, "and the non-streaming path is still there"
    assert llm_mod.supports_streaming(llm_mod.StubLLM()) is False, "capability is opt-in per provider"


def test_streaming_is_not_retried_mid_stream():
    """Retrying a stream is not transparent: earlier sentences may already have been spoken."""
    class Inner:
        name = "fake"
        def respond(self, user_text, history=None):
            return "ok"
        def respond_stream(self, user_text, history=None):
            raise RuntimeError("503 UNAVAILABLE")

    wrapped = RetryingLLM(Inner(), backoff=(0.0, 0.0))
    with pytest.raises(RuntimeError):
        list(wrapped.respond_stream("hi"))
