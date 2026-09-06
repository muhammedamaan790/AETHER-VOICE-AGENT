# AETHER — Rime + Acceptance Evidence

Created Day 1. Acceptance tests are pre-registered here **before** any demo run. Result fields stay
blank until an actual execution fills them in. Nothing in this file may be filled in from
expectation — see [RULES.md](RULES.md) R10.

---

## Part 1 — Rime configuration

Configured via environment variables, never hardcoded. See [.env.example](.env.example).

| Setting | Env var | Value |
|---|---|---|
| Endpoint | `RIME_API_URL` | `https://users.rime.ai/v1/rime-tts` (VERIFIED) |
| Model | `RIME_MODEL` | `mistv2` (VERIFIED) |
| Voice | `RIME_VOICE` | `astra` (VERIFIED) |
| Language | `RIME_LANGUAGE` | `eng` (VERIFIED) |
| API key | `RIME_API_KEY` | secret — never committed, never shown |
| Response format | — | raw MP3 bytes in the body; `Accept: audio/mp3` (VERIFIED) |
| Response sample rate | — | carried by the MP3; observed 16000, resampled to the AudioGate rate |

**Status: Rime is connected and working.** A human verified the contract against a live 200
response that played real audio, and `aether/audio/rime.py` now implements exactly that contract:

    POST https://users.rime.ai/v1/rime-tts
    Authorization: Bearer <RIME_API_KEY>      (secret; .env only, never committed)
    Content-Type: application/json
    Accept: audio/mp3
    body: {"text": ..., "modelId": "mistv2", "speaker": "astra", "lang": "eng"}
    response: raw MP3 bytes

Nothing else is sent. The four non-secret values remain configuration, not literals (R9.4/R9.5).
There is still no fallback TTS: unconfigured means silence, not substitution.

> **Still outstanding (human):** organizer preflight, account tier / rate limits, and the Day-6
> re-check. Those are separate from the contract verification above and have NOT been done.

---

## Part 2 — Human-only checklist

These require a human. Do not mark them complete on the basis of anything an agent did.

### Rime catalog verification
- [x] Confirm the Rime API endpoint currently in use — `https://users.rime.ai/v1/rime-tts`
- [x] Confirm the model ID exists in the live catalog — `mistv2`
- [x] Confirm the voice ID exists and is available to this account — `astra`
- [x] Confirm the language code is supported for that model and voice — `eng`
- [ ] Confirm the account tier and any rate limits that affect a live demo
- [ ] Record the date of verification and re-check on Day 6

### Organizer / event preflight
- [ ] Confirm eligibility requirements for the Rime track
- [ ] Complete any organizer preflight or registration step
- [ ] Confirm submission format, deadline, and demo length
- [ ] Confirm what evidence judges expect to see

### Active provider observability
- [x] `ResponseSpoken.provider` is emitted for every spoken turn — observed as `provider=rime`
- [ ] The observability panel shows the active speech provider live
- [ ] A judge can see Rime is active without taking our word for it

### Fallback disclosure
- [x] Determine whether any TTS fallback exists in the codebase — **none exists**; unconfigured Rime raises `RimeNotConfigured` and the agent stays silent
- [ ] If none exists, state that plainly in README and demo narration
- [ ] If one exists, it is explicitly disclosed and visibly labelled when used — never silent
- [ ] Confirm every judged turn used Rime, from the trace, after the demo run

### Differentiation review
- [ ] **TODO (human):** Review the current Rime Voice AI project catalog and confirm AETHER is not
      a close reproduction of an existing project. This review has **not** been performed. Record
      the date, what was reviewed, and the conclusion here when it is.

### Secrets hygiene
- [ ] `.env` is git-ignored and has never been committed (`git log --all -- .env` is empty)
- [ ] `.env.example` contains placeholders only
- [ ] No credentials in screenshots, panel, terminal output, or the recording — not for one frame
- [ ] Repository scanned for secrets before Day 6 recording

