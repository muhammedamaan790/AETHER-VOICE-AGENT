# AETHER — Canonical 7-Day Plan

This is the plan of record. Deviating from it is a decision to be recorded in [MEMORY.md](MEMORY.md),
not a silent change.

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done

---

## Day 1 — Foundation + voice spike

- [x] Repository scaffolding
- [x] Acceptance tests defined **before** the demo (see [RIME_EVIDENCE.md](RIME_EVIDENCE.md))
- [x] Architecture and canonical event model
- [~] Rime eligibility / catalog / preflight human checklist — contract + catalog values verified by a human; organizer preflight and rate limits still outstanding
- [x] Basic mic → STT → LLM → Rime loop — VAD/STT/LLM/Rime all working end to end; run headless via `scripts/bench_turn.py` (3/3 turns spoke). **Not yet run with a live mic**
- [x] Latency measurement harness — `scripts/measure_audio_kill.py` (synthetic + mic modes)
- [x] Minimal VAD / ducking spike — WebRTC VAD, immediate duck, duration-confirmed stop

**Checkpoint status:** latency instrumentation exists and produced real numbers. **Rime is
actually used** — `/ws3` streaming is the default transport and `provider=rime` appears in the
trace. The LLM path is live (Gemini). Remaining gap: the loop has not been run end-to-end with a
**live microphone and a human**; every measurement so far is headless.

First audio-kill measurement happens as soon as the VAD path exists. **Do not fake a Day-1 latency
number.**

---

## Day 2 — Realtime loop + warehouse data

- [x] VAD
- [x] Immediate audio duck on speech onset
- [x] Full stop on confirmed meaningful interruption — duration proxy (300 ms), pending the classifier
- [x] STT integration
- [x] Deterministic synthetic warehouse dataset (smallest useful fixture) — `aether/warehouse/`
- [x] Tool interface with injectable delay — `aether/tools/`, fence checked before the tool body runs
- [x] Typed / manual interruption tests, before relying on the classifier — `tests/test_interruption_recovery.py`, `tests/test_warehouse_tools.py`

Typed interruption injection comes first deliberately: the supervisor must be testable without the
classifier being correct.

---

## Day 3 — Classification + supervisor

- [x] `BACKCHANNEL` — withholds a turn; the answer in flight is never fenced
- [x] `REFINEMENT` — labelled, and fences (nothing to salvage)
- [x] `REPLACEMENT` — the safe fallback for anything unrecognised
- [x] `STATUS_QUERY` — withholds a turn; **not** answered aloud, see below
- [x] `CANCEL` — fences with no successor task, emits `CancellationResolved`
- [x] `NEW_TASK` — an ordinary turn with nothing displaced
- [x] Supervisor state transitions (ARCHITECTURE.md §6) — at the turn boundary, not as a module
- [x] Safe fallback: prefer fencing over allowing stale output

Landed 2026-09-08 in `aether/classify/`. Three deviations from the plan, recorded here rather than
made silently:

1. **No confidence score, so no threshold.** The classifier is closed-set and whole-utterance: it
   either matches a table exactly or falls through to `REPLACEMENT`, which fences. The "low-
   confidence safe fallback" is therefore the *default* branch rather than a comparison against
   `AETHER_CLASSIFIER_CONFIDENCE_THRESHOLD`, which is now marked dead in `.env.example`.
2. **Classification runs BEFORE the generation is allocated.** Allocation is what fences, so
   classifying afterwards would classify a task that had already been destroyed. Two real defects
   came from the original ordering — see MEMORY.md §7.
3. **`STATUS_QUERY` protects the task instead of speaking.** ARCHITECTURE.md §6 explains why.

---

## Day 4 — Fencing + tools + evidence engine

- [x] Generation IDs
- [x] Generation validity checks — `GenerationRegistry.is_active`, consulted by every stage
- [x] Fencing — audio-side and tool-side; pulled forward from Day 4 to fix real defects
- [x] Stale-result rejection — LLM, TTS, audio and tool paths
- [x] `ResultDiscarded`
- [x] `ResultLeaked` — defined, asserted absent in every safe-mode scenario, and surfaced in the UI
- [~] `ResultSalvaged` — **CUT.** No partial-result store exists, so `records_reused` would be 0 on
      every run for ever. `TaskReplaced.records_salvaged` carries the honest zero instead (R8)
