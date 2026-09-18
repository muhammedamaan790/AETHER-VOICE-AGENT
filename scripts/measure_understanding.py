"""How well does AETHER understand its callers, in each language, and how fast does it answer?

    python scripts/measure_understanding.py            # every language
    python scripts/measure_understanding.py --language hin
    python scripts/measure_understanding.py --csv out.csv

WHAT IS MEASURED, and what is not.

**Understanding** here means ROUTING: did the sentence reach the tool that can answer it? That is
the honest thing to measure without a phone in your hand, and it is the thing that decides whether
a caller gets a database fact or a model guess. It does not measure recognition -- what Whisper
makes of the audio -- which is measured separately in `scripts/compare_recognisers.py` and reported
in RIME_EVIDENCE.md, and it does not measure whether the sentence sounds natural when spoken.

**Latency** here is the deterministic path end to end: route, run the tool, render the sentence. No
audio, no network, no model. It is the number that matters for the project's central claim, because
a question that reaches a tool costs this and a question that reaches the model costs this plus a
round trip to Gemini -- measured at roughly 0.9 to 1.8 seconds.

Every expectation is READ FROM THE DATABASE where it can be. Where a question's expected tool is
written down, it is written down once, here, beside the question -- and a question that stops
reaching its tool fails loudly rather than quietly becoming a model call.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# (what the caller says, the tool that should answer it). Grouped by what a caller is trying to do,
# because a score per capability is more useful than one number: "ordering works, policies do not"
# is actionable and "87%" is not.
SUITES: dict[str, dict[str, list[tuple[str, str]]]] = {
    "the menu": {
        "eng": [
            ("what starters do you have", "list_category"),
            ("how much is the chicken kebab", "price_of"),
            ("do you have vegan options", "find_by_diet"),
            ("is the fish curry available", "check_availability"),
            ("does the butter chicken contain nuts", "check_allergens"),
            ("i am allergic to nuts what can i eat", "safe_for"),
            ("tell me about the paneer tikka", "describe_item"),
            ("what is on the menu", "menu_overview"),
        ],
        "hin": [
            ("स्टार्टर में क्या है", "list_category"),
            ("चिकन कबाब कितने का है", "price_of"),
            ("क्या वीगन विकल्प हैं", "find_by_diet"),
            ("क्या फिश करी उपलब्ध है", "check_availability"),
            ("बटर चिकन में मेवा है क्या", "check_allergens"),
            ("मुझे मेवे से एलर्जी है मैं क्या खा सकता हूँ", "safe_for"),
            ("पनीर टिक्का के बारे में बताइए", "describe_item"),
            ("मेन्यू में क्या क्या है", "menu_overview"),
        ],
        "spa": [
            ("que entrantes tienen", "list_category"),
            ("cuanto cuesta el chicken kebab", "price_of"),
            ("tienen opciones veganas", "find_by_diet"),
            ("esta disponible el fish curry", "check_availability"),
            ("el butter chicken lleva frutos secos", "check_allergens"),
            ("soy alergico a los frutos secos que puedo comer", "safe_for"),
            ("hableme del paneer tikka", "describe_item"),
            ("que hay en la carta", "menu_overview"),
        ],
    },
    "rooms and policies": {
        "eng": [
            ("is room three zero five free", "room_status"),
            ("how much is an executive suite", "room_price"),
            ("what kinds of room do you have", "list_room_types"),
            ("do you have any rooms free", "room_availability"),
            ("what time is check in", "check_in_out"),
            ("do you have parking", "hotel_policy"),
            ("where are you located", "hotel_info"),
            ("when is room service available", "service_hours"),
        ],
        "hin": [
            ("कमरा तीन शून्य पाँच खाली है क्या", "room_status"),
            ("एग्जीक्यूटिव सूट कितने का है", "room_price"),
            ("आपके पास कैसे कमरे हैं", "list_room_types"),
            ("कोई कमरा खाली है क्या", "room_availability"),
            ("चेक इन का समय क्या है", "check_in_out"),
            ("पार्किंग है क्या", "hotel_policy"),
            ("होटल कहाँ है", "hotel_info"),
            ("रूम सर्विस कब तक है", "service_hours"),
        ],
        "spa": [
            ("esta libre la habitacion tres cero cinco", "room_status"),
            ("cuanto cuesta una suite ejecutiva", "room_price"),
            ("que tipos de habitacion tienen", "list_room_types"),
            ("tienen habitaciones libres", "room_availability"),
            ("a que hora es el check in", "check_in_out"),
            ("tienen aparcamiento", "hotel_policy"),
            ("donde estan ubicados", "hotel_info"),
            ("hasta que hora hay servicio de habitaciones", "service_hours"),
        ],
    },
    "booking": {
        "eng": [
            ("book a table for four at eight", "reserve_table"),
            ("do you have a table free at eight", "table_availability"),
            ("book a deluxe king for two nights", "reserve_room"),
            ("cancel booking one zero zero four", "cancel_booking"),
            ("what is my booking reference", "my_booking"),
            ("cancel my reservation", "cancel_my_booking"),
            ("when will room three zero five be available", "room_free_from"),
            ("is there a booking on room three zero five", "reservation_for_room"),
        ],
        "hin": [
            ("चार लोगों के लिए आठ बजे टेबल बुक कीजिए", "reserve_table"),
            ("आठ बजे कोई टेबल खाली है क्या", "table_availability"),
            ("दो रात के लिए डीलक्स किंग बुक कीजिए", "reserve_room"),
            ("बुकिंग एक शून्य शून्य चार रद्द कीजिए", "cancel_booking"),
            ("मेरी बुकिंग का नंबर क्या है", "my_booking"),
            ("मेरी बुकिंग रद्द कीजिए", "cancel_my_booking"),
            ("कमरा तीन शून्य पाँच कब तक बुक है", "room_free_from"),
            ("कमरा तीन शून्य पाँच पर कोई बुकिंग है क्या", "reservation_for_room"),
        ],
        "spa": [
            ("reserve una mesa para cuatro a las ocho", "reserve_table"),
            ("hay alguna mesa libre a las ocho", "table_availability"),
            ("reserve una deluxe king para dos noches", "reserve_room"),
            ("cancele la reserva uno cero cero cuatro", "cancel_booking"),
            ("cual es mi numero de reserva", "my_booking"),
            ("cancele mi reserva", "cancel_my_booking"),
            ("hasta cuando esta reservada la habitacion tres cero cinco", "room_free_from"),
            ("hay alguna reserva en la habitacion tres cero cinco", "reservation_for_room"),
        ],
    },
    "ordering": {
        "eng": [
            ("i would like to order the chicken kebab", "add_to_order"),
            ("can i have the paneer tikka", "add_to_order"),
            ("order two masala chai", "add_to_order"),
            ("repeat my order", "repeat_order"),
            ("what did i order", "repeat_order"),
            ("that's all place the order", "place_order"),
            ("cancel my order", "cancel_order"),
        ],
        "hin": [
            ("मुझे चिकन कबाब ऑर्डर करना है", "add_to_order"),
            ("पनीर टिक्का ऑर्डर कीजिए", "add_to_order"),
            ("दो मसाला चाय ऑर्डर कीजिए", "add_to_order"),
            ("मेरा ऑर्डर दोहराइए", "repeat_order"),
            ("मैंने क्या ऑर्डर किया था", "repeat_order"),
            ("बस इतना ही ऑर्डर भेज दीजिए", "place_order"),
            ("मेरा ऑर्डर रद्द कीजिए", "cancel_order"),
        ],
        "spa": [
            ("quiero pedir el chicken kebab", "add_to_order"),
            ("pedir el paneer tikka", "add_to_order"),
            ("pedir dos masala chai", "add_to_order"),
            ("repita mi pedido", "repeat_order"),
            ("que he pedido", "repeat_order"),
            ("eso es todo envie el pedido", "place_order"),
            ("cancele mi pedido", "cancel_order"),
        ],
    },
}

LANGUAGES = ("eng", "hin", "spa")


def _measure(said: str, expected: str, language) -> dict:
    """Route, run and render one sentence. Returns what happened and how long it took."""
    from aether.hotel.router import route
    from aether.hotel.tools import render
    from aether.tools import ToolRunner

    started = time.perf_counter()
    decision = route(said)
    route_ms = (time.perf_counter() - started) * 1000
    if decision is None:
        return {"said": said, "expected": expected, "got": None, "ok": False,
                "route_ms": route_ms, "total_ms": route_ms, "spoken": ""}

    runner = _runner()
    ran = time.perf_counter()
    result = runner.run(decision.tool, gen="g", turn_id=1, is_valid=lambda: True,
                        **decision.params)
    spoken = render(result, language) or ""
    total_ms = route_ms + (time.perf_counter() - ran) * 1000
    return {"said": said, "expected": expected, "got": decision.tool,
            "ok": decision.tool == expected, "route_ms": route_ms,
            "total_ms": total_ms, "spoken": spoken}


_RUNNER = None


def _runner():
    """One runner on a scratch copy, so measuring never writes to the real hotel."""
    global _RUNNER
    if _RUNNER is None:
        import shutil
        import tempfile

        from aether.hotel.db import HotelStore, default_db_path
        from aether.hotel.tools import HOTEL_TOOLS
        from aether.tools import ToolRunner
        from aether.trace import Trace

        scratch = Path(tempfile.mkdtemp(prefix="measure-")) / "hotel.db"
        shutil.copy(default_db_path(), scratch)
        _RUNNER = ToolRunner(Trace(), HotelStore(path=scratch), tools=HOTEL_TOOLS)
    return _RUNNER


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--language", choices=LANGUAGES, help="just one language")
    ap.add_argument("--csv", metavar="PATH", help="also write every row to a CSV")
    ap.add_argument("--misses", action="store_true", help="print only what did not route")
    args = ap.parse_args()

    from aether.lang import by_code

    languages = [args.language] if args.language else list(LANGUAGES)
    rows: list[dict] = []

    for suite, by_language in SUITES.items():
        for code in languages:
            language = by_code(code)
            for said, expected in by_language[code]:
                row = _measure(said, expected, language)
                row["suite"], row["language"] = suite, code
                rows.append(row)

    # --- what did not reach its tool -----------------------------------------------------------
    misses = [r for r in rows if not r["ok"]]
    if misses:
        print("=" * 100)
        print(f"MISSES -- {len(misses)} of {len(rows)} sentences did not reach the tool that "
              f"answers them")
        print("=" * 100)
        for row in misses:
            got = row["got"] or "THE MODEL"
            print(f"  [{row['language']}] {row['suite']:20} {row['said']}")
            print(f"       expected {row['expected']}, got {got}")
        print()
    if args.misses:
        return

    # --- understanding, per capability and language ---------------------------------------------
    print("=" * 100)
    print("UNDERSTANDING -- did the sentence reach the tool that can answer it?")
    print("=" * 100)
    header = f"  {'capability':22}" + "".join(f"{c:>12}" for c in languages) + f"{'all':>12}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for suite in SUITES:
        line = f"  {suite:22}"
        for code in languages:
            here = [r for r in rows if r["suite"] == suite and r["language"] == code]
            hit = sum(1 for r in here if r["ok"])
            line += f"{f'{hit}/{len(here)}':>12}"
        here = [r for r in rows if r["suite"] == suite]
        hit = sum(1 for r in here if r["ok"])
        print(line + f"{f'{hit}/{len(here)}':>12}")
    print("  " + "-" * (len(header) - 2))
    total = f"  {'ALL':22}"
    for code in languages:
        here = [r for r in rows if r["language"] == code]
        hit = sum(1 for r in here if r["ok"])
        total += f"{f'{hit}/{len(here)}':>12}"
    hit = sum(1 for r in rows if r["ok"])
    total += f"{f'{hit}/{len(rows)}':>12}"
    print(total)
    print(f"\n  {hit} of {len(rows)} reached a deterministic tool "
          f"({100 * hit / len(rows):.1f}%). The rest reach Gemini, which is not a failure -- it is "
          f"slower and not grounded in a row.")

    # --- latency ---------------------------------------------------------------------------------
    print()
    print("=" * 100)
    print("LATENCY -- route, run the tool, render the sentence. No audio, no network, no model.")
    print("=" * 100)
    print(f"  {'language':12}{'n':>6}{'median':>12}{'mean':>12}{'p95':>12}{'max':>12}")
    print("  " + "-" * 64)
    for code in languages:
        ms = sorted(r["total_ms"] for r in rows if r["language"] == code and r["ok"])
        if not ms:
            continue
        p95 = ms[min(len(ms) - 1, int(0.95 * len(ms)))]
        print(f"  {code:12}{len(ms):>6}{statistics.median(ms):>11.2f}m"
              f"{statistics.fmean(ms):>11.2f}m{p95:>11.2f}m{max(ms):>11.2f}m")
    everything = sorted(r["total_ms"] for r in rows if r["ok"])
    p95 = everything[min(len(everything) - 1, int(0.95 * len(everything)))]
    print("  " + "-" * 64)
    print(f"  {'ALL':12}{len(everything):>6}{statistics.median(everything):>11.2f}m"
          f"{statistics.fmean(everything):>11.2f}m{p95:>11.2f}m{max(everything):>11.2f}m")
    print("\n  Times are milliseconds. For comparison, a turn that reaches Gemini costs this plus "
          "a\n  measured 0.9-1.8 s round trip -- which is the whole reason the router exists.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["language", "suite", "said", "expected", "got", "ok",
                                    "route_ms", "total_ms", "spoken"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n  wrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
