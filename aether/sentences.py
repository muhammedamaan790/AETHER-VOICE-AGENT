"""Sentence/clause accumulator for streamed LLM output.

Turns a token stream into chunks worth speaking. A TTS engine handed single tokens produces
choppy, unnatural audio and wastes a synthesis round-trip per token; handed a whole paragraph it
cannot start until generation finishes. The useful unit in between is a sentence or a strong
clause.

The rules are deliberately shallow -- this is a boundary detector, not a parser:

* emit on `.` `!` `?` `…` when the next character is whitespace or the stream ends
* emit on `;` and `:` too, which are strong enough to speak across
* never emit on a boundary inside a number (``3.5``), an abbreviation (``Dr.``), an ellipsis
  mid-sentence, or a decimal-like token
* never emit a fragment shorter than `min_chars`, so `Dr.` cannot escape as its own "sentence"

Anything still buffered when the stream ends is flushed, so no text is ever silently dropped.
This module has no knowledge of generations, fencing, audio or providers: it is pure text.
"""

from __future__ import annotations

from collections.abc import Iterator

# Strong boundaries. `;` and `:` are included because a voice agent can naturally pause there,
# and waiting for a full stop can hold back a long first clause.
TERMINATORS = ".!?…;:"

# Trailing tokens that may follow a terminator and still belong to the same sentence.
CLOSERS = '"\')]}»”’'

# Common abbreviations that end in a period without ending a sentence. Short and Anglocentric on
# purpose: a fuller list is a different problem, and `min_chars` already catches most fragments.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt",
    "vs", "etc", "eg", "ie", "approx", "no", "fig", "al",
}

DEFAULT_MIN_CHARS = 12


class SentenceAccumulator:
    """Feed it token text; take back whole sentences.

    Not thread-safe and not meant to be: it is driven by whichever thread is consuming the
    provider's stream.
    """

    def __init__(self, min_chars: int = DEFAULT_MIN_CHARS):
        self.min_chars = min_chars
        self._buf = ""

    @property
    def buffered(self) -> str:
        """Text held back so far. Exposed so a fenced turn can prove it discarded it."""
        return self._buf

    def reset(self) -> None:
        """Drop everything buffered. Used when a generation is fenced mid-stream."""
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return any complete sentences it produced (possibly none)."""
        if not text:
            return []
        self._buf += text
        out: list[str] = []
        while True:
            cut = self._boundary()
            if cut is None:
                break
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:].lstrip()
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> str:
        """Return whatever is left, clearing it. Call once the stream has ended cleanly."""
        remaining, self._buf = self._buf.strip(), ""
        return remaining

    # --- boundary detection -----------------------------------------------------------

    def _boundary(self) -> int | None:
        """Index just past the end of the first complete sentence, or None."""
        for i, ch in enumerate(self._buf):
            if ch not in TERMINATORS:
                continue

            end = i + 1
            while end < len(self._buf) and self._buf[end] in CLOSERS:
                end += 1

            # A terminator only ends a sentence if something follows it -- otherwise the next
            # token may still be arriving and could turn `3.` into `3.5`.
            if end >= len(self._buf):
                return None
            if not self._buf[end].isspace():
                continue

            if self._is_false_boundary(i, ch):
                continue
            if len(self._buf[:end].strip()) < self.min_chars:
                continue
            return end
        return None

    def _is_false_boundary(self, i: int, ch: str) -> bool:
        """A terminator that does not actually end a sentence."""
        if ch != ".":
            return False

        before = self._buf[:i]
        # `3.5`, `1.2.3` -- a digit either side means a number, not a sentence end.
        if before and before[-1].isdigit():
            after = self._buf[i + 1: i + 2]
            if after.isdigit():
                return True

        # `Dr.`, `etc.` -- the last word is a known abbreviation.
        word = before.rsplit(None, 1)[-1] if before.split() else ""
        if word.rstrip(".").lower() in ABBREVIATIONS:
            return True

        # `...` -- an ellipsis mid-thought, not a full stop.
        return before.endswith(".") or self._buf[i + 1: i + 2] == "."


def stream_sentences(chunks: Iterator[str], min_chars: int = DEFAULT_MIN_CHARS) -> Iterator[str]:
    """Convenience: token chunks in, sentences out, with the tail flushed at the end."""
    acc = SentenceAccumulator(min_chars=min_chars)
    for chunk in chunks:
        yield from acc.feed(chunk)
    tail = acc.flush()
    if tail:
        yield tail
