"""Real LLM smoke test through the existing AETHER generation/fencing path.

Makes an actual API call with the configured provider, then pushes the answer through the same
generation + fence check the voice pipeline uses, and reports whether it would reach Rime.

    python scripts/smoke_llm.py --question "What is the capital of France?"

Nothing here bypasses fencing, and no API key is ever printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.events import EventType                              # noqa: E402
from aether.llm import LLMConfigurationError, build_llm, resolve_provider  # noqa: E402
from aether.supervisor.generations import GenerationRegistry     # noqa: E402
from aether.trace import Trace, now_ms                           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--question", default="What is the capital of France?")
    ap.add_argument("--fence", action="store_true",
                    help="fence the generation mid-flight to prove the answer is discarded")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()  # core AETHER .env at the repo root -- never telephony_spike/.env

    try:
        provider = resolve_provider()
    except LLMConfigurationError as exc:
        print(f"CONFIGURATION ERROR: {exc}")
        return 2

    print(f"provider selected : {provider or '(none -- stub)'}")

    llm = build_llm()
    print(f"llm.name          : {llm.name}")
    if provider is None:
        print("\nNo LLM provider configured, so this would be the stub, not a real model.")
        print("Set LLM_PROVIDER and the matching *_API_KEY in .env to run a real call.")
        return 3

    trace = Trace()
    gens = GenerationRegistry(trace)
    gen = gens.allocate(turn_id=1)
    trace.emit(EventType.TASK_STARTED, turn_id=1, gen=gen.id, task="respond",
               params={"utterance": args.question})

    print(f"\nquestion          : {args.question}")
    t0 = now_ms()
    try:
        answer = llm.respond(args.question)
    except Exception as exc:
        # Never surface raw provider exception text to speech; keep the diagnostic concise.
        print(f"\nLLM CALL FAILED: {type(exc).__name__}: {exc}")
        return 4
    elapsed = now_ms() - t0

    print(f"latency_ms        : {elapsed:.0f}")
    print(f"answer            : {answer!r}")
    print(f"sentences         : {sum(answer.count(c) for c in '.!?')}")
    print(f"chars             : {len(answer)}")

    if args.fence:
        gens.mark_fenced(gen.id)
        trace.emit(EventType.FENCE_REQUESTED, gen=gen.id, reason="smoke_test_interruption")
        print("\n[fenced the generation before the answer was checked]")

    # The same check the voice pipeline performs before anything reaches Rime.
    if not gens.is_active(gen.id):
        trace.emit(EventType.RESULT_DISCARDED, turn_id=1, gen=gen.id,
                   active_gen=gens.active.id if gens.active else None,
                   reason="stale_generation_llm", stage="llm")
        print("VERDICT           : DISCARDED -- would NOT reach Rime")
    else:
        print("VERDICT           : VALID -- would proceed to Rime (provider=rime)")

    print("\ntrace events      :", [e.type for e in trace.events])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
