# AETHER — Architecture

## 1. Shape of the system

```
  microphone (always open)
        |
     [ VAD ] --SpeechOnset--> [ Audio Gate ] --duck immediately--> speaker
        |                            ^                                ^
        v                            | stop / resume                  |
     [ STT ] --TranscriptFinal-->    |                          [ Rime TTS ]
        |                            |                                ^
        v                            |                                |
  [ Classifier ] --InterruptionClassified--> [ SUPERVISOR ] --> [ Output Gate ]
                                                  |                   ^
                                     generations, fencing,            | validity check
                                     cancellation, salvage            |
                                                  |                   |
                                                  v                   |
                                          [ Task Runner ] --ResultReceived
                                                  |
                                     [ Warehouse tools ] [ LLM knowledge path ]

  every arrow above emits canonical events --> [ Trace ] --> [ Evaluator / Panel ]
```

The **Supervisor** is the only component allowed to change conversational state. The **Output
Gate** is the only path to speech, and it is the single place the golden invariant is enforced.

## 2. Components

| Component | Responsibility | Must not |
|---|---|---|
| VAD | Detect speech onset/offset on the always-open mic | Decide meaning |
| Audio Gate | Duck immediately on onset; full-stop on confirmed interruption; resume after backchannel | Conflate duck with stop |
| STT | Produce final transcripts | Decide meaning |
| Classifier | Map a final transcript + current task context to one of six classes + confidence | Mutate state |
| Supervisor | Own generations, fencing, cancellation, salvage decisions, task transitions | Speak |
| Task Runner | Execute warehouse tools or the LLM knowledge path for a generation | Speak directly |
| Output Gate | Validity-check every result against the active generation, then speak via Rime | Trust its caller |
| Trace | Append-only canonical event log | Invent events |
| Evaluator | Read traces, compute metrics, assert invariants | Write to the runtime |

## 3. Generation model

A **generation** is one version of the current conversational task.

```
status in { active, fenced }        # exactly two states in the core model
```

- IDs are monotonic: `G1`, `G2`, `G3`, ...
- Exactly one generation is `active` at any time.
- `fenced` is terminal. A fenced generation never becomes active again.
- There is **no `suspended` status.** It enters the model only if single-slot suspend/resume is
  actually implemented and tested (Day-5 stretch, see [PHASES.md](PHASES.md)).

Worked example:

```
G1 active     -> task: "find priority orders"     -> tool call dispatched
user: "Actually, only aisle 9."                   -> REFINEMENT
G2 created, active                                -> task: priority orders in aisle 9
G1 fenced
G1's tool result arrives late                     -> ResultReceived(gen=G1)
Output Gate: G1 != active generation              -> ResultDiscarded(gen=G1, reason=stale)
                                                  -> never spoken
```

### Fencing vs cancellation

They are different and must never be collapsed:

- **Fencing** invalidates a generation's *output*. The task intent may continue in a successor
  generation. Recorded as `FenceRequested` + `GenerationChanged`.
- **Cancellation** ends the *task intent* itself. There is no successor. Recorded as
  `CancellationResolved` (a cancel also fences, but fencing alone is not a cancel).

### Fencing vs salvage

Fencing gates **speech**, not **data**. The Supervisor may copy records out of a fenced generation
into its successor *at transition time*, because at that moment it knows those records are still
valid under the new constraint. What is forbidden is a fenced generation's *result* reaching the
Output Gate on its own. Salvage is a deliberate supervisor act; leakage is an accident.

## 4. Canonical event vocabulary

**There is exactly one event vocabulary.** The runtime, the trace, the tests, and the evaluator all
use these names verbatim. The machine-readable source of truth is
[aether/events.py](aether/events.py). Adding, renaming, or removing an event is a change to this
document and that file, together, and never anywhere else.

