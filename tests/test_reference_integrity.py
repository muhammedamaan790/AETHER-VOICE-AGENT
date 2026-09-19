"""Every reference a reader can click must go somewhere, and every number must be the current one.

`test_docs_are_current.py` already guards the claims that drift. This guards the *navigation* and
the *derived figures*, which drift the same way and are just as visible to a judge.

Both of the defects this was written for were real and both were shipped:

* The README said fencing happens at "five independent checkpoints" in the executive summary and
  "four independent layers" two hundred lines further down. JUDGING.md said four in three places.
  The source has five, each announcing itself with its own `ResultDiscarded` reason.
* RIME_EVIDENCE.md reported zero leaks across **84** trace files. There were 106. The result was
  unchanged, which is exactly why nobody noticed.

The numbers here are never written down twice: `scripts/metrics.py` derives them from the code, the
traces and the committed evidence, and this file asserts the documents agree with it.

DELIBERATELY NOT CHECKED: anything the repository cannot derive. STT word accuracy on narrowband
audio, live-mic duck latency and concurrent callers are unmeasured, are documented as unmeasured,
and must stay that way rather than acquire a plausible number.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import metrics  # noqa: E402

# Everything a judge is invited to open. Kept in step with test_docs_are_current.DOCS on purpose:
# a document worth checking for stale claims is worth checking for broken links.
DOCS = ["README.md", "AETHER_FAST_REFERENCE.md", "DEMO.md", "DEMO_SCRIPT.md", "JUDGING.md",
        "RIME_EVIDENCE.md", "ARCHITECTURE.md", "DESIGN.md", "RULES.md", "PRD.md", "PHASES.md",
        "SETUP.md", "MEMORY.md", "evidence/README.md"]

# The front door. Its navigation is held to a higher standard than the rest, because it is the one
# page every judge certainly opens.
FRONT = "README.md"


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _anchor(heading: str) -> str:
    """GitHub's heading -> fragment rule: lowercase, drop punctuation, spaces become hyphens.

    Dashes matter and are easy to get wrong. An em dash is *punctuation*, so "Speech output — Rime"
    loses the dash but keeps both surrounding spaces, giving `speech-output--rime` with two hyphens.
    An earlier version of this function kept the em dash (it falls inside the Latin-1 range used to
    preserve accented headings) and reported a correct README link as broken.

    GitHub does NOT collapse runs of whitespace -- each space becomes its own hyphen. So the dash
    in that heading leaves two spaces behind and the fragment is `speech-output--rime`, with two.
    """
    text = heading.strip().lower()
    text = re.sub(r"[`*_\[\]()<>.,:;!?\"'/\\‐-―‘-‟]", "", text)
    text = re.sub(r"[^a-z0-9 \-À-ɏऀ-ॿ]", "", text)
    return text.replace(" ", "-").strip("-")


def _links(text: str):
    """Markdown links, plus href/src in the inline HTML the README uses for its header."""
    for label, target in re.findall(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text):
        yield label, target
    for target in re.findall(r"<(?:a[^>]*?href|img[^>]*?src)=\"([^\"]+)\"", text):
        yield "", target


def _is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:"))


# --- navigation ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name", DOCS)
def test_every_relative_link_points_at_a_file_that_exists(name):
    """A broken link in a document a judge is invited to follow is small and reads badly."""
    base = (ROOT / name).parent
    broken = []
    for label, target in _links(read(name)):
        if _is_external(target) or target.startswith("#"):
            continue
        path = (base / target.split("#")[0]).resolve()
        if not path.exists():
            broken.append(f"[{label}]({target})")
    assert not broken, f"{name} links to files that do not exist:\n  " + "\n  ".join(broken)


def test_every_internal_anchor_in_the_readme_resolves_to_a_heading():
    """The contents list and the in-page cross-references must actually land somewhere.

    Anchors are the easiest thing in a README to break, because renaming a heading is invisible to
    everything except the link that pointed at it.
    """
    text = read(FRONT)
    headings = {_anchor(h) for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.M)}
    # GitHub also generates an anchor for the <div>-wrapped H1 in the header block.
    broken = []
    for label, target in _links(text):
        if not target.startswith("#"):
            continue
        if target.lstrip("#") not in headings:
            broken.append(f"[{label}]({target})")
    assert not broken, ("README anchors that match no heading:\n  " + "\n  ".join(broken)
                        + "\n\nheadings present:\n  " + "\n  ".join(sorted(headings)))


@pytest.mark.parametrize("name", DOCS)
def test_every_cross_document_anchor_resolves(name):
    """A deep link into another document must land on a heading, not just open the file.

    The README's navigation table sends a judge straight to the acceptance summary, the verified
    metrics and the "what we did not build" section. A link that silently degrades to the top of a
    700-line document is worse than no deep link, because nobody notices it happened.
    """
    base = (ROOT / name).parent
    broken = []
    for label, target in _links(read(name)):
        if _is_external(target) or "#" not in target or target.startswith("#"):
            continue
        path_part, fragment = target.split("#", 1)
        target_file = (base / path_part).resolve()
        if not target_file.exists() or target_file.suffix != ".md":
            continue
        headings = {_anchor(h) for h in
                    re.findall(r"^#{1,6}\s+(.+?)\s*$",
                               target_file.read_text(encoding="utf-8"), re.M)}
        if fragment not in headings:
            broken.append(f"[{label}]({target}) -- no such heading in {path_part}")
    assert not broken, f"{name} deep links that miss:\n  " + "\n  ".join(broken)


@pytest.mark.parametrize("name", DOCS)
def test_backticked_repository_paths_exist(name):
    """`aether/hotel/tools.py` in prose is a promise that the file is there.

    Only paths that look unambiguously like repository files are checked -- a leading directory we
    ship, and a real extension. Prose that happens to contain a slash is not a citation.

    `data/` is excluded on purpose, and not out of laziness: it holds two kinds of path that are
    cited correctly but are absent from a clean checkout. `data/aether_learned.db` is created at
    runtime and gitignored (it is one deployment's guesses, not part of the hotel), and
    `data/projects.json` belongs to Rime's own catalogue repository, not to this one.
    """
    pattern = re.compile(r"`((?:aether|tests|scripts|evidence|docs)/[\w./\-]+"
                         r"\.(?:py|md|jsonl|log|svg|html|json))`")
    missing = sorted({m for m in pattern.findall(read(name)) if not (ROOT / m).exists()})
    assert not missing, f"{name} cites files that do not exist:\n  " + "\n  ".join(missing)


def test_the_architecture_image_is_actually_clickable():
    """The README tells the reader to click the diagram to enlarge it.

    An <img> alone does nothing on GitHub inside a <div>, so the instruction has to be backed by a
    real link to the full-resolution file. Written as a property rather than a phrase match: if the
    diagram is shown at all, it is wrapped in a link to itself.
    """
    text = read(FRONT)
    assert "docs/architecture.svg" in text, "the README no longer shows the architecture diagram"
    wrapped = re.search(
        r"<a[^>]*href=\"([^\"]*architecture\.svg)\"[^>]*>\s*<img[^>]*src=\"([^\"]*architecture\.svg)\"",
        text, re.S)
    assert wrapped, ("the architecture <img> is not wrapped in an <a> to the full-size file, so "
                     "'click to enlarge' does nothing")
    assert (ROOT / "docs" / "architecture.svg").exists()


def test_the_architecture_diagram_carries_no_stylesheet():
    """GitHub sanitises <style> out of an SVG in a README, and a class-styled diagram renders there
    as unstyled black text. Every colour must therefore be an inline presentation attribute."""
    svg = (ROOT / "docs" / "architecture.svg").read_text(encoding="utf-8")
    assert "<style" not in svg, "GitHub will strip this and the diagram will render unstyled"
    assert "class=" not in svg, "class selectors cannot survive GitHub's SVG sanitiser"


# --- derived figures ----------------------------------------------------------------------------

def _docs_saying(pattern: str) -> list[tuple[str, str]]:
    found = []
    for name in DOCS:
        for hit in re.findall(pattern, read(name), re.I):
            found.append((name, hit if isinstance(hit, str) else hit[0]))
    return found


_WORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


def _as_int(token: str) -> int:
    return _WORD.get(token.lower(), None) or int(token)


def test_every_document_agrees_on_the_number_of_fence_checkpoints():
    """The defect this was written for: "five independent checkpoints" and "four independent
    layers" in the same README, and "four layers" in JUDGING.md."""
    expected = metrics.fence_checkpoints()
    wrong = [f"{name}: says {said!r}, source has {len(expected)} "
             f"({', '.join(sorted(expected))})"
             for name, said in _docs_saying(
                 r"\b(\w+)\s+independent\s+(?:fence\s+)?(?:checkpoints?|layers?)")
             if _as_int(said) != len(expected)]
    assert not wrong, "stale fence-checkpoint counts:\n  " + "\n  ".join(wrong)


def test_every_document_agrees_on_the_trace_count():
    """RIME_EVIDENCE.md said 84 when there were 106, and the zero-leak result was identical, so
    nothing about the claim looked wrong."""
    actual = metrics.traces()["files"]
    wrong = [f"{name}: says {said}, there are {actual}"
             for name, said in _docs_saying(r"(\d+)\s+(?:recorded\s+)?(?:trace\s+files?|runs?\b)")
             if said.isdigit() and 10 < int(said) < 10000 and int(said) != actual]
    assert not wrong, "stale trace counts:\n  " + "\n  ".join(wrong)


def test_no_document_claims_a_stale_output_has_ever_been_spoken():
    """The central claim, and the one figure that must never be quoted from memory."""
    assert metrics.traces()["leaked"] == 0, (
        "ResultLeaked fired in the committed traces -- the README's headline claim is now false "
        "and must be corrected before anything else"
    )


def test_every_document_agrees_on_the_tool_count():
    actual = metrics.tools()["total"]
    wrong = [f"{name}: says {said}, there are {actual}"
             for name, said in _docs_saying(r"(\d+)\s+(?:hotel\s+)?tools\b")
             if int(said) != actual]
    assert not wrong, "stale tool counts:\n  " + "\n  ".join(wrong)


def test_the_two_latency_figures_are_never_swapped():
    """`turn_latency_ms` and `tts_ms` measure different things and differ by about a second.

    Quoting Rime's first-audio median as the turn latency would overstate the system fourfold, so
    wherever either number appears it must appear against the right words.

    Tight bindings only: a number with the WRONG label directly attached, inside one sentence.
    Proximity was tried twice and produced only false positives -- first a character window that
    spanned the RIME_EVIDENCE table, whose two adjacent rows label both figures correctly, then a
    whole-line rule that flagged a sentence naming both figures correctly in sequence.
    """
    call = metrics.phone_call()
    turn, first = call["median_turn_ms"], call["median_first_audio_ms"]
    # Only the label-BEFORE-number direction, and only within a few words. A number followed by
    # the other label is how a correct sentence reads -- "median turn latency 1262 ms, with Rime
    # first audio at 280 ms" names both correctly, and a looser rule called it a defect.
    bad = [
        (rf"first[- ]audio[^.\n]{{0,14}}\b{turn}\b", f"{turn} ms is the WHOLE TURN"),
        (rf"turn latency[^.\n]{{0,14}}\b{first}\b", f"{first} ms is RIME FIRST AUDIO"),
    ]
    problems = []
    for name in DOCS:
        text = read(name).lower()
        for pattern, why in bad:
            if re.search(pattern, text):
                problems.append(f"{name}: {why}, but the words next to it say otherwise")
    assert not problems, "\n  ".join(problems)