---

## Part 3 — Pre-registered acceptance tests

Every scenario asserts on the canonical event vocabulary
([ARCHITECTURE.md](ARCHITECTURE.md) §4). Mode is `safe` unless stated otherwise.

Common assertion for every safe-mode scenario: **no `ResultLeaked` event appears in the trace.**

---

### A — REFINEMENT

**Setup:** Active task under `G1` (warehouse order search) with a delayed tool result.
**Interruption:** "Actually, only aisle 9."

| Assertion | Result |
|---|---|
| `InterruptionClassified.class == REFINEMENT` | `<from_run>` |
| `G2` created and active; `G1` fenced | `<from_run>` |
| `G1`'s late result yields `ResultDiscarded`, not `ResponseSpoken` | `<from_run>` |
| `ResultSalvaged.records_reused` recorded (may be 0) | `<from_run>` |
| No `ResultLeaked` | `<from_run>` |
| `audio_kill_latency_ms` | `<from_run>` |

---

### B — REPLACEMENT

**Setup:** Active task under `G1`.
**Interruption:** "Forget that, find order 4812 instead."

| Assertion | Result |
|---|---|
| `InterruptionClassified.class == REPLACEMENT` | `<from_run>` |
| `FenceRequested(G1)` then `GenerationChanged(G1 to G2)` | `<from_run>` |
| New task starts under `G2` and completes | `<from_run>` |
| `G1`'s result, if it arrives, is discarded | `<from_run>` |
| No `ResultLeaked` | `<from_run>` |

---

### C — STATUS_QUERY

**Setup:** Active task under `G1`, tool in flight.
**Interruption:** "How many have you found so far?"

| Assertion | Result |
|---|---|
| `InterruptionClassified.class == STATUS_QUERY` | `<from_run>` |
| Active generation is **unchanged** (still `G1`) | `<from_run>` |
| No `FenceRequested`, no `TaskReplaced` | `<from_run>` |
| Status is spoken from live task state | `<from_run>` |
| `G1` completes normally afterwards and may speak | `<from_run>` |

---

### D — CANCEL

**Setup:** Active task under `G1` with a delayed result.
**Interruption:** "Stop." / "Cancel that."

| Assertion | Result |
|---|---|
| `InterruptionClassified.class == CANCEL` | `<from_run>` |
| `CancellationResolved` is emitted | `<from_run>` |
| `G1` fenced; no successor task started | `<from_run>` |
| Late `G1` result is discarded, never spoken | `<from_run>` |
| Cancellation is distinguishable from plain fencing in the trace | `<from_run>` |

---

### E — BACKCHANNEL

**Setup:** Agent speaking under `G1`.
**Interruption:** "mhm" / "yeah" / "okay"

| Assertion | Result |
|---|---|
| `BackchannelDetected` emitted | `<from_run>` |
| `AudioDucked` then `AudioResumed` | `<from_run>` |
| No `AudioStopped` (no full stop) | `<from_run>` |
| Active generation unchanged; task intact | `<from_run>` |
| No `FenceRequested` | `<from_run>` |

---

### F — NEW_TASK

**Setup:** Active warehouse task under `G1`.
**Interruption:** "What's the capital of Japan?" (also: "What's 17 percent of 840?")

| Assertion | Result |
|---|---|
| `InterruptionClassified.class == NEW_TASK` (not refinement, not replacement) | `<from_run>` |
| `TaskReplaced(reason=new_task)` — logged distinctly | `<from_run>` |
| `G1` fenced mechanically (Tier 1); no suspend is claimed | `<from_run>` |
| The new question is answered via the LLM knowledge path | `<from_run>` |
| No `ResultLeaked` | `<from_run>` |

---

### G — FORCED STALE RESULT (controlled, two modes)

