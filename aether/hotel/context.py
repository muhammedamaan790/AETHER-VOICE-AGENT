"""What "it" refers to: one remembered subject, and the rules for when it may be used.

THE PROBLEM. The router is a pure function of one sentence, which is what makes it fast and
impossible to be wrong for interesting reasons. It is also why "how much is the chicken kebab?"
followed by "is it available tonight?" used to answer *"we have forty-one rooms free"* -- "available
tonight" is a room question, and nothing carried the kebab forward. The demo sheet carried a rule
telling the presenter never to use a pronoun, which is a workaround for a defect, not a feature.

THE RISK, AND WHY THIS IS SMALL. Memory is where a deterministic router starts guessing. A system
that remembers the wrong subject answers confidently about the wrong thing, which is worse than
falling through to the model. So this is deliberately the most conservative version that fixes the
actual complaint:

* **One subject, not a history.** The last concrete thing the caller was told about. No stack, no
  scoring, no "probably the kebab".
* **It is only consulted when the sentence has no subject of its own.** Name anything -- a dish, a
  room, a category, a policy -- and that wins outright. Substitution happens only into a genuine
  gap.
* **It is only consulted for an explicit referring word** (`it`, `that`, `the same`, `वो`, `eso`).
  An elliptical fragment with no pronoun at all still falls through to the model, because guessing
  what an unmarked fragment refers to is exactly the class of confident error this avoids.
* **It is committed at the SPOKEN boundary, never before.** A fenced turn leaves no subject behind,
  for the same reason it leaves no history behind: the caller never heard it, so it is not part of
  the conversation. This is the golden invariant applied to reference rather than to output, and it
  falls out of putting the update next to `history.commit_turn` rather than in the router.

WHAT IT DELIBERATELY DOES NOT DO. No coreference across three turns, no "the first one", no plurals
resolved to a set. Those need a model, and a model in this path is what the whole project is built
to avoid for facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words that mean "the thing we were just talking about". Kept small and explicit: every entry here
# is a licence to answer a question the caller did not fully ask, so the bar is that a person could
# not reasonably have meant anything else.
#
# "one" is absent on purpose -- "how much is one" and "one night" and "room one zero one" all
# contain it, and the failure mode is answering about a dish when the caller said a number.
_REFERRING = (
    # English
    "it", "that", "this", "them", "those", "these", "the same", "that one",
    # Hindi, in both scripts. वो/वह "that", ये/यह "this", इसका/उसका "its".
    "वो", "वह", "ये", "यह", "इसका", "उसका", "इसकी", "उसकी", "इसमें", "उसमें",
    "wo", "woh", "yeh", "iska", "uska",
    # Spanish. "eso"/"esa"/"ese" that, "lo mismo" the same. Subject pronouns are usually dropped in
    # Spanish, which is why "¿está disponible?" also has to work -- see `_SUBJECTLESS`.
    "eso", "esa", "ese", "esto", "esta", "lo mismo", "la misma",
)

# Spanish and Hindi drop the subject far more readily than English does, so a bare "¿está
# disponible?" is a perfectly ordinary way to ask "is it available?". These are the few frames where
# the subject is unambiguously elided rather than merely absent -- each one is a complete question
# in its language that cannot be about anything but the previous subject.
_SUBJECTLESS = (
    "esta disponible", "está disponible", "cuanto cuesta", "cuánto cuesta",
    "esta libre", "está libre", "tiene", "lleva",
)

_REFERRING_PATTERNS = tuple(
    re.compile(rf"(?<!\w){re.escape(word)}(?!\w)") for word in
    sorted(_REFERRING, key=len, reverse=True)
)


@dataclass
class Subject:
    """The last concrete thing the caller was told about, and how to put it back into a sentence.

    `phrase` is the text spliced in where the pronoun was -- a dish name, a room type, "room three
    zero five". It is deliberately the words the ROUTER matches rather than the words AETHER speaks:
    the point is to reconstruct the question the caller meant, and the router's own vocabulary is
    the only thing guaranteed to route.
    """

    phrase: str | None = None
    kind: str | None = None          # "dish" | "room_type" | "room" | "category" | "policy"

    def remember(self, phrase: str | None, kind: str | None) -> None:
        if phrase:
            self.phrase, self.kind = phrase, kind

    def forget(self) -> None:
        self.phrase, self.kind = None, None

    def __bool__(self) -> bool:
        return bool(self.phrase)


def refers_back(spoken: str) -> bool:
    """Whether this sentence points at something it does not name."""
    if any(pattern.search(spoken) for pattern in _REFERRING_PATTERNS):
        return True
    return any(spoken.strip() == frame or spoken.strip().startswith(frame + " ")
               for frame in _SUBJECTLESS)


def resolve(spoken: str, subject: Subject | None) -> str:
    """Put the remembered subject back into a sentence that points at it, or return it unchanged.

    Returns the sentence untouched whenever anything is uncertain -- no subject remembered, no
    referring word, or a sentence that already names something. "Unchanged" then falls through the
    normal router rules, and if they do not match, the model answers. That is the safe direction.
    """
    if not subject or not subject.phrase:
        return spoken
    if not refers_back(spoken):
        return spoken

    # The pronoun is replaced in place rather than appended, so word order survives: "is it
    # available" becomes "is chicken kebab available", which the availability rule already matches.
    for pattern in _REFERRING_PATTERNS:
        if pattern.search(spoken):
            return pattern.sub(subject.phrase, spoken, count=1)

    # A subjectless frame: the subject goes where the language would have put it.
    return f"{spoken} {subject.phrase}"
