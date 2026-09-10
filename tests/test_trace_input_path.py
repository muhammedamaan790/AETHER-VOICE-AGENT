"""Every new trace must say where its audio came from.

The demo evidence in `evidence/` proves a real telephone call, but only because a second file --
the LiveKit worker log -- happened to survive alongside it. The trace itself could not tell a judge
whether it was recorded over a phone line or off a laptop microphone, and reconstructing that from
a worker log is not evidence anyone should have to do twice.

So a trace now carries `input_path`. These tests fix the three properties that make it worth having:
it is stamped, it is stamped exactly once, and the entry points actually set it. Historical traces
are deliberately NOT rewritten -- a run whose input path was never observed carries no value, which
is the truthful record of what was known at the time.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from aether.events import EventType
from aether.trace import Trace

ROOT = Path(__file__).resolve().parents[1]

# The only two paths audio can currently take into AETHER. "browser" is deliberately absent: the
# web console is a viewer over the same local-microphone session the CLI runs, and no audio travels
# from the browser, so recording "browser" would name the screen an operator was watching rather
# than the route the caller's voice took.
PATHS = {"telephony", "local_microphone"}

ENTRY_POINTS = {
    "aether/telephony/agent.py": "telephony",
    "aether/spike.py": "local_microphone",
    "aether/web/__main__.py": "local_microphone",
}


def _emit(trace: Trace, n: int = 3) -> None:
    for _ in range(n):
        trace.emit(EventType.SPEECH_ONSET, turn_id=1)


def test_a_trace_that_was_never_told_its_path_records_none() -> None:
    """The truthful default. Silence is not a claim, and must never be filled in with a guess."""
    trace = Trace()
    _emit(trace)
    assert trace.input_path is None
    assert all("input_path" not in e.fields for e in trace.events)


def test_the_path_is_stamped_on_the_next_event_after_it_is_declared() -> None:
    trace = Trace()
    trace.input_path = "telephony"
    _emit(trace, 1)
    assert trace.events[0].fields["input_path"] == "telephony"


def test_the_path_is_stamped_exactly_once() -> None:
    """It is a property of the session, not of every event. A 160-event call must carry it once."""
    trace = Trace()
    trace.input_path = "telephony"
    _emit(trace, 20)
    stamped = [e for e in trace.events if "input_path" in e.fields]
    assert len(stamped) == 1
    assert stamped[0] is trace.events[0]


def test_a_path_declared_after_setup_has_already_emitted_is_still_recorded() -> None:
    """The case a "stamp the first event" rule would silently lose.

    An entry point can only declare its path once it knows it, and pipeline setup emits before
    that. If the rule fired only on event 1, those runs would record nothing at all -- which looks
    identical to a run that was never told, and so destroys the field's meaning.
    """
    trace = Trace()
    _emit(trace, 5)                      # setup chatter, before the path is known
    trace.input_path = "telephony"
    _emit(trace, 5)
    stamped = [e for e in trace.events if "input_path" in e.fields]
    assert len(stamped) == 1
    assert stamped[0].seq == 6


def test_the_path_survives_into_the_written_file(tmp_path: Path) -> None:
    """A field only the in-memory object knows would not be evidence. It has to reach the JSONL."""
    trace = Trace.new_run(tmp_path, echo=False)
    trace.input_path = "telephony"
    _emit(trace, 3)
    trace.close()

    # `Event.to_json` flattens per-event fields alongside the envelope, so the stamp appears at
    # the top level of the line rather than nested under a "fields" key.
    lines = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    carrying = [ln for ln in lines if "input_path" in ln]
    assert len(carrying) == 1
    assert carrying[0]["input_path"] == "telephony"


def test_stamping_never_displaces_an_events_own_fields() -> None:
    """The stamp is added to the event, not substituted for it."""
    trace = Trace()
    trace.input_path = "local_microphone"
    trace.emit(EventType.SPEECH_ONSET, turn_id=1, text="hello", reason="onset")
    ev = trace.events[0]
    assert ev.fields["text"] == "hello"
    assert ev.fields["reason"] == "onset"
    assert ev.fields["input_path"] == "local_microphone"


@pytest.mark.parametrize(("source", "expected"), sorted(ENTRY_POINTS.items()))
def test_every_entry_point_declares_its_input_path(source: str, expected: str) -> None:
    """Read as CODE, not as text.

    A grep would be satisfied by the word appearing in a docstring explaining why the field exists.
    Walking the AST for a real assignment to an `input_path` attribute is the only check that
    cannot be passed by prose.
    """
    tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
    found = [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "input_path"
    ]
    assert found == [expected], f"{source} does not declare input_path={expected!r}"
    assert set(found) <= PATHS


def test_historical_evidence_is_left_exactly_as_it_was_recorded() -> None:
    """The demo call predates this field and must not acquire one retroactively.

    Back-filling `input_path` into an old trace would be manufacturing evidence: the value would be
    true, but the file would be claiming the run observed something it never observed.
    """
    demo = ROOT / "evidence" / "demo-run.jsonl"
    if not demo.exists():                      # evidence is not required to run the suite
        pytest.skip("no demo evidence in this checkout")
    lines = [json.loads(line) for line in demo.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "the demo evidence is empty"
    assert all("input_path" not in ln for ln in lines)
