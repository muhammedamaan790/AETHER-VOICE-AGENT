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
| Model | `RIME_MODEL` | **`mistv3`** — VERIFIED against the live catalog on 2026-09-08 (see Part 2). `mistv2` remains a one-line fallback |
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

### Part 1a — model change to `mistv3` (2026-09-07)

Switched from `mistv2` after measuring both through this project's own `/ws3` client, voice
`astra`, 4 sentences each, warm connection:

| Model | Cold start | Warm first-audio | Audio produced | Speaking rate | Errors |
|---|---|---|---|---|---|
| `mistv2` | 2649 ms | 488 ms | 8.05 s | 17.2 chars/s | 0/4 |
| **`mistv3`** | **1438 ms** | **308 ms** | 10.72 s | 12.9 chars/s | 0/4 |
| `coda` | 3774 ms | 312 ms | 10.84 s | 12.7 chars/s | 0/4 |

`mistv3` is **180 ms faster to first audio** (37%), which is the quantity `turn_latency_ms`
measures. The trade is a **33% slower speaking rate** — it takes longer to finish the same text.
That trade favours AETHER because replies are capped at one short sentence and the demo is about
interrupting rather than waiting for the agent to finish.

> **⚠ Outstanding human verification (R9.5).** These numbers prove `mistv3` *responds and produces
> audio*. They do **not** establish that it is a supported or GA model in Rime's catalog — it may
> be preview or unannounced. A human must confirm it in Rime's live catalog before it can carry
> the same VERIFIED status `mistv2` has. Until then `mistv2` stays the fallback and is one line
> away: `RIME_MODEL=mistv2`.
>
> Not assessed at all: **audio quality**. Duration was measured; naturalness, clarity and
> pronunciation were not, and cannot be from sample counts. That judgement needs a human listening.

---

### Part 1b — WebSocket `/ws3` streaming (the default judged transport)

Added 2026-09-06/07. The HTTP contract above still exists and is reachable with
`RIME_TRANSPORT=http`, but **the default path is now `/ws3`**, because a blocking MP3 request
cannot start speaking until the whole utterance is synthesised, and cannot stop mid-utterance when
a generation is fenced. Both transports are Rime, so R9.4 is unaffected — this is a transport
choice, not a fallback provider.

    wss://users-ws.rime.ai/ws3?speaker=&modelId=&audioFormat=pcm&samplingRate=&lang=
    header:    Authorization: Bearer <RIME_API_KEY>      (secret; .env only, never committed)
    send:      {"text": "..."}
    interrupt: {"operation": "clear"}
    recv:      {"type": "chunk", "data": <base64>} | {"type": "timestamps"}
             | {"type": "done"} | {"type": "error"}

`audioFormat=pcm` at the AudioGate's own sample rate means no MP3 decode and no resample — bytes go
straight to the speaker.

**Executed against the live API.** `scripts/bench_turn.py` drove three end-to-end turns through
this transport; all three spoke, and the traces carry `provider=rime`, `transport=ws3`,
`model=mistv2`, `llm_transport_reused=true`.

Two behaviours are measured, not assumed:

- **`{"operation": "eos"}` is deliberately never sent.** Measured 2026-09-06: sending it makes the
  server close the connection (1005), which forced a fresh handshake every turn. Without it the
  utterance still synthesises and still ends with `done`.
- **Connection reuse matters.** Handshake 920 ms vs synthesis 520 ms on a cold socket; reusing the
  cached connection took a turn's TTS from 2849 ms to 389 ms.

> **Still unverified (human):** whether `mistv2` on `/ws3` accepts `speedAlpha`, `reduceLatency`,
> or a `time_scale_factor` equivalent. Field names were seen in two third-party repositories but
> **not** in Rime's documentation, so none is sent. R9.5: catalog values are verified by a human or
> not used.

> **Still outstanding (human):** organizer preflight, account tier / rate limits, and the Day-6
> re-check. Those are separate from the contract verification above and have NOT been done.

---

## Part 2 — Human-only checklist

These require a human. Do not mark them complete on the basis of anything an agent did.

### Rime catalog verification
- [x] Confirm the Rime API endpoint currently in use — `https://users.rime.ai/v1/rime-tts`
- [x] Confirm the model ID exists in the live catalog — `mistv2`
- [x] Confirm the model ID exists in the live catalog — **`mistv3`**, 2026-09-08
- [x] Confirm the voice ID exists and is available to this account — `astra`
- [x] Confirm the language code is supported for that model and voice — `eng`
- [ ] Confirm the account tier and any rate limits that affect a live demo
- [ ] Record the date of verification and re-check before the demo

