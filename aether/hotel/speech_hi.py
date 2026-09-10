"""Hindi rendering of the things a hotel says out loud: numbers, prices, times, dates, lists.

The Hindi counterpart of the helpers in `aether/hotel/__init__.py`, and it exists for the same
reason: **Rime is never handed a digit or a colon.** A spoken agent should not gamble on how a TTS
engine reads `420` or `14:00`, and the gamble is worse across a script change.

Devanagari, not romanised. Decided by listening, not by preference: both were sent to
`arcana`/`anaya` over `/ws3` (`scripts/verify_rime_hindi.py`) and Devanagari was the one that
sounded right. It also came back materially shorter for identical content -- 9.47 s against 12.21 s
-- which is what you would expect when an engine parses a script natively rather than falling back
to spelling it out.

**Hindi numbers are not compositional the way English ones are.** English builds every two-digit
number from twenty tens and ten units; Hindi has a distinct word for each of 1-100, and they are not
derivable by rule (`उनतालीस` for 39, `उनचास` for 49). So 0-100 is a table, because that is what the
language is, and everything above it composes in the Indian system -- सौ (hundred), हज़ार (thousand).

Scope is deliberately the hotel's own range: prices from 80 to 15000. It raises nothing outside it
and pretends to be nothing more general.
"""

from __future__ import annotations

CURRENCY = "रुपये"

# 0-100. A table because Hindi genuinely has one distinct word per number here -- there is no rule
# that produces उनतालीस from 30 and 9.
_UNITS = (
    "शून्य", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ",
    "दस", "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह", "अठारह", "उन्नीस",
    "बीस", "इक्कीस", "बाईस", "तेईस", "चौबीस", "पच्चीस", "छब्बीस", "सत्ताईस", "अट्ठाईस", "उनतीस",
    "तीस", "इकतीस", "बत्तीस", "तैंतीस", "चौंतीस", "पैंतीस", "छत्तीस", "सैंतीस", "अड़तीस", "उनतालीस",
    "चालीस", "इकतालीस", "बयालीस", "तैंतालीस", "चवालीस", "पैंतालीस", "छियालीस", "सैंतालीस", "अड़तालीस", "उनचास",
    "पचास", "इक्यावन", "बावन", "तिरेपन", "चौवन", "पचपन", "छप्पन", "सत्तावन", "अट्ठावन", "उनसठ",
    "साठ", "इकसठ", "बासठ", "तिरेसठ", "चौंसठ", "पैंसठ", "छियासठ", "सड़सठ", "अड़सठ", "उनहत्तर",
    "सत्तर", "इकहत्तर", "बहत्तर", "तिहत्तर", "चौहत्तर", "पचहत्तर", "छिहत्तर", "सतहत्तर", "अठहत्तर", "उन्यासी",
    "अस्सी", "इक्यासी", "बयासी", "तिरासी", "चौरासी", "पचासी", "छियासी", "सतासी", "अठासी", "नवासी",
    "नब्बे", "इक्यानवे", "बानवे", "तिरानवे", "चौरानवे", "पचानवे", "छियानवे", "सत्तानवे", "अट्ठानवे", "निन्यानवे",
    "सौ",
)

_MONTHS = ("जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
           "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर")


def say_number(n: int) -> str:
    """Spoken Hindi for a whole number up to 99999 -- past anything on this hotel's price list.

    Composes in the Indian system above one hundred: सौ for hundreds, हज़ार for thousands. Narrow on
    purpose, exactly like its English counterpart.
    """
    if n < 0:
        return "ऋण " + say_number(-n)
    if n <= 100:
        return _UNITS[n]
    if n < 1000:
        rest = n % 100
        head = f"{_UNITS[n // 100]} सौ"
        return f"{head} {say_number(rest)}" if rest else head
    if n < 100000:
        rest = n % 1000
        head = f"{say_number(n // 1000)} हज़ार"
        return f"{head} {say_number(rest)}" if rest else head
    raise ValueError(f"{n} is outside the range this hotel needs; refusing to guess")


