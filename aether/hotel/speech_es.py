"""Spanish rendering of the things a hotel says aloud: numbers, prices, times, dates, lists.

The Spanish counterpart of the helpers in `aether/hotel/__init__.py` and `speech_hi.py`, and it
exists for the same reason: **Rime is never handed a digit or a colon.** A spoken agent should not
gamble on how a TTS engine reads `420` or `14:00`, and the gamble gets worse across a language.

Spanish numbers are mostly regular, and the irregularities are exactly where a naive implementation
breaks:

* 16-29 are single fused words -- `dieciséis`, `veintidós` -- not "diez y seis";
* 31 upward are three words with `y` -- `treinta y uno` -- but 21-29 are never;
* several hundreds are suppletive: 500 is `quinientos`, not "cincocientos"; 700 is `setecientos`;
  900 is `novecientos`;
* 100 alone is `cien`, but 101 is `ciento uno`.

So the tables below are the language, not a shortcut. Scope is the hotel's own range -- 80 to 15000
-- and it raises rather than guesses outside it.
"""

from __future__ import annotations

CURRENCY = "rupias"

_UNITS = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve",
          "diez", "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete",
          "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidós", "veintitrés",
          "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho", "veintinueve")

_TENS = {30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta",
         70: "setenta", 80: "ochenta", 90: "noventa"}

# Suppletive forms. `quinientos`, `setecientos` and `novecientos` are not built from their units,
# which is the trap in every hand-rolled Spanish number function.
_HUNDREDS = {1: "ciento", 2: "doscientos", 3: "trescientos", 4: "cuatrocientos",
             5: "quinientos", 6: "seiscientos", 7: "setecientos", 8: "ochocientos",
             9: "novecientos"}

_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio",
           "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def _feminine(word: str) -> str:
    """`doscientos` -> `doscientas`, `uno` -> `una`, `veintiuno` -> `veintiuna`.

    Spanish numbers agree in gender with the noun they count, and only two families inflect: the
    hundreds from 200 up, and anything ending in `uno`. `cien`, `ciento`, `mil` and the tens do not.
    """
    # `ientos`, not `cientos`: `quinientos`, `setecientos` and `novecientos` are suppletive and do
    # not contain "cientos", so matching that would silently leave the three irregular hundreds
    # masculine -- which is precisely where a Spanish speaker would notice.
    if word.endswith("ientos"):
        return word[:-2] + "as"
    if word == "uno":
        return "una"
    if word.endswith("uno"):          # veintiuno -> veintiuna, treinta y uno -> treinta y una
        return word[:-1] + "a"
    return word


def say_number(n: int, feminine: bool = False) -> str:
    """Spoken Spanish for a whole number up to 99999 -- past anything this hotel charges.

    `feminine` makes the number agree with a feminine noun. It is not decoration: `rupia` is
    feminine, so a price is `cuatrocientas veinte rupias`, and `cuatrocientos veinte rupias` is an
    error a Spanish speaker hears immediately. English needs no such thing, which is exactly why
    this is a rewrite rather than a translation of the English helper.
    """
    if n < 0:
        return "menos " + say_number(-n, feminine)
    if n < 30:
        return _feminine(_UNITS[n]) if feminine else _UNITS[n]
    if n < 100:
        tens, rest = (n // 10) * 10, n % 10
        if not rest:
            return _TENS[tens]
        unit = _feminine(_UNITS[rest]) if feminine else _UNITS[rest]
        return f"{_TENS[tens]} y {unit}"
    if n == 100:
        return "cien"                      # alone it is `cien`; 101 is `ciento uno`
    if n < 1000:
        rest = n % 100
        head = _HUNDREDS[n // 100]
        if feminine:
            head = _feminine(head)
        return f"{head} {say_number(rest, feminine)}" if rest else head
    if n < 100000:
        thousands, rest = n // 1000, n % 1000
        # `mil` itself never inflects, and neither does the count in front of it: it is "dos mil
        # rupias", never "dos miles". Only the remainder below a thousand agrees.
        head = "mil" if thousands == 1 else f"{say_number(thousands)} mil"
        return f"{head} {say_number(rest, feminine)}" if rest else head
    raise ValueError(f"{n} is outside the range this hotel needs; refusing to guess")


def say_price(price: float) -> str:
    """`420.0` -> `cuatrocientas veinte rupias`. Whole units only; this menu has no paise.

    Feminine, because `rupia` is.
    """
    return f"{say_number(int(round(price)), feminine=True)} {CURRENCY}"


def say_list(items: list[str]) -> str:
    """`a, b y c` -- how a short list is read aloud in Spanish."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" y {items[-1]}"


def say_room_number(number: str | int) -> str:
    """`305` -> `tres cero cinco`. A room number names a door, not a quantity.

    Said digit by digit for the same reason as in English and Hindi: `say_number(305)` gives
    "trescientos cinco", which counts rooms rather than naming one.
    """
    digits = str(number).strip()
    if not digits.isdigit():
        return str(number)
    return " ".join(_UNITS[int(d)] for d in digits)


def say_time(clock: str) -> str:
    """`14:00` -> `las dos de la tarde`; `06:00` -> `las seis de la mañana`.

    Spanish uses the 12-hour clock with a part-of-day phrase, and takes a plural article except at
    one o'clock (`la una`, not `las una`) -- which is the agreement a translation of the English
    helper would get wrong.
    """
    raw = str(clock).strip()
    hour_part, _, minute_part = raw.partition(":")
    if not hour_part.strip().isdigit():
        return raw
    hour = int(hour_part)
    minute = int(minute_part) if minute_part.strip().isdigit() else 0

    if hour == 0:
        base, part = "las doce", "de la noche"
    elif hour == 12:
        base, part = "las doce", "del mediodía"
    else:
        h = hour if hour <= 12 else hour - 12
        base = "la una" if h == 1 else f"las {say_number(h)}"
        part = ("de la mañana" if hour < 12
                else "de la tarde" if hour < 20
                else "de la noche")
    if minute:
        # Spanish names the common fractions rather than counting minutes: half past is "y media"
        # and quarter past is "y cuarto". "las diez y treinta" is understood but is not what anyone
        # says, and on a hotel line it is the sort of phrasing that gives a machine away.
        fraction = {30: "y media", 15: "y cuarto"}.get(minute)
        return f"{base} {fraction} {part}" if fraction else f"{base} y {say_number(minute)} {part}"
    return f"{base} {part}"


def say_date(iso: str) -> str:
    """`2026-09-07` -> `el siete de septiembre`. The year is dropped, as in the other renderers."""
    parts = str(iso).strip().split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return str(iso)
    _, month, day = (int(p) for p in parts)
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return str(iso)
    return f"el {say_number(day)} de {_MONTHS[month - 1]}"


def say_a(phrase: str) -> str:
    """Spanish articles agree with gender, which this layer cannot know for an arbitrary room name.

    Returning the phrase unchanged is the honest option: the templates are written so that no
    article is needed in front of a hotel's proper noun, rather than guessing `un`/`una` and being
    wrong half the time.
    """
    return str(phrase).strip()
