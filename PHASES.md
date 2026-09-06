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
- [~] Basic mic → STT → LLM → Rime loop — VAD/STT/**Rime all working end to end offline**; LLM is still a stub (no credential); not yet run with a live mic
- [x] Latency measurement harness — `scripts/measure_audio_kill.py` (synthetic + mic modes)
- [x] Minimal VAD / ducking spike — WebRTC VAD, immediate duck, duration-confirmed stop

**Checkpoint status:** latency instrumentation exists and produced real numbers. **Rime is
actually used** — the verified contract returns real MP3 audio, which is decoded and played, with
`provider=rime` in the trace. Remaining gaps: the loop has not been run end-to-end with a live
microphone, and the LLM is still a stub pending a provider choice.

First audio-kill measurement happens as soon as the VAD path exists. **Do not fake a Day-1 latency
number.**

---

## Day 2 — Realtime loop + warehouse data

- [ ] VAD
- [ ] Immediate audio duck on speech onset
- [ ] Full stop on confirmed meaningful interruption
- [ ] STT integration
- [ ] Deterministic synthetic warehouse dataset (smallest useful fixture)
- [ ] Tool interface with injectable delay
- [ ] Typed / manual interruption tests, before relying on the classifier

Typed interruption injection comes first deliberately: the supervisor must be testable without the
classifier being correct.

---

## Day 3 — Classification + supervisor

- [ ] `BACKCHANNEL`
- [ ] `REFINEMENT`
- [ ] `REPLACEMENT`
- [ ] `STATUS_QUERY`
- [ ] `CANCEL`
- [ ] `NEW_TASK`
- [ ] Supervisor state transitions (ARCHITECTURE.md §6)
- [ ] Low-confidence safe fallback: prefer fencing over allowing stale output

---

## Day 4 — Fencing + tools + evidence engine

- [ ] Generation IDs
- [ ] Generation validity checks
- [ ] Fencing
- [ ] Stale-result rejection
- [ ] `ResultDiscarded`
- [ ] `ResultLeaked`
- [ ] `ResultSalvaged`
- [ ] `CancellationResolved`
- [ ] Controlled delayed-result tests
- [ ] Test-only unsafe mode
- [ ] Evaluator implementation started

---

## Day 5 — Hardening

- [ ] Integrate all paths
- [ ] Run acceptance tests A–G
- [ ] Fix race conditions
- [ ] Verify full-duplex behavior
- [ ] Verify stale results cannot speak
- [ ] Improve salvage
- [ ] Improve the warehouse scenario
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