- [x] `CancellationResolved`
- [x] Controlled delayed-result tests — fence forced mid-lookup, acceptance scenario G safe mode
- [ ] Test-only unsafe mode — **specified, not built.** `AETHER_UNSAFE_MODE` is parsed and honoured
      nowhere; building a real bypass through the gate purely to fail a control test is not a trade
      worth making (R11). Its acceptance test is skipped rather than passing
- [ ] Evaluator implementation started

---

## Day 5 — Hardening

- [x] Integrate all paths — hotel tools, router, classifier and controls are wired into the one loop
- [x] Run acceptance tests A–G — six of eight execute; the two skips name the missing feature
- [x] Verify stale results cannot speak — asserted at the LLM, tool, TTS and gate layers
- [~] Fix race conditions — the console's port release and the post-turn `phase` window were fixed
      (MEMORY.md §7). Concurrency across two simultaneous calls remains unexercised
- [ ] Verify full-duplex behavior — **blocked on a real call**
- [~] Improve salvage — cut, see Day 4
- [x] Improve the scenario — replaced by the hotel menu, which is the product

### Beyond the original plan (2026-09-08)

- [x] Hotel SQLite database, 17 read-only tools, deterministic routing — `aether/hotel/`
- [x] LiveKit↔AETHER audio bridges, synthetic-verified — `aether/bridge/`
- [x] Telephony worker: lifecycle, greeting, teardown — `aether/telephony/`
- [x] One listening toggle + separate INTERRUPT, through the real bridge
- [x] Siri-style console: nine orb states, transcript, evidence strip, reconnect recovery
- [x] Call failure-stage diagnostics — `aether/telephony/diagnostics.py`
- [x] Process prewarm, measured — `aether/prewarm.py`
- [ ] **One real phone call.** Nothing above is evidence about telephony (RIME_EVIDENCE Part 6)
- [ ] *(optional)* Spoken-prefix recovery — only if the core is already solid
- [ ] *(optional)* Single-slot suspend/resume — only if the core is already solid

### CUT RULE

If spoken-prefix recovery is not working reliably by the end of Day 5, **cut it.** It is never part
of the core claim unless actually implemented and tested.

The same applies to suspend/resume. If attempted, it is single-slot only, no stack, with a `RESUME`
classification, suspended generations cannot speak while suspended, and late results are held or
tagged rather than spoken. If it is not solid, cut it — and do not add a `suspended` status to the
generation model.

---

## Day 6 — Evidence + demo

- [ ] Evaluator
- [ ] Observability panel
- [ ] Trace viewer / output
- [ ] Metrics
- [ ] Acceptance-test results filled in from real runs
- [ ] LIVE demo recording

The demo must show:

1. Real voice
2. Interruption
3. Task continuity
4. Refinement / replacement / status / cancel
5. Stale-result protection
6. The warehouse scenario
7. Rime visibly active

Re-check before recording: Rime catalog and preflight, credentials, repository secrets, screenshots,
demo recording, `.env.example`, fallback disclosure.

**Never show credentials, not even for one frame.**

---

## Day 7 — Freeze + rehearsal

- [ ] Feature freeze
- [ ] No new architecture
- [ ] Final test run
- [ ] Final README
- [ ] Final evidence
- [ ] Final demo
- [ ] Rehearse judge questions

### Judge questions to rehearse

- Why is this a hard voice problem?
- Why can't a normal chatbot do this?
- Why is generation fencing necessary?
- Why is fencing different from cancellation?
- What happens when the classifier is wrong?
- How do you prove stale results never speak?
- How do you know the numbers are real?
- Why Rime?
- Why is voice actually necessary?
- What happens if the tool is slow?
- What happens if the user interrupts during speech?
- Can it answer general questions?
- How is the engine reusable outside the warehouse?