| Event | Emitted when | Key fields |
|---|---|---|
| `SpeechOnset` | VAD detects user speech start | `t` |
| `SpeechEnded` | VAD detects user speech end | `t` |
| `TranscriptFinal` | STT emits a final transcript | `text` |
| `BackchannelDetected` | Utterance judged a backchannel | `text` |
| `InterruptionClassified` | Classifier produces a class | `class`, `confidence`, `fallback_applied` |
| `TaskStarted` | A task begins under a generation | `gen`, `task`, `params` |
| `TaskReplaced` | Active task is superseded (refinement, replacement, or new task) | `from_gen`, `to_gen`, `reason` |
| `FenceRequested` | Supervisor asks that a generation be fenced | `gen`, `reason` |
| `GenerationChanged` | Active generation changes | `from_gen`, `to_gen`, `from_status`, `to_status` |
| `ResultReceived` | A task result arrives at the Output Gate | `gen`, `record_count` |
| `ResultDiscarded` | Result rejected as stale | `gen`, `active_gen`, `reason` |
| `ResultSalvaged` | Records carried from a fenced generation into its successor | `from_gen`, `to_gen`, `records_reused` |
| `ResultLeaked` | A stale result reached output (invariant violation) | `gen`, `active_gen` |
| `AudioDucked` | Agent audio ducked on speech onset | `t` |
| `AudioResumed` | Ducked audio resumed (backchannel path) | `t` |
| `AudioStopped` | Agent audio fully stopped on confirmed interruption | `t`, `gen` |
| `CancellationResolved` | A cancel has fully settled | `gen`, `outcome` |
| `ResponseSpoken` | Text was spoken | `gen`, `provider`, `text` |

Every event additionally carries: `seq` (monotonic), `t` (monotonic ms), `turn_id`, and `gen`
(nullable for pre-generation audio events).

Consolidation notes, recorded so nobody re-adds a synonym:

- `TaskReplaced` is the single task-transition event. Refinement, replacement, and new-task
  transitions all use it, distinguished by `reason`. There is no `TaskRefined` or `TaskSwitched`.
- `AudioDucked` / `AudioResumed` accompany `AudioStopped` because duck and full stop must be
  distinguishable (PRD FR-2.4) and the duck-latency metric needs its own timestamp.
- `ResponseSpoken` carries `provider`; that field is how Rime's use is observable.
- There is no separate `ToolCallStarted`. `TaskStarted` marks the start for tool-latency purposes.

### What the trace must prove

| Question | Answered by |
|---|---|
| What did the user say? | `TranscriptFinal.text` |
| How was it classified? | `InterruptionClassified.class` |
| Which generation was active? | `GenerationChanged`, plus `gen` on every event |
| When did fencing occur? | `FenceRequested` then `GenerationChanged` |
| Was a result received? | `ResultReceived` |
| Was it discarded? | `ResultDiscarded` |
| Was anything salvaged? | `ResultSalvaged.records_reused` |
| Did anything leak? | `ResultLeaked` |
| When did audio duck / stop? | `AudioDucked`, `AudioStopped` |
| When did cancellation resolve? | `CancellationResolved` |
| What was ultimately spoken? | `ResponseSpoken.text`, `ResponseSpoken.provider` |

## 5. Interruption pipeline

Ordered, and the order is part of the contract:

1. VAD detects user speech, emitting `SpeechOnset`.
2. Agent audio is **ducked immediately**, emitting `AudioDucked`. No classification has happened yet.
3. STT transcribes, emitting `TranscriptFinal`.
4. If the utterance is a backchannel: `BackchannelDetected`, `AudioResumed`, and the pipeline stops
   here. No generation change.
5. Otherwise the interruption is confirmed meaningful, current speech **fully stops**, emitting
   `AudioStopped`.
6. Classifier assigns one of the six classes, emitting `InterruptionClassified`.
7. Supervisor applies the transition below.
8. Only results belonging to the active generation may be spoken.

## 6. Supervisor transitions

