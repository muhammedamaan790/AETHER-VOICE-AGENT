# AETHER

**AETHER solves conversation continuity during realtime voice interaction: users can interrupt,
refine, replace, question, cancel, or start a new task while the agent is working, without allowing
stale state to become spoken output.**

The hard problem is not speech-to-text and it is not text-to-speech. It is what happens when a
human talks *while the agent is thinking, running a tool, or speaking*. The system must decide what
the interruption **means**, update conversational state correctly, and guarantee that work which is
now obsolete never reaches the speaker.

## The golden invariant

> **A stale result must never become spoken output.**

Every unit of work carries a generation ID. Before anything is spoken, the system verifies the
result belongs to the currently valid generation. If it does not, the result is discarded and the
discard is recorded in the event trace.

This invariant matters more than classifier accuracy. When classification is uncertain, AETHER
fails safe: it fences rather than speaks.

## What it does

1. Answers normal conversational / general questions.
2. Handles voice interruption while the agent is speaking.
3. Handles interruption while the agent is thinking or running a tool.
4. Interprets what the interruption means relative to the current task.
5. Preserves useful work when the work is genuinely reusable.
6. Fences obsolete generations.
7. Prevents stale results from reaching spoken output.
8. Answers status questions about the active task.
9. Handles explicit cancellation.
10. Handles new, unrelated tasks.

It does **not** blindly restart everything on every interruption.

## Interruption taxonomy (frozen — six classes)

| Class | Meaning | Effect |
|---|---|---|
| `BACKCHANNEL` | "mhm", "yeah", "okay" | Not a task interruption. Audio may duck, then resume. |
| `REFINEMENT` | Changes/adds a constraint to the current task | New generation; salvage reusable work; fence the old generation |
| `REPLACEMENT` | Swaps the current task for another | Fence previous generation; start new task |
| `STATUS_QUERY` | Asks about progress | Answer status; underlying task stays intact |
| `CANCEL` | Explicitly stops the current task | Cancel and fence; `CancellationResolved` in the trace |
| `NEW_TASK` | Unrelated request | Fence active task (Tier 1: mechanical); answer the new question |

Suspend/resume is **not** implemented and is not claimed. See [PHASES.md](PHASES.md) Day 5.

## Demonstration environment

A small **synthetic warehouse** (orders, priorities, bins, aisles, inventory, pick status). It is a
hands-busy workflow, which makes voice necessity obvious, and it is deterministic, which makes the
continuity problem testable. It is a demo fixture, not a product — the continuity engine is
domain-agnostic and the same engine answers general questions.

## Evidence model

- **LIVE** — the final demo uses a real microphone, real human speech, real STT, a real reasoning
  path, real Rime speech output, and real interruption.
- **CONTROLLED** — deterministic fixtures prove fencing, classification, salvage, cancellation,
  timing, reproducibility, and zero stale leaks.

Replayed traces never substitute for the live demo.

## Speech output

Rime is the primary and sole TTS in the judged path. Every spoken turn in the judged flow uses
Rime, and the active speech provider is observable at runtime. See
[RIME_EVIDENCE.md](RIME_EVIDENCE.md).

## Running the Day-1 spike

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in Rime + LLM values (see RIME_EVIDENCE.md)

python -m aether.spike                              # live loop: mic -> VAD -> STT -> LLM -> Rime
python scripts/measure_audio_kill.py --mode mic     # VAD-onset -> audio-stopped, real speech
python scripts/measure_audio_kill.py --mode synthetic   # same metric, unattended
python scripts/verify_pipeline.py --wav probe.wav   # offline pipeline check, no mic needed
pytest -q                                           # add -m "not audio" to skip device tests
```

**Wear headphones.** The microphone stays open while the agent speaks and there is no acoustic
echo cancellation — on open speakers the agent retriggers its own VAD. AEC is out of scope for the
spike.

Without Rime credentials the agent runs but cannot speak, and says so. There is no fallback TTS.

## Status

**The realtime voice path is built and tested; the continuity engine on top of it is not.**
358 tests pass, 9 are skipped — those 9 are the pre-registered acceptance scenarios, which stay
skipped until the classifier and supervisor exist.

| Built and tested | Not started |
|---|---|
| Always-open mic, WebRTC VAD, noise/duration rejection | Six-class classifier (`aether/classify/`) |
| Local STT (faster-whisper `base.en`) | Supervisor transitions (`aether/supervisor/`) |
| Multi-provider LLM with per-sentence streaming | Evaluator (`aether/evaluator/`) |
| Rime TTS over `/ws3`, persistent connection | Result-side Output Gate, salvage, unsafe mode |
| AudioGate with generation-tagged audio | Acceptance scenarios A–H (specs exist, all skipped) |
| Generation registry + barge-in coordinator | |
| Warehouse fixture + 9 structured tools | |
| Append-only JSONL trace, per-stage telemetry | |
| Observation-only web UI | |

Two things are deliberately **not** claimed. The assembled loop has never been run with a live
microphone and a human — only headless, from a WAV, via `scripts/bench_turn.py`. And the warehouse
tools are complete but not yet wired into the voice loop, because routing an utterance to a tool is
classifier work.

**No measurement in this repository is real until it carries a `<from_run>` value.** See
[MEMORY.md](MEMORY.md) for the authoritative status of what is built, what is measured, and what is
deferred.

## Documents

| File | Purpose |
|---|---|
| [PRD.md](PRD.md) | Product requirements and acceptance criteria |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, generation model, canonical event vocabulary |
| [RULES.md](RULES.md) | Non-negotiable invariants and the rules that enforce them |
| [PHASES.md](PHASES.md) | Canonical 7-day plan with checkpoints and cut rules |
| [DESIGN.md](DESIGN.md) | Design decisions and the reasoning behind them |
| [MEMORY.md](MEMORY.md) | Locked decisions, status, measured vs unmeasured, human tasks |
| [RIME_EVIDENCE.md](RIME_EVIDENCE.md) | Rime checklist and pre-registered acceptance tests |
