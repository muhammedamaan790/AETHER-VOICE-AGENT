"""Pay the pipeline's one-off costs before a caller is waiting on them.

Building `Day1Spike` measured **5920 ms** on this machine with warm caches, and the first real call
saw 9051 ms cold. A caller who has just been answered by a hotel should not sit through that, so
this runs at worker startup, before any call exists.

Measured breakdown of the 5920 ms (`python -m aether.prewarm` re-measures it):

    AudioGate      1492 ms     of which `import sounddevice` is 549 ms
    WhisperSTT     2601 ms     of which `import faster_whisper` is 358 ms, weights load 1323 ms
    build_llm      1380 ms     of which `import google.genai` is 600 ms
    MicVAD          442 ms
    build_tts         4 ms

Two different costs, and only one of them is per-call:

* **Imports** are process-global and already shared -- warming them changes nothing about what any
  call owns. `livekit-agents` runs jobs with `JobExecutorType.THREAD` by default, so a job runs in
  this same process and inherits every module already imported. ~1.5 s, free of risk.
* **Whisper's weights** cost 1323 ms to load the first time and 685 ms every time after, because
  the file is then in the OS page cache. Constructing one model at startup and **discarding it**
  buys that 638 ms for every subsequent call while leaving each call with its own model object.

That second point is the whole design of this module. The obvious optimisation -- keep one
`WhisperModel` and share it between calls -- would save the remaining 685 ms and introduce shared
mutable state on the transcription path, where two concurrent calls would interleave inside
ctranslate2. Warming the page cache gets most of the benefit and shares nothing, so it is the
default. Sharing is available behind `AETHER_STT_SHARED=1` for a single-caller demo, is serialised
by a lock, and is documented as a tradeoff rather than presented as free.

Nothing here is required. Every step is best-effort: a prewarm failure must never stop a worker
from accepting calls, because a slow call is better than no call.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("aether.prewarm")


def _timed(label: str, fn, results: dict[str, float]) -> None:
    """Run one warm-up step, record what it cost, and never let it raise."""
    start = time.perf_counter()
    try:
        fn()
    except Exception as exc:                    # noqa: BLE001 - best effort by design
        logger.info("prewarm %s skipped: %s", label, type(exc).__name__)
        return
    results[label] = round((time.perf_counter() - start) * 1000.0, 1)


def _import_audio() -> None:
    import sounddevice  # noqa: F401


def _import_stt() -> None:
    import faster_whisper  # noqa: F401


def _import_llm() -> None:
    """Import the SDK of whichever provider is configured, without constructing a client.

    A client is deliberately NOT built: it would hold per-call mutable state (`last_usage`,
    `last_stream_timing`) and every call constructs its own anyway. Only the import is shared,
    and the import is already shared whether we warm it or not.
    """
    from .llm import resolve_provider

    provider = resolve_provider()
    module = {
        "gemini": "google.genai",
        "openai": "openai",
        "anthropic": "anthropic",
        "groq": "groq",
    }.get(provider or "")
    if module:
        __import__(module)


def _warm_weights(model_size: str) -> None:
    """Load the Whisper weights once and throw the model away.

    The point is the OS page cache, not the object: the next construction reads the same file from
    memory instead of disk. Discarding it is what keeps every call owning its own model.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    del model


def prewarm(*, stt_model: str = "base.en", warm_weights: bool = True) -> dict[str, float]:
    """Warm the process. Returns milliseconds actually spent per step, for the log.

    Safe to call more than once and safe to call from any thread; every step is idempotent
    (imports are cached, and re-warming the page cache is a no-op that costs the cached load).
    """
    results: dict[str, float] = {}
    _timed("import_audio", _import_audio, results)
    _timed("import_stt", _import_stt, results)
    _timed("import_llm", _import_llm, results)
    if warm_weights:
        _timed("whisper_weights", lambda: _warm_weights(stt_model), results)
        # And every OTHER recogniser a call could reach, because the greeting offers Hindi and
        # Spanish and the multilingual model is a DIFFERENT set of weights -- `base.en` cannot
        # transcribe either.
        #
        # This is not a latency nicety. Measured on this machine: the first use of the multilingual
        # model took 331 seconds, because it was not on disk and faster-whisper downloaded it. That
        # would have happened on the first Hindi turn, mid-call, with the caller waiting. Warming it
        # here moves the download to worker start-up, where it costs nobody a conversation -- and if
        # the network is down it fails at start-up, loudly, instead of during a demo.
        for extra in _other_recognisers(stt_model):
            _timed(f"whisper_weights_{extra}", lambda size=extra: _warm_weights(size), results)
    results["total_ms"] = round(sum(results.values()), 1)
    return results


def _other_recognisers(primary: str) -> list[str]:
    """Every Whisper model some language could need, except the one already warmed.

    Read from the language registry rather than listed here, so adding a language cannot leave its
    recogniser un-warmed and waiting to surprise a caller.
    """
    from .lang import LANGUAGES

    seen, extras = {primary}, []
    for language in LANGUAGES.values():
        if language.whisper_model not in seen:
            seen.add(language.whisper_model)
            extras.append(language.whisper_model)
    return extras


def main() -> None:
    """`python -m aether.prewarm` -- measure the warm-up on THIS machine, and print it.

    Provided so the numbers in this module's docstring can be re-derived rather than believed.
    """
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("cold:")
    for key, value in prewarm().items():
        print(f"  {key:16} {value:8.1f} ms")
    print("warm (second run -- what a later call would pay):")
    for key, value in prewarm().items():
        print(f"  {key:16} {value:8.1f} ms")


if __name__ == "__main__":
    main()
