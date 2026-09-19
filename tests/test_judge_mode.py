"""Judge mode shows the engine's own state, or it shows a dash. It never shows a plausible number.

Judge mode is the console's evidence view: the live pipeline stage, the generation timeline with
ACTIVE/FENCED, whether the turn was answered deterministically or by the model, and the latency
breakdown. It is the most persuasive thing in the project *and* the easiest place to accidentally
lie, because a hardcoded "0.90 ms" would look exactly like a measured one.

So the properties guarded here are about honesty rather than appearance:

* the route panel names a tool only when the engine reported one;
* every figure comes from the snapshot, so the static markup carries no numbers at all;
* the panels are empty in the HTML and filled from state;
* the "Why?" drawer cites files that exist;
* and the console still works with judge mode off, which is how it ships.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aether.web.server import UiState

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "aether" / "web" / "static" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return CONSOLE.read_text(encoding="utf-8")


def _spoken(**fields):
    base = {"type": "ResponseSpoken", "turn_id": 1, "gen": "G1", "text": "hello",
            "provider": "rime", "llm_provider": "gemini:flash"}
    base.update(fields)
    return base


# --- the route a turn actually took ---------------------------------------------------------------

def test_the_deterministic_tool_reaches_the_snapshot():
    """Without this the console could only say "no model ran", not which tool answered instead."""
    state = UiState()
    state.apply(_spoken(route_tool="price_of", llm_ms=0.0, stt_ms=900.0))
    snap = state.snapshot()
    assert snap["route_tool"] == "price_of"
    assert snap["latency"]["llm_ms"] == 0.0, "a deterministic turn still records a zero LLM span"


def test_a_model_turn_reports_no_tool():
    """`route_tool` is None on the fallback path, and the console must print the model instead."""
    state = UiState()
    state.apply(_spoken(route_tool=None, llm_ms=1386.0))
    snap = state.snapshot()
    assert snap["route_tool"] is None
    assert snap["llm_provider"] == "gemini:flash"


def test_a_tool_name_never_carries_over_onto_a_later_model_turn():
    """The defect this prevents: a deterministic turn followed by a model turn would leave the
    previous tool's name on screen beside the word DETERMINISTIC, which is a false claim about
    where the answer came from."""
    state = UiState()
    state.apply(_spoken(gen="G1", route_tool="price_of", llm_ms=0.0))
    assert state.snapshot()["route_tool"] == "price_of"
    state.apply(_spoken(gen="G2", route_tool=None, llm_ms=1200.0))
    assert state.snapshot()["route_tool"] is None, "the tool name survived into a model turn"


def test_the_turn_loop_clears_the_tool_name_every_turn():
    """The console can only be honest if the engine resets it. Asserted at the source, because the
    reset is a single line that a later edit could quietly drop."""
    spike = (ROOT / "aether" / "spike.py").read_text(encoding="utf-8")
    assert "self._route_tool = None" in spike, (
        "the per-turn reset is gone -- a tool name can now leak onto a model turn"
    )
    assert "route_tool=getattr(self, \"_route_tool\", None)" in spike, (
        "ResponseSpoken no longer carries the routed tool"
    )


def test_a_fenced_turn_contributes_no_route(  ):
    """A discarded turn produces no ResponseSpoken, so nothing about a route is claimed for it."""
    state = UiState()
    state.apply({"type": "ResultDiscarded", "gen": "G4", "reason": "stale_generation_tool"})
    snap = state.snapshot()
    assert snap["route_tool"] is None
    assert snap["last_discard"] == "stale_generation_tool"
    assert snap["transcript"][-1]["status"] == "interrupted"


# --- the UI cannot invent a figure ----------------------------------------------------------------

def test_judge_mode_is_off_until_it_is_switched_on(html):
    """It ships off, so a demo never depends on a display option being in the right state."""
    assert 'id="judgeBtn"' in html and 'aria-pressed="false"' in html
    assert re.search(r"\.judge-only\{display:none\}", html), "judge panels must be hidden by default"
    assert "<body class=" not in html, "the body must not start in judge mode"


@pytest.mark.parametrize("panel", ["gens", "route", "lat", "intr"])
def test_every_judge_panel_is_empty_in_the_markup(html, panel):
    """Filled from the snapshot at runtime and from nowhere else.

    A number typed into the HTML would render identically to a measured one, and would keep
    rendering after the engine stopped reporting it.
    """
    m = re.search(rf'<div class="[^"]*" id="{panel}">(.*?)</div>', html, re.S)
    assert m, f"#{panel} is missing from the console"
    assert not m.group(1).strip(), f"#{panel} ships with content in the markup: {m.group(1)!r}"


def test_the_latency_panel_renders_a_dash_for_anything_unreported(html):
    """`—` rather than 0. A turn the engine reported nothing for has no measurement, and zero is a
    measurement of something that did not happen."""
    body = html.split("function renderLatency", 1)[1].split("function ", 1)[0]
    assert "—" in body, "the latency panel has no em-dash branch"
    assert "value !== undefined && value !== null" in body, (
        "the dash must be chosen by presence, not by falsiness -- 0 ms is a real measurement"
    )


def test_the_route_panel_will_not_name_a_tool_it_was_not_given(html):
    body = html.split("function renderRoute", 1)[1].split("function ", 1)[0]
    assert "s.route_tool ?" in body, "the tool name must be conditional on the snapshot field"
    assert "—" in body, "there must be a dash branch when no tool was reported"


def test_the_pipeline_does_not_claim_finer_resolution_than_the_events_carry(html):
    """The engine reports ONE `thinking` phase covering routing, the tool and rendering. Lighting
    those independently would be invented detail, so they light together."""
    body = html.split("function stagesFor", 1)[1].split("function ", 1)[0]
    assert '"classifier", "router", "answer", "render"' in body, (
        "the thinking stages must light as one group, because one event covers them"
    )


# --- the evidence drawer cites real things ---------------------------------------------------------

def test_every_file_the_why_drawer_cites_exists(html):
    """The drawer's whole purpose is that a judge can go and look. A path that 404s is worse than
    no citation. Paths and named symbols only -- never line numbers, which go stale on the next
    edit to an unrelated part of the file."""
    table = html.split("const WHY = {", 1)[1].split("\n};", 1)[0]
    cited = re.findall(r"\b((?:aether|tests|scripts|evidence|docs)/[\w./\-]+"
                       r"\.(?:py|md|jsonl|log|svg|json))\b", table)
    assert cited, "the Why drawer cites no files at all"
    missing = sorted({p for p in cited if not (ROOT / p).exists()})
    assert not missing, "the Why drawer cites files that do not exist:\n  " + "\n  ".join(missing)


def test_the_why_drawer_cites_no_line_numbers(html):
    table = html.split("const WHY = {", 1)[1].split("\n};", 1)[0]
    stale = re.findall(r"[\w/]+\.py:\d+", table)
    assert not stale, f"line-numbered citations go stale immediately: {stale}"


def test_the_leak_counter_is_driven_by_events_not_decoration(html):
    """"Stale leaks: 0" is the headline claim. It must come from the fold, which counts
    `ResultLeaked`, and never be a literal that stays zero because nothing updates it."""
    state = UiState()
    assert state.snapshot()["leaks"] == 0
    state.apply({"type": "ResultLeaked", "gen": "G9", "reason": "test"})
    assert state.snapshot()["leaks"] == 1, "the counter is not wired to the event"
    assert "els.factLeaks.textContent" in html, "the UI must write the counter from state"


def test_the_snapshot_stays_json_serialisable_with_the_new_field():
    state = UiState()
    state.apply(_spoken(route_tool="room_price", llm_ms=0.0))
    json.dumps(state.snapshot())