**Setup:** Identical fixture in both runs — `G1` task with an injected delay long enough that its
result arrives after the interruption. The **only** difference between runs is
`AETHER_UNSAFE_MODE`.

**G-unsafe** (`AETHER_UNSAFE_MODE=1`, test-only):

| Assertion | Result |
|---|---|
| The stale result reaches output | `<from_run>` |
| `ResultLeaked` is emitted | `<from_run>` |
| `stale_leak_rate > 0` | `<from_run>` |

**G-safe** (default):

| Assertion | Result |
|---|---|
| The identical delayed result is blocked | `<from_run>` |
| `ResultDiscarded` is emitted | `<from_run>` |
| No `ResultLeaked` | `<from_run>` |
| `stale_leak_rate == 0` | `<from_run>` |

---

### H — GENERAL Q&A REFINEMENT (zero-salvage case)

**Setup:** General question answered via the knowledge path under `G1`.
**Interruption:** a correction that changes the query identity (e.g. correcting the year).

| Assertion | Result |
|---|---|
| Classified as `REFINEMENT` | `<from_run>` |
| `G1` fenced; fresh lookup under `G2` | `<from_run>` |
| `ResultSalvaged.records_reused == 0` — recorded honestly, not inflated | `<from_run>` |
| The `G1` answer is never spoken as current | `<from_run>` |

---

## Part 4 — Metrics from runs

Definitions are in [PRD.md](PRD.md) §6. Values here come only from execution, and each names its
run.

| Metric | Run ID | Value |
|---|---|---|
| `stale_leak_rate` (safe) | — | `<from_run>` |
| `stale_leak_rate` (unsafe control) | — | `<from_run>` |
| `audio_kill_latency_ms` — **synthetic mode** | `run-20260906T020258Z` | n=8: min 301.65, median 321.24, max 333.18 |
| `duck_latency_ms` — **synthetic mode** | `run-20260906T020258Z` | n=8: min 1.27, median 21.95, max 30.39 |
| `audio_kill_latency_ms` — **live mic, human speech** | — | `<from_run>` |
| `duck_latency_ms` — **live mic, human speech** | — | `<from_run>` |
| `classification_latency_ms` | — | `<from_run>` |
| `tool_latency_ms` | — | `<from_run>` |
| `response_latency_ms` | — | `<from_run>` |
| `classification_accuracy` | — | `<from_run>` |
| `records_reused` (total) | — | `<from_run>` |

**Conditions for `run-20260906T020258Z`** (`scripts/measure_audio_kill.py --mode synthetic
--repeats 8`), Windows 11 / Python 3.13.2, WASAPI output @48 kHz, WebRTC VAD aggressiveness 2:

- Synthetic voiced frames were injected into `MicVAD.process_frame` at real-time pacing while a
  test tone played through the real output device. Same VAD, same AudioGate as the live path.
  **This is not a live-microphone measurement and does not substitute for one.**
- Excludes microphone capture latency (upstream of `SpeechOnset.t`).
- Excludes audio already buffered past the callback: `stream_output_latency_ms = 22.0`.
- `audio_kill_latency_ms` is dominated by the deliberate 300 ms confirmation policy
  (`MEANINGFUL_SPEECH_MS`), which the Day-3 classifier replaces. The reaction figure is the duck.

Not a benchmark, but observed: `tiny.en` transcribed a 3.13 s fixture in 423–430 ms on CPU.
Superseded configuration — STT is now `base.en` with `beam_size=5`, which has not been measured.

---

## Part 5 — Live demo evidence

| Item | Status |
|---|---|
| Real microphone | `<from_run>` |
| Real human speech | `<from_run>` |
| Real STT | `<from_run>` |
| Real reasoning path | `<from_run>` |
| Real Rime speech output | `<from_run>` |
| Real interruption | `<from_run>` |
| Trace captured for the demo run | `<from_run>` |
| Every judged spoken turn used Rime (verified from trace) | `<from_run>` |
| Recording link | TODO |
