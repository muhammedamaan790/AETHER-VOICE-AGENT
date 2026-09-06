# AETHER — Product Requirements

## 1. Problem

During realtime voice interaction, an agent is frequently mid-flight: thinking, running a tool, or
speaking. Humans interrupt exactly then. A conventional voice stack treats every interruption the
same way — barge-in stops the audio and the turn restarts. That loses the task, discards valid
work, and — worse — lets results computed for the *previous* version of the task arrive late and be
spoken as if they were current.

AETHER solves conversation continuity across those interruptions.

## 2. Users and context

Primary demo persona: a warehouse worker whose hands and eyes are busy (scanner, pallet, ladder).
They cannot look at a screen or type. They speak, they change their minds mid-sentence, they ask
"how many so far?", they say "no, aisle 9 instead".

The engine is domain-agnostic. The warehouse makes voice necessity concrete; general Q&A proves the
engine is not warehouse-specific.

## 3. Goals

- G1. Interpret an interruption's *meaning*, not just its existence.
- G2. Guarantee the golden invariant: a stale result never becomes spoken output.
- G3. Preserve reusable work rather than restarting from zero.
- G4. Produce a machine-checkable event trace that proves G1–G3 happened.
- G5. Speak every judged turn through Rime, observably.

## 4. Non-goals

- A general-purpose voice assistant platform.
- Multiple domain implementations.
- A large warehouse application (WMS, auth, dashboards, CRUD).
- Elaborate UI beyond an observability panel and trace output.
- RAG infrastructure, custom model training, multi-agent architecture.
- Suspend/resume stacks. (Single-slot suspend/resume is an optional Day-5 stretch only.)

## 5. Functional requirements

### FR-1 Conversation
- FR-1.1 Answer general/conversational questions via the LLM knowledge path.
- FR-1.2 Answer warehouse questions via deterministic tools over the synthetic dataset.

### FR-2 Full-duplex audio
- FR-2.1 The user microphone stays active while the agent is speaking.
- FR-2.2 On VAD speech onset, agent audio **ducks immediately**.
- FR-2.3 Agent audio **fully stops** only once a meaningful interruption is confirmed.
- FR-2.4 Duck and full stop are distinct states, distinctly recorded. They are not interchangeable.
- FR-2.5 After a `BACKCHANNEL`, ducked audio resumes; the task is untouched.

### FR-3 Classification
- FR-3.1 Every confirmed interruption is classified into exactly one of the six frozen classes.
- FR-3.2 Every classification emits `InterruptionClassified` with the class and a confidence.
- FR-3.3 Below the confidence threshold, the supervisor takes the **safe** branch (fence) rather
  than allowing potentially stale output.

### FR-4 Generation model and fencing
- FR-4.1 Every unit of work belongs to a generation with a unique, monotonic ID (G1, G2, …).
- FR-4.2 A generation is `active` or `fenced`. No other status exists in the core model.
- FR-4.3 Exactly one generation is `active` at a time.
- FR-4.4 Every result is validity-checked against the active generation before speech.
- FR-4.5 A result whose generation is fenced is discarded and recorded as `ResultDiscarded`.
- FR-4.6 A stale result that nonetheless reaches output is recorded as `ResultLeaked`.

### FR-5 Salvage
- FR-5.1 On `REFINEMENT`, the system attempts to reuse the prior generation's records.
- FR-5.2 Salvage happens only when reuse is genuinely valid (the new constraint is a local filter
  over already-retrieved records).
- FR-5.3 `ResultSalvaged` carries `records_reused`, which may legitimately be `0`.

### FR-6 Cancellation
- FR-6.1 `CANCEL` cancels the task and resolves with `CancellationResolved`.
- FR-6.2 Cancellation is distinct from fencing: fencing invalidates a generation's *output*;
  cancellation ends the *task intent*. Both are recorded separately.

### FR-7 Status
- FR-7.1 `STATUS_QUERY` is answered from live task state.
- FR-7.2 Answering a status query must not change the generation or destroy task state.

### FR-8 Speech output
- FR-8.1 All judged spoken turns use Rime.
- FR-8.2 The active speech provider is observable at runtime and in the trace.
- FR-8.3 If any fallback provider exists, its use is disclosed, never silent.

### FR-9 Observability
- FR-9.1 One canonical event vocabulary is used by the runtime, the trace, the tests, and the
  evaluator. See [ARCHITECTURE.md](ARCHITECTURE.md).
- FR-9.2 Every event carries a monotonic timestamp, generation ID, and turn ID.
- FR-9.3 The trace alone must be sufficient to reconstruct: what the user said, how it was
  classified, which generation was active, when fencing occurred, whether a result was received,
  discarded, salvaged, or leaked, when audio stopped, when cancellation resolved, and what was
  ultimately spoken.

### FR-10 Test-only unsafe mode
- FR-10.1 A test-only switch disables generation fencing.
- FR-10.2 Unsafe mode is never reachable in the judged/demo path and is loudly labelled.
- FR-10.3 The evaluator runs the same delayed-result scenario in both modes: unsafe leaks, safe
  blocks.

## 6. Metrics (definitions only — no values until measured)

| Metric | Definition | Value |
|---|---|---|
| `stale_leak_rate` | `leaked / (discarded + leaked)` | `<from_run>` |
| `audio_kill_latency_ms` | `AudioStopped.t − SpeechOnset.t` | `<from_run>` |
| `duck_latency_ms` | `AudioDucked.t − SpeechOnset.t` | `<from_run>` |
| `classification_latency_ms` | `InterruptionClassified.t − TranscriptFinal.t` | `<from_run>` |
| `tool_latency_ms` | `ResultReceived.t − TaskStarted.t` | `<from_run>` |
| `response_latency_ms` | `ResponseSpoken.t − TranscriptFinal.t` | `<from_run>` |
| `records_reused` | sum of `ResultSalvaged.records_reused` | `<from_run>` |
| `classification_accuracy` | correct / total over the labelled fixture set | `<from_run>` |

Target for `stale_leak_rate` in safe mode is `0`. It may only be written as a result after an
actual run.

## 7. Acceptance criteria

Pre-registered before the demo. Full specifications and result placeholders live in
[RIME_EVIDENCE.md](RIME_EVIDENCE.md).

| ID | Scenario | Pass condition |
|---|---|---|
| A | REFINEMENT | New generation created; old generation fenced; old result discarded, never spoken |
| B | REPLACEMENT | Old generation fenced; new task starts and completes |
| C | STATUS_QUERY | Status answered; generation unchanged; task state intact |
| D | CANCEL | `CancellationResolved` emitted; obsolete output cannot speak |
| E | BACKCHANNEL | Task survives; no generation change; audio ducks and resumes |
| F | NEW_TASK | Answered as a new task; not mislabelled refinement/replacement |
| G | FORCED STALE RESULT | Unsafe mode emits `ResultLeaked`; safe mode emits `ResultDiscarded` for the identical delayed result |

## 8. Live demo requirements

Real microphone, real human speech, real STT, real reasoning path, real Rime output, real
interruption. Traces may accompany the demo as evidence; they may not replace it.