**How `mistv3` was verified (2026-09-08).** Queried Rime's live voice catalog at
`https://users.rime.ai/data/voices/voice_details.json` — 863 entries. Model IDs present and their
entry counts:

| `modelId` | entries |
|---|---|
| `arcana` | 269 |
| `coda` | 253 |
| `mistv2` | 141 |
| `mist` | 117 |
| **`mistv3`** | **83** |

`astra` appears under `mistv2`, `arcana` **and** `mistv3`. So `mistv3` + `astra` + `eng` is a
catalogued, supported combination — not an undocumented endpoint that merely happened to respond.
This closes the R9.5 concern raised when the model was changed.

**Which of the two sounds better has NOT been judged, and is not claimed anywhere in this
repository.** A listening comparison is a human task and is listed below.

### Listening comparison — `mistv2` vs `mistv3`
- [ ] Play both renders of the identical sentence and choose one

Both files are produced by `scripts/render_rime_compare.py` into the repository root
(`rime_mistv2.wav`, `rime_mistv3.wav`; `*.wav` is gitignored, so they are never committed). The
sentence exercises exactly what the demo needs — identity, a spelled-out price, and a negative:

> "You've reached AETHER, the hotel manager. The chicken kebab is three hundred and eighty rupees,
> and the seafood platter is not available today."

Measured, same speaker, same transport (`/ws3`, PCM @ 48 kHz). **Two runs, both recorded**, because
the second disagreed with the first by ~1 s on first-audio and quoting only one would have made a
network-variable number look settled:

| Run | Model | Audio length | First audio | Samples |
|---|---|---|---|---|
| 2026-09-08 a | `mistv2` | 6.92 s | 2500 ms | 332138 |
| 2026-09-08 a | `mistv3` | 8.05 s | 1248 ms | 386400 |
| 2026-09-08 b | `mistv2` | 7.08 s | 2592 ms | 339940 |
| 2026-09-08 b | `mistv3` | 8.19 s | 2247 ms | 393120 |

Two claims survive both runs and are the only ones made here: **`mistv3` reached first audio sooner
in both**, and **`mistv3` speaks about 15-16% longer in both**. First-audio absolute values vary
with connection warmth and should not be quoted as a fixed figure from this test — the streaming
first-audio measurement in Part 1b is the one taken under controlled conditions.

Naturalness remains the human judgement above.

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

### Automated coverage (2026-09-08)

Six of the eight scenarios now run as real tests in `tests/test_acceptance.py`, against a real
`Day1Spike` -- real classifier, real `GenerationRegistry`, real `AudioGate`, real `ToolRunner`, real
fencing -- with only the four IO edges faked (microphone, STT, LLM, Rime).

| Scenario | Automated | Note |
|---|---|---|
| A REFINEMENT | ✅ | `ResultSalvaged` asserted **absent**, not present — see below |
| B REPLACEMENT | ✅ | |
| C STATUS_QUERY | ✅ | narrowed: the status is **not spoken**, only protected |
| D CANCEL | ✅ | |
| E BACKCHANNEL | ✅ | narrowed: no duck/resume in hands-free, because hands-free never ducks |
| F NEW_TASK | ✅ | |
| G unsafe control | ⛔ skipped | `AETHER_UNSAFE_MODE` is inert; no leak path exists to disable |
| G safe mode | ✅ | runs standalone, without the unsafe control condition |
| H zero-salvage | ⛔ skipped | salvage is not implemented and is deliberately not faked |

**A green test here is not a demo run.** The `<from_run>` fields below stay blank until an actual
execution fills them in, and **none of them is evidence about a telephone** — see Part 6.

Three assertions in the original specifications were changed rather than quietly dropped, and each
change is recorded in the test's own docstring:

- **C** originally said "status spoken from live task state". It is not spoken. Answering aloud over
  an answer already in flight would need a second audio path able to bypass the Audio Gate's single
  active generation, and putting a hole in the mechanism that enforces the golden invariant to say
  "just a moment" is not a trade worth making.
- **E** originally said `AudioDucked` then `AudioResumed`. Hands-free — the default, and what the
  phone path runs — never ducks, because a ducked-but-never-fenced utterance would be silently
  discarded. The audible result is stronger: the answer does not dip at all.
- **A / H** originally expected `ResultSalvaged.records_reused` (may be 0). AETHER holds no
  partial-result store, so it would be 0 on every run for ever. The event is asserted **absent**
  and `TaskReplaced.records_salvaged` carries the honest zero (RULES.md R8).

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

