"""Turning what the recogniser produced into something a keyword table can be compared against.

One copy, imported by both `router` and `clarify`. It lived in `router` first and was copied into
`clarify` for an afternoon, and the copy immediately drifted: it used the original `[^\\w\\s-]`
expression, which deletes every Devanagari vowel mark, so "नहीं" normalised to "नह" and a Hindi
caller's "no" stopped being a no. Two copies of a text-preparation rule is two behaviours.
"""

from __future__ import annotations

import unicodedata


def is_kept(ch: str) -> bool:
    r"""Whether `normalise` keeps this character.

    COMBINING MARKS ARE KEPT, and that is the whole reason this is a function rather than the
    one-line `[^\w\s-]` it used to be. Python's `\w` is `str.isalnum()` plus underscore, and a
    Devanagari vowel sign is category Mn/Mc, for which `isalnum()` is False. So the old expression
    deleted every matra and virama: "मेन्यू में क्या है" normalised to "म न य म क य ह", and no
    Hindi keyword could ever match. Hindi routing was impossible, not merely unimplemented.

    Tested by category rather than by codepoint range so the next script -- Tamil, Arabic, Thai --
    works without another edit here.
    """
    return (ch.isalnum() or ch.isspace() or ch in "-_"
            or unicodedata.category(ch).startswith("M"))


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. STT output is not tidy."""
    return " ".join("".join(ch if is_kept(ch) else " " for ch in str(text).lower()).split())