| Class | Generation | Task | Audio | Distinctive events |
|---|---|---|---|---|
| `BACKCHANNEL` | unchanged | unchanged | duck then resume | `BackchannelDetected`, `AudioResumed` |
| `REFINEMENT` | `Gn` to `Gn+1` | constraint updated | stop | `TaskReplaced(reason=refinement)`, `FenceRequested`, `ResultSalvaged` (may be 0) |
| `REPLACEMENT` | `Gn` to `Gn+1` | new task | stop | `TaskReplaced(reason=replacement)`, `FenceRequested` |
| `STATUS_QUERY` | **unchanged** | unchanged | stop | `ResponseSpoken` only |
| `CANCEL` | `Gn` fenced, no successor task | ended | stop | `FenceRequested`, `CancellationResolved` |
| `NEW_TASK` | `Gn` to `Gn+1` | unrelated task | stop | `TaskReplaced(reason=new_task)`, `FenceRequested` |

Tier 1 `NEW_TASK` fences the active task mechanically. It does not suspend it. The old task is not
resumed, and AETHER does not claim it is.

Low-confidence fallback: if classifier confidence is below threshold, the Supervisor takes the safe
branch — fence — because an unnecessary fence costs a redundant lookup, while a missed fence costs
a violated invariant.

## 7. Output Gate (where the invariant lives)

```
on ResultReceived(gen):
    if UNSAFE_MODE:                      # test-only, never in the judged path
        speak(result); emit ResponseSpoken
        if gen != active_gen: emit ResultLeaked(gen, active_gen)
        return
    if gen != active_gen or gen.status == fenced:
        emit ResultDiscarded(gen, active_gen, reason="stale")
        return
    speak(result); emit ResponseSpoken(gen, provider="rime")
```

`ResultLeaked` exists so a leak is *detectable and countable*, not so it is acceptable. In safe mode
it must never appear.

## 8. Test-only unsafe mode

Enabled only by an explicit test/environment switch (`AETHER_UNSAFE_MODE`), never by configuration
used in the demo. It disables the generation validity check in the Output Gate and nothing else, so
the safe and unsafe runs of acceptance scenario G differ in exactly one variable.

## 9. Tasks and tools

Two task paths, one supervisor:

- **Warehouse tools** — deterministic lookups over a small synthetic dataset (orders, priorities,
  bins, aisles, inventory, pick status). Tools accept an injectable delay so delayed-result races
  are reproducible.
- **LLM knowledge path** — general Q&A. In controlled tests it is backed by a deterministic stub so
  fixtures are reproducible; the live demo uses the real model.

Both return results tagged with the generation that requested them. Neither speaks directly.

## 10. Stack and concurrency model

Python 3.11+, chosen for audio-library availability and pytest.

### Realtime audio/control path: threaded and callback-friendly

The realtime audio and control path is **threaded and callback-friendly, not asyncio-first**. This
is a locked decision (MEMORY.md §1), taken from the implementation rather than ahead of it:

- PortAudio invokes the audio callback on its own realtime thread. Duck and stop state must be
  readable and actionable *from inside that callback*.
- The callback path must therefore not depend on an asyncio event loop being alive, scheduled, or
  reachable. Waiting on a loop to service a duck would put the golden invariant behind a scheduler.
- Duck/stop are consequently plain flags: `request_duck()` / `request_stop()` are a bool assignment
  and are safe to call directly from the VAD's input callback. The output callback applies the
  change and stamps when it took effect. A watcher thread turns those stamps into trace events, so
  no logging or allocation happens on the realtime thread.

### Elsewhere

**This does not forbid asyncio.** Outside the realtime audio callback — supervisor state, tool
calls, LLM and TTS requests — asyncio remains available and appropriate where it fits. Concurrency
is chosen per component; only the realtime audio/control path is constrained by the above.

Concrete VAD / STT library choices are recorded in [MEMORY.md](MEMORY.md) §10.