### Prewarm — call setup cost removed before the caller waits

Measured with `python -m aether.prewarm` on 2026-09-08, Windows 11 / Python 3.13.2, warm disk:

| Step | Cold | Warm (what a later call pays) |
|---|---|---|
| `import sounddevice` | 364.7 ms | 0.0 ms |
| `import faster_whisper` | 274.2 ms | 0.0 ms |
| import the configured LLM SDK | 882.6 ms | 0.0 ms |
| Whisper weights load (constructed and discarded) | 2288.1 ms | 671.8 ms |
| **total** | **3809.6 ms** | **671.8 ms** |

The worker runs this **before** it registers, so the process is warm before any call can arrive.
`livekit-agents` uses `JobExecutorType.THREAD` by default, so a job runs in that same process and
inherits the imports and the page-cached weights.

Related, and **not** applied as a default: with `HF_HUB_OFFLINE=1` a warm Whisper construction
measured 390.9 ms instead of 671.8 ms — faster-whisper otherwise makes a HuggingFace revision check,
which is a network round-trip on the call's setup path. It is documented in `.env.example` rather
than hardcoded, because it breaks a fresh clone that has not downloaded the model yet.

Full pipeline construction, for context (`Day1Spike`, warm, no PortAudio streams opened):
`AudioGate` 1492 ms · `WhisperSTT` 2601 ms · `build_llm` 1380 ms · `MicVAD` 442 ms ·
`build_tts` 4 ms · **total 5920 ms**. The first real call measured 9051 ms cold.

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

---

## Part 6 — Real phone call — **NOT YET VERIFIED**

**No successful phone call has been completed.** Nothing anywhere in this repository is evidence
about telephone audio, and no test asserts any. Every row below is a placeholder and must stay one
until an actual call fills it in.

The first attempt reached the worker and AETHER never replied; four defects were found and fixed
(entrypoint deadlock on an Event only teardown could set; 9051 ms of blocking pipeline construction
on the event loop; `track_subscribed` registered after `connect()` so the event landed in the gap;
and no greeting existed). Those fixes are tested. **They have not been exercised by a call.**

### Call path

| Item | Status |
|---|---|
| Worker registers as `aether-hotel` | `<from_run>` |
| Inbound call reaches the entrypoint | `<from_run>` |
| Caller's audio track subscribed | `<from_run>` |
| Greeting heard on the handset | `<from_run>` |
| Caller transcribed correctly (word accuracy) | `<from_run>` |
| Rime audio heard on the handset | `<from_run>` |
| Call ends cleanly, trace closed | `<from_run>` |
| `diagnose()` verdict for the call | `<from_run>` |

### Telephony audio — every number here is laptop-derived until this table is filled

| Item | Laptop value | Real-call value |
|---|---|---|
| `AETHER_SPEECH_FLOOR` | 35 (calibrated on a laptop mic) | `<from_run>` |
| Inbound `peak_rms` during speech | — | `<from_run>` |
| Inbound `floor_rms` (ambient) | — | `<from_run>` |
| `min_speech_ms` | 250 | `<from_run>` |
| Endpointing (trailing silence) | 500 ms | `<from_run>` |
| STT word accuracy | — | `<from_run>` |

**None of these transfers.** Telephony audio is narrowband, codec-compressed, carrier-processed and
network-jittered; a threshold derived from a laptop microphone has no reason to hold. Recalibration
is expected and is the single most likely day-of failure.

### Interruption on a call

| Item | Status |
|---|---|
| Natural barge-in stops speech mid-word | `<from_run>` |
| Barge-in latency (speech onset → audio stopped) | `<from_run>` |
| `ResultLeaked` count for the call | `<from_run>` |
| INTERRUPT button fences from the console during a call | `<from_run>` |
| STOP LISTENING leaves the participant joined in LiveKit | `<from_run>` |
| Turn latency, menu path | `<from_run>` |
| Turn latency, LLM path | `<from_run>` |

### AEC

Not implemented, and deliberately not implemented speculatively. The expectation is that the
caller's handset and the carrier already do echo cancellation, and that AETHER's audio reaches them
over the network rather than through a speaker beside their microphone — so the local speaker-bleed
problem should not exist on a call. **That is a hypothesis.** Implement only if AETHER is observed
self-triggering on a real call.

| Item | Status |
|---|---|
| AETHER self-triggers on its own outbound audio | `<from_run>` |
