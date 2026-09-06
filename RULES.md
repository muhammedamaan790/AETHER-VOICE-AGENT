# AETHER — Rules

Non-negotiable rules. Breaking one is a bug, not a trade-off. If a rule appears to block real work,
raise it rather than quietly working around it.

## R1 — The golden invariant

**A stale result must never become spoken output.**

- R1.1 Every unit of work carries the generation ID that requested it.
- R1.2 Every result passes the Output Gate's validity check before speech.
- R1.3 A result whose generation is not the active generation is discarded and logged as
  `ResultDiscarded`.
- R1.4 In safe mode, `ResultLeaked` must never appear in a trace. One occurrence fails the run.
- R1.5 This invariant outranks classifier accuracy. A wrong-but-safe classification is a defect to
  fix later; a leak is a failure now.

## R2 — Fail safe under uncertainty

- R2.1 Below the classifier confidence threshold, take the fencing branch.
- R2.2 When a result's generation cannot be determined, discard it.
- R2.3 Silence is preferable to speaking something possibly stale.

## R3 — One event vocabulary

- R3.1 The canonical list lives in [ARCHITECTURE.md](ARCHITECTURE.md) §4, and machine-readably in
  [aether/events.py](aether/events.py).
- R3.2 Runtime, trace, tests, and evaluator use those names verbatim. No synonyms, no per-module
  local names, no free-form strings.
- R3.3 Adding or renaming an event requires updating both the document and `events.py` in the same
  change.
- R3.4 The trace is append-only. Events are never rewritten to make a run look better.

## R4 — Generation model

- R4.1 Status is `active` or `fenced`. Nothing else.
- R4.2 Exactly one generation is active at a time.
- R4.3 `fenced` is terminal.
- R4.4 `suspended` does not exist unless suspend/resume is actually implemented and tested.

## R5 — Fencing is not cancellation

- R5.1 Fencing invalidates a generation's output; the task intent may continue in a successor.
- R5.2 Cancellation ends the task intent; there is no successor.
- R5.3 A cancel fences, but a fence is not a cancel.
- R5.4 Cancellation resolves explicitly with `CancellationResolved`. A trace that shows only
  fencing has not proven cancellation.

## R6 — Duck is not stop

- R6.1 Duck happens immediately on `SpeechOnset`, before any understanding exists.
- R6.2 Full stop happens only once a meaningful interruption is confirmed.
- R6.3 They are separate events (`AudioDucked`, `AudioStopped`) and separate metrics.
- R6.4 Documentation, code, and demo narration never use the two words interchangeably.

## R7 — Status queries are read-only

- R7.1 `STATUS_QUERY` never changes the active generation.
- R7.2 It never mutates or restarts the underlying task.
- R7.3 It is answered from live task state, not from a re-run.

## R8 — Salvage honesty

- R8.1 Salvage only where reuse is genuinely valid — the new constraint is a local filter over
  records already retrieved.
- R8.2 `records_reused = 0` is a legitimate, expected outcome. Report it.
- R8.3 Never fabricate or inflate a salvage count to make the system look smarter.
- R8.4 Salvage happens at supervisor transition time. It is never a fenced generation's result
  sneaking through the Output Gate.

## R9 — Rime

- R9.1 Rime is the primary and sole TTS in the judged path.
- R9.2 Every spoken turn in the judged flow uses Rime.
- R9.3 The active speech provider is observable at runtime and recorded in `ResponseSpoken.provider`.
- R9.4 If a fallback provider exists at all, its use is disclosed explicitly. Silent fallback is
  forbidden.
- R9.5 Rime endpoint, model, voice, and language are configuration, never hardcoded, and never
  invented — see [RIME_EVIDENCE.md](RIME_EVIDENCE.md).

## R10 — Measurement honesty

- R10.1 Never write a number that did not come from an execution.
- R10.2 Unmeasured values are written as `<from_run>` (or `<measured>`) placeholders.
- R10.3 Metrics may be defined mathematically before they are measured; results may not.
- R10.4 `stale_leak_rate = 0` is a target until a real run produces it, and only then is it a
  result.
- R10.5 Every reported number names the run that produced it.

## R11 — Unsafe mode is test-only

- R11.1 Reachable only via an explicit test/environment switch.
- R11.2 Never enabled in the demo or any judged path.
- R11.3 It disables the Output Gate validity check and nothing else, so safe/unsafe comparisons
  differ in exactly one variable.
- R11.4 Loudly labelled wherever it appears.

## R12 — Scope discipline

Do not build: multiple domain implementations, a large warehouse application, elaborate UI, RAG
infrastructure, custom model training, multi-agent architecture, a suspend/resume stack, or
anything unrelated to interruption and continuity.

General Q&A stays supported. It must not turn the architecture into a generic assistant platform.

## R13 — Claim discipline

- R13.1 Never claim a capability that is not implemented and tested. Suspend/resume and
  spoken-prefix recovery are specifically covered by this rule.
- R13.2 Cut features per the Day-5 cut rule rather than shipping a claim the code does not support.
- R13.3 The live demo is live. Replayed traces are supporting evidence, never a substitute.

## R14 — Secrets

- R14.1 No credentials in the repository, in `.env.example`, in screenshots, or in the recording —
  not for a single frame.
- R14.2 `.env` is git-ignored. Only `.env.example` is committed, with placeholder values.
