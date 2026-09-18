"""Does changing the recogniser change the ANSWER a caller gets?

    python scripts/compare_routing.py

The question that actually matters when swapping recognisers. Accuracy scores are interesting;
what a caller experiences is whether their question still reaches the same database row.

So: speak each question with Rime in the language a caller would ask it, transcribe it with BOTH
recognisers, route each transcript, and compare the tool and the arguments. A difference here is a
different answer -- or no answer, if one transcript falls through to the model.

Note the two recognisers write numbers differently: asked "room three zero five", one returns words
and the other returns "305". That is not an error, and the router accepts both -- which is exactly
the kind of thing this checks rather than assumes.

Costs Rime and Groq API calls. Writes nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _harness():
    """Reuse the round-trip helpers rather than duplicating them."""
    spec = importlib.util.spec_from_file_location("cmp", ROOT / "scripts" / "compare_recognisers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# What a caller actually says, in the language they would say it. Chosen to cover the tools a demo
# leans on and the two shapes that historically broke: a spoken room number, and a dish shorthand.
QUESTIONS = {
    "eng": [
        "How much is the chicken kebab?",
        "Is room three zero five free?",
        "What time is check in?",
        "Do you have parking?",
        "What starters do you have?",
        "I am allergic to nuts, what can I eat?",
    ],
    "hin": [
        "Chicken Kebab की कीमत क्या है?",
        "क्या कमरा तीन शून्य पाँच खाली है?",
        "चेक-इन कितने बजे है?",
        "क्या पार्किंग है?",
    ],
    "spa": [
        "¿Cuánto cuesta el Chicken Kebab?",
        "¿Está libre la habitación tres cero cinco?",
        "¿A qué hora es la entrada?",
        "¿Tienen aparcamiento?",
    ],
}


def describe(decision) -> str:
    if decision is None:
        return "-> MODEL (no database answer)"
    args = ", ".join(f"{k}={v}" for k, v in sorted(decision.params.items()))
    return f"{decision.tool}({args})"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)

    cmp = _harness()
    from aether.hotel.router import route
    from aether.lang import ENGLISH, HINDI, SPANISH
    from aether.stt import WhisperSTT
    from aether.stt_groq import GroqSTT
    from aether.trace import Trace

    groq = GroqSTT(Trace(), samplerate=16000)
    local_cache: dict[str, WhisperSTT] = {}
    agree = differ = 0

    for language in (ENGLISH, HINDI, SPANISH):
        print(f"\n{'=' * 78}\n{language.name}\n{'=' * 78}")
        for asked in QUESTIONS[language.code]:
            pcm = cmp.speak(asked, language, 48000)
            if not len(pcm):
                print(f"  NO AUDIO for {asked!r}")
                continue
            narrow = cmp.resample_to_16k(pcm, 48000)

            if language.whisper_model not in local_cache:
                local_cache[language.whisper_model] = WhisperSTT(
                    Trace(), model_size=language.whisper_model)
            local = local_cache[language.whisper_model]
            local.samplerate = 16000
            heard_local = local.transcribe(narrow, turn_id=1, language=language)

            groq.samplerate = 16000
            heard_groq = groq.transcribe(narrow, turn_id=1, language=language)

            # The router reads the caller's words. It is language-agnostic by design -- the hotel's
            # own nouns are English on the menu and on the doors -- so both transcripts go through
            # the same rules.
            route_local, route_groq = route(heard_local), route(heard_groq)
            same = describe(route_local) == describe(route_groq)
            agree += same
            differ += not same

            print(f"\n  asked : {asked}")
            print(f"    local: {heard_local}")
            print(f"           {describe(route_local)}")
            print(f"    groq : {heard_groq}")
            print(f"           {describe(route_groq)}")
            print(f"    {'SAME ANSWER' if same else '*** DIFFERENT ANSWER ***'}")

    total = agree + differ
    print(f"\n{'=' * 78}")
    print(f"same answer: {agree}/{total}    different: {differ}/{total}")
    if differ:
        print("A difference is not automatically a regression -- read which one is RIGHT above.")


if __name__ == "__main__":
    main()
