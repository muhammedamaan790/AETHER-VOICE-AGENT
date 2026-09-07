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
                                          cancellation                |
                                                  |                   |
                                                  v                   |
                                          [ Task Runner ] --ResultReceived
                                                  |
                                        [ Menu Router ]
                                          /                                         [ Hotel tools ]      [ LLM knowledge path ]

  every arrow above emits canonical events --> [ Trace ] --> [ Evaluator / Panel ]
                                                   |
                                            [ Web Bridge ] --> the AETHER console
```

The same brain runs behind two transports. On a phone call the microphone and speaker are replaced
by LiveKit tracks, and **nothing else changes**:

```
  PSTN --> LiveKit SIP --> Room --+--> inbound track --> [ InboundBridge ] --> VAD
                                  |                                            (unchanged)
                                  +--< published track <-- [ OutboundBridge ] <-- Audio Gate
```

`InboundBridge` resamples, rebuffers into exact VAD frames, and honours the listening gate;
`OutboundBridge` drives the real `AudioGate._callback` so generation tagging and flush-on-fence
happen in their usual place. LiveKit is transport. It is never the brain, and `AgentSession` is
deliberately not used -- it would supply its own STT, LLM, TTS and turn detection and bypass
everything this document describes.

The **Supervisor** is the only component allowed to change conversational state. The **Output
Gate** is the only path to speech, and it is the single place the golden invariant is enforced.

## 2. Components

| Component | Responsibility | Must not |
|---|---|---|
| VAD | Detect speech onset/offset on the always-open mic | Decide meaning |
| Audio Gate | Duck immediately on onset; full-stop on confirmed interruption; resume after backchannel | Conflate duck with stop |
| STT | Produce final transcripts | Decide meaning |
| Classifier | Map a final transcript plus "is anything in flight" to one of six classes | Mutate state, hold state, or fence |
| Supervisor | Own generations, fencing, cancellation, task transitions | Speak |
| Menu Router | Name a hotel tool and its arguments for a sentence, or decline | Execute anything |
| Task Runner | Execute hotel tools or the LLM knowledge path for a generation | Speak directly |
| Output Gate | Validity-check every result against the active generation, then speak via Rime | Trust its caller |
| Trace | Append-only canonical event log | Invent events |
| Evaluator | Read traces, compute metrics, assert invariants | Write to the runtime |
| Inbound/Outbound Bridge | Carry a transport's audio into and out of the unchanged pipeline | Reimplement VAD, ducking or fencing |
| Web Bridge | Fan the trace out to browsers; accept exactly two controls | Hold a second copy of conversational truth |

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
| `InterruptionClassified` | Classifier produces a class | `interruption_class`, `rule`, `in_flight`, `text` |
| `TaskStarted` | A task begins under a generation | `gen`, `task`, `params` |
| `TaskReplaced` | Active task is superseded (refinement, replacement, or new task) | `reason`, `interruption_class`, `records_salvaged` |
| `FenceRequested` | Supervisor asks that a generation be fenced | `gen`, `reason` |
| `GenerationChanged` | Active generation changes | `from_gen`, `to_gen`, `from_status`, `to_status` |
| `ResultReceived` | A task result arrives at the Output Gate | `gen`, `record_count` |
| `ResultDiscarded` | Result rejected as stale | `gen`, `active_gen`, `reason` |
| `ResultSalvaged` | Records carried from a fenced generation into its successor | `from_gen`, `to_gen`, `records_reused` |
| `ResultLeaked` | A stale result reached output (invariant violation) | `gen`, `active_gen` |
| `AudioDucked` | Agent audio ducked on speech onset | `t` |
| `AudioResumed` | Ducked audio resumed (backchannel path) | `t` |
| `AudioStopped` | Agent audio fully stopped on confirmed interruption | `t`, `gen` |
| `CancellationResolved` | A cancel has fully settled | `gen`, `cancelled`, `had_task_in_flight`, `successor_task` |
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
- `InterruptionClassified` carries **no confidence field**, because the classifier has no
  confidence to report: it is closed-set and whole-utterance, so it either matches a table exactly
  or falls through to `REPLACEMENT`. `rule` records *which* table matched, which is the auditable
  fact a score was meant to stand in for.
- `TaskReplaced.records_salvaged` is always `0` and `ResultSalvaged` is never emitted. Salvage is
  not implemented -- AETHER holds no partial-result store -- and the zero is recorded honestly
  rather than the event being emitted to look like evidence (RULES.md R8).
- `CancellationResolved.cancelled` distinguishes a cancellation that ended a real task from "stop"
  said into silence, which is a legal no-op.
- `FenceRequested.reason` is what separates the triggers that share one fence:
  `voiced_duration_confirmed` (natural barge-in), `button_interrupt` / `keyboard_interrupt`
  (deliberate control), `cancelled_by_caller` (the CANCEL class), `call_disconnected` (hangup),
  and the `stale_*` reasons emitted by the stages that refuse to publish.

### What the trace must prove

| Question | Answered by |
|---|---|
| What did the user say? | `TranscriptFinal.text` |
| How was it classified? | `InterruptionClassified.interruption_class`, and `rule` for why |
| Which generation was active? | `GenerationChanged`, plus `gen` on every event |
| When did fencing occur? | `FenceRequested` then `GenerationChanged` |
| Was a result received? | `ResultReceived` |
| Was it discarded? | `ResultDiscarded` |
| Was anything salvaged? | Nothing ever is; `TaskReplaced.records_salvaged` is `0` and says so |
| Did anything leak? | `ResultLeaked` |
| When did audio duck / stop? | `AudioDucked`, `AudioStopped` |
| When did cancellation resolve? | `CancellationResolved` |
| What was ultimately spoken? | `ResponseSpoken.text`, `ResponseSpoken.provider` |

## 5. Interruption pipeline

Ordered, and the order is part of the contract:

1. VAD detects user speech, emitting `SpeechOnset`.
2. Agent audio is **ducked immediately**, emitting `AudioDucked`. No classification has happened yet.
   (Open-mic only -- see the note on modes below.)
3. STT transcribes, emitting `TranscriptFinal`.
4. Classifier assigns one of the six classes, emitting `InterruptionClassified`.
5. If the class **protects the task** -- `BACKCHANNEL` or `STATUS_QUERY` -- the pipeline stops here.
   No generation is allocated, nothing is fenced, and a `BACKCHANNEL` additionally emits
   `BackchannelDetected` and `AudioResumed` if a duck is outstanding.
6. If the class is `CANCEL`, the active generation is fenced with reason `cancelled_by_caller` and
   **no successor task is started**. `CancellationResolved` records the outcome.
7. Otherwise a new generation is allocated -- which is what fences the previous one -- and the
   Supervisor applies the transition below.
8. Only results belonging to the active generation may be spoken.

### Step 4 comes before step 7, and that is load-bearing

Allocating a generation is what fences the previous one, so classifying *after* allocation would be
classifying a task that had already been destroyed. Two real defects came from that ordering, and
both are fixed by taking the transcript before touching the coordinator:

- "mm-hm" said over an answer killed the answer it was encouraging.
- A noise burst that transcribed to nothing killed the answer too -- the empty transcript returned
  early, but the generation had already been allocated on the way in.

`TranscriptFinal` therefore carries `gen: null`: at the moment STT runs, the generation this
utterance belongs to does not exist yet, and may never exist.

### The duck is mode-dependent; the fence is not

Ducking on onset happens only in `open_mic`, where a duck can be promoted to a fence by sustained
voice. In `hands_free` (the default, and what the phone path runs) neither realtime callback is
wired, so nothing ducks and the answer plays at full volume until a fence stops it. That is
deliberate: a ducked-but-never-fenced utterance takes the resume-and-return path and would be
silently discarded. **Duck only where a fence can follow it.**

All three input modes reach the same `fence_now`, so the generation model, the Audio Gate and every
stale-result guarantee are identical across them. Only the trigger differs, and the trace records
which one fired.

## 6. Supervisor transitions

| Class | Generation | Task | Audio | Distinctive events |
|---|---|---|---|---|
| `BACKCHANNEL` | unchanged | unchanged | **keeps playing** (resume if ducked) | `BackchannelDetected` |
| `REFINEMENT` | `Gn` to `Gn+1` | new task, corrected | stop | `TaskReplaced(reason=refinement)`, `FenceRequested` |
| `REPLACEMENT` | `Gn` to `Gn+1` | new task | stop | `TaskReplaced(reason=replacement)`, `FenceRequested` |
| `STATUS_QUERY` | **unchanged** | unchanged | **keeps playing** | `InterruptionClassified` only |
| `CANCEL` | `Gn` fenced, no successor task | ended | stop | `FenceRequested(reason=cancelled_by_caller)`, `CancellationResolved` |
| `NEW_TASK` | fresh `Gn` | unrelated task | nothing was playing | no `TaskReplaced` -- nothing was displaced |

Tier 1 `NEW_TASK` fences the active task mechanically. It does not suspend it. The old task is not
resumed, and AETHER does not claim it is.

Three rows differ from what an earlier draft of this document specified, and the differences are
deliberate rather than shortfalls:

- **`STATUS_QUERY` does not speak.** Answering aloud would need a second audio path able to bypass
  the Audio Gate's single active generation. Putting a hole in the mechanism that enforces the
  golden invariant in order to say "just a moment" is not a trade worth making. The class protects
  the task; it does not talk over it, and the caller hears the answer they were already waiting for.
- **`REFINEMENT` salvages nothing**, so it behaves exactly as `REPLACEMENT` does. Only the label on
  `TaskReplaced` differs. See RULES.md R8.
- **`NEW_TASK` emits no `TaskReplaced`**, because by definition nothing was in flight to replace.
  Emitting one would record a displacement that did not happen.

**Uncertain classification falls to `REPLACEMENT`, which fences.** There is no confidence threshold
to tune: the classifier matches closed sets exactly or it does not match, and not matching means
falling through to the behaviour AETHER had before the classifier existed. An unnecessary fence
costs a redundant lookup; a missed fence costs a violated invariant.

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

**Specified, and NOT built.** `AETHER_UNSAFE_MODE` is parsed into `RuntimeConfig.unsafe_mode` and
read by nothing: no code path exists that lets a stale result reach output, so there is no check for
it to disable. Setting it changes no behaviour today.

The design stands: it would disable the generation validity check in the Output Gate and nothing
else, so the safe and unsafe runs of acceptance scenario G would differ in exactly one variable.

It has not been implemented because building a real bypass through the gate -- the one mechanism the
golden invariant rests on -- purely so that a control test can fail is not a trade worth making
(RULES.md R11). `test_unsafe_mode_leaks_stale_result` is therefore **skipped rather than passing**,
and says so. `test_safe_mode_blocks_the_same_stale_result` runs, forcing a fence mid-lookup and
asserting the result is discarded, `ResultLeaked` is absent, and nothing enters history.

## 9. Tasks and tools

Two task paths, one supervisor:

- **Hotel menu tools** — deterministic lookups over the structured menu in `aether/hotel/`
  (category, price, diet, availability, allergens, spice level). Tools accept an injectable delay
  so delayed-result races are reproducible. A parallel warehouse fixture (`aether/warehouse/`)
  remains as the second domain that proves the runner is domain-agnostic.
- **LLM knowledge path** — general Q&A. In controlled tests it is backed by a deterministic stub so
  fixtures are reproducible; the live demo uses the real model.

Both return results tagged with the generation that requested them. Neither speaks directly.

### Which path a sentence takes

`aether/hotel/router.py` decides, by keyword and slot, before the model is consulted. It returns a
tool name and arguments, or `None`. It never executes anything, so it can never bypass fencing:
`ToolRunner` runs the tool with the usual fence check, injectable delay and identity stamping, and
`aether/hotel/tools.py` renders the result through a spoken template.

`None` means the LLM takes the sentence — greeting, chit-chat, clarification, an unknown dish, or
anything the router is not confident about. **A wrong tool call is worse than a slower answer**, so
ambiguity always falls through rather than guessing.

Menu facts are kept away from the model for two reasons: the LLM stage measured ~1.8 s that a
lookup does not, and a model asked to read back a price can still say a number the hotel does not
charge — or an allergen that could put somebody in hospital. A menu is a fact table, and facts
belong in a lookup.

### Two rules for anything the tools return

**Structured payloads travel out-of-band, never inside the spoken text stream.** A tool result is a
Python object (`aether.tools.ToolResult`) handed to the supervisor; it is never serialised into the
model's reply for the caller to parse back out. Systems that merge narration and structured output
into one token stream have to latch the boundary mid-stream and stop speaking at the right token —
a whole class of bug that does not exist if the two never share a channel. The model chooses which
tool runs; it does not author the payload, and the payload never passes through the TTS path.

**Optional enrichment is bounded by a deadline; the primary tool call is not.** If a future lookup
is merely *helpful* — cached context, a hint, a secondary source — it is raced against a deadline
and degrades to empty rather than delaying the turn. The primary tool call is never capped that
way: a slow tool is the situation AETHER exists to handle, and the answer to it is interruption and
continuity, not a timeout that hides the problem.

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