def say_price(price: float) -> str:
    """`420.0` -> `चार सौ बीस रुपये`. Whole units only; this menu has no paise."""
    return f"{say_number(int(round(price)))} {CURRENCY}"


def say_list(items: list[str]) -> str:
    """`a, b और c` -- how a short list is read aloud in Hindi."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" और {items[-1]}"


def say_room_number(number: str | int) -> str:
    """`305` -> `तीन सौ पाँच`. Said as a CARDINAL, which is how Hindi says a room number.

    This used to say `तीन शून्य पाँच`, digit by digit, and the docstring justified it as "the same
    reason as in English". That reasoning was the mistake: English says a room number digit by
    digit, Hindi does not. A Hindi speaker asks for room 101 as "एक सौ एक", never "एक शून्य एक",
    and the digit-by-digit form reads as a phone number or a PIN. Corrected 2026-09-10 on a native
    speaker's correction -- exactly the class of error RIME_EVIDENCE lists as needing one.

    So Hindi delegates to `say_number`, and the English convention stays in the English renderer
    where it belongs.
    """
    digits = str(number).strip()
    if not digits.isdigit():
        return str(number)
    return say_number(int(digits))


def say_time(clock: str) -> str:
    """`14:00` -> `दोपहर दो बजे`; `06:00` -> `सुबह छह बजे`; `23:00` -> `रात ग्यारह बजे`.

    Hindi puts the part of day *before* the hour, which is why this is a rewrite rather than a
    translation of the English helper.
    """
    raw = str(clock).strip()
    hour_part, _, minute_part = raw.partition(":")
    if not hour_part.strip().isdigit():
        return raw
    hour = int(hour_part)
    minute = int(minute_part) if minute_part.strip().isdigit() else 0

    if hour == 0:
        return "रात बारह बजे" if not minute else f"रात बारह बजकर {say_number(minute)} मिनट"
    if hour == 12:
        part, spoken_hour = "दोपहर", 12
    elif hour < 12:
        part, spoken_hour = "सुबह", hour
    elif hour < 16:
        part, spoken_hour = "दोपहर", hour - 12
    elif hour < 20:
        part, spoken_hour = "शाम", hour - 12
    else:
        part, spoken_hour = "रात", hour - 12

    # Hindi names the common fractions rather than counting minutes, and two of them are irregular:
    # half past one is डेढ़ and half past two is ढाई -- neither is "साढ़े एक" or "साढ़े दो". Quarter
    # past is सवा. "दस बजकर तीस मिनट" is understood but nobody says it, and on a hotel line that is
    # exactly the phrasing that gives a machine away.
    if minute == 30:
        if spoken_hour == 1:
            return f"{part} डेढ़ बजे"
        if spoken_hour == 2:
            return f"{part} ढाई बजे"
        return f"{part} साढ़े {say_number(spoken_hour)} बजे"
    if minute == 15:
        return f"{part} सवा {say_number(spoken_hour)} बजे"

    base = f"{part} {say_number(spoken_hour)}"
    if minute:
        return f"{base} बजकर {say_number(minute)} मिनट"
    return f"{base} बजे"


def say_date(iso: str) -> str:
    """`2026-09-07` -> `सात सितंबर`. The year is dropped, as in the English helper: a caller asking
    about a stay this week does not need it."""
    parts = str(iso).strip().split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return str(iso)
    _, month, day = (int(p) for p in parts)
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return str(iso)
    return f"{say_number(day)} {_MONTHS[month - 1]}"


def say_a(phrase: str) -> str:
    """Hindi has no indefinite article, so this is identity.

    It exists so the renderers can share one shape across languages: the English side needs a/an
    agreement, the Hindi side needs nothing, and neither renderer has to know which it is.
    """
    return str(phrase).strip()
