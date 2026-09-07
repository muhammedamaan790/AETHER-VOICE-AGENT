# AETHER — Project Memory

Authoritative state of the project. Read this first after any context loss. If this file disagrees
with anyone's recollection, this file wins until it is updated with evidence.

**Last updated:** Day 1 (2026-09-06) — voice spike built; audio path measured; **Rime verified,
connected and speaking**.

---

## 1. Locked product decisions

Do not reopen these without evidence. Reopening one is a recorded decision, not a preference.

1. **Framing.** AETHER solves conversation continuity during realtime voice interaction: users can
   interrupt, refine, replace, question, cancel, or start a new task while the agent is working,
   without allowing stale state to become spoken output. It is **not** pitched as "a general-purpose
   voice AI".
2. **Golden invariant.** A stale result must never become spoken output. This outranks classifier
   accuracy.
3. **Taxonomy is frozen at six classes.** `BACKCHANNEL`, `REFINEMENT`, `REPLACEMENT`,
   `STATUS_QUERY`, `CANCEL`, `NEW_TASK`. No seventh class in the core.
4. **Generation status is `active` or `fenced`.** No `suspended` unless suspend/resume is actually
   implemented and tested.
5. **Fencing is not cancellation.** Separate concepts, separate events.
6. **Duck is not stop.** Duck is immediate on speech onset; full stop only on confirmed meaningful
   interruption.
7. **Rime is the primary and sole TTS in the judged path**, and the active provider is observable.
8. **General Q&A stays supported** as a capability and a test surface, and must not turn the
   architecture into a generic assistant platform.
9. **Warehouse is the demo fixture**, deliberately tiny and deterministic. Not a product, not a WMS.
10. **One canonical event vocabulary**, shared by runtime, trace, tests, and evaluator.
11. **Tier-1 `NEW_TASK` fences the active task mechanically.** No suspend, and no claim of suspend.
12. **Never invent numbers.** Unmeasured values are `<from_run>` placeholders.
13. **The live demo is live.** Replayed traces are supporting evidence, never a substitute.
14. **Realtime audio concurrency is threaded/callback-friendly.** The realtime audio and control
    path is threaded and callback-friendly. Asyncio is **not** the primary mechanism inside the
    realtime audio callback. Asyncio may be used outside the callback where appropriate.
    *Decided Day 1 from the actual PortAudio callback implementation and realtime correctness
    requirements:* the audio callback runs on PortAudio's own realtime thread and must read and act
    on duck/stop state there, so that path cannot depend on an asyncio event loop being alive or
    scheduled. Duck/stop are plain flags settable from the input callback; a watcher thread does
    the logging so nothing allocates on the realtime thread. Recorded in ARCHITECTURE.md §10. This
    supersedes the original scaffolding assumption of "asyncio-first" (§11 assumption 1).
15. **`ResponseSpoken` is emitted only after the Output Gate commits audio for actual playback.**
    It must **not** be emitted merely because TTS synthesis completed. Synthesised audio can exist
    without ever being played — a stale generation's audio may be synthesised and then discarded —
    and emitting at synthesis time would make the trace claim the user heard audio that was in fact
    never played. That would corrupt the very evidence the golden invariant is proven with. The
    Output Gate is the authoritative emission point. *Decided Day 1, after finding `RimeTTS`
    emitting the event at synthesis time.* Day 1 emits it at audio-commit in the caller because no
    Output Gate exists yet; Day 4 moves that emission behind the generation validity check. The
    event vocabulary is unchanged.

---

## 2. Current architecture (summary)

Always-open mic → VAD → Audio Gate (duck immediately) → STT → Classifier → **Supervisor** →
Task Runner (warehouse tools or LLM knowledge path) → **Output Gate** (generation validity check) →
Rime TTS. Every stage emits canonical events to an append-only trace, which the evaluator and
observability panel read.

- The **Supervisor** is the only component that changes conversational state.
- The **Output Gate** is the only path to speech and the single place the golden invariant is
  enforced — one choke point, one thing to audit, one thing unsafe mode disables.

Full detail: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 3. Event model

Canonical events (source of truth: [aether/events.py](aether/events.py), documented in
[ARCHITECTURE.md](ARCHITECTURE.md) §4):

`SpeechOnset`, `SpeechEnded`, `TranscriptFinal`, `BackchannelDetected`, `InterruptionClassified`,
`TaskStarted`, `TaskReplaced`, `FenceRequested`, `GenerationChanged`, `ResultReceived`,
`ResultDiscarded`, `ResultSalvaged`, `ResultLeaked`, `AudioDucked`, `AudioResumed`, `AudioStopped`,
`CancellationResolved`, `ResponseSpoken`.

Consolidations already decided: `TaskReplaced` is the single task-transition event (refinement /
replacement / new task differ by `reason`); `AudioDucked` and `AudioResumed` were added so duck and
full stop are distinguishable and separately measurable; no separate `ToolCallStarted`.

---

## 4. Implementation status

Day 1 built the voice spike. The continuity engine is still untouched, by design.

| Component | Status |
|---|---|
| Documentation set | Written |
| `aether/events.py` — canonical vocabulary | Implemented; used by every module below |
| `aether/config.py` — config | Implemented; Rime values verified and set in `.env` |
| `aether/trace.py` — append-only JSONL trace | **Implemented, tested** |
| `aether/audio/vad.py` — always-open mic + WebRTC VAD | **Implemented, tested** |
| `aether/audio/player.py` — AudioGate duck/stop/resume | **Implemented, tested**; now also **generation-aware** (see below) |
| `aether/stt.py` — faster-whisper (local, `base.en`) | **Implemented, tested on a speech fixture** |
| `aether/llm.py` — reasoning path | Interface implemented; **only the stub has run** (no credential) |
| `aether/conversation.py` — session history (Level 1) | **Implemented, tested.** In-memory, session-scoped, committed only at the completed/spoken boundary |
| `aether/audio/rime.py` — Rime TTS client | **Implemented and executed against the live API** — real MP3 audio returned, decoded and played |
| `aether/supervisor/generations.py` — generation IDs | ID allocation only, tested. **No fencing logic** |
| `aether/spike.py` — Day-1 loop | Implemented; **not run end-to-end with a live mic** |
| Classifier | Not started (Day 3) |
| Supervisor transitions | Not started (Day 3) |
| Fencing / Output Gate | **Audio-side fencing landed early** (pulled forward from Day 4 to fix a real defect): the AudioGate tags every queued chunk with its generation, refuses `enqueue` for a non-active generation, flushes on fence, and drops in-flight chunks in the callback — each recorded as `ResultDiscarded`. The *result*-side Output Gate (tool results, salvage, unsafe mode) is still Day 4 |
| Warehouse dataset + tools | Not started (Day 2) |
| Evaluator | Not started (Day 4) |
| Observability panel | Not started (Day 6) |
| Acceptance tests A–H | Pre-registered as specs and skipped stubs; none executed |

### Conversation history: Level 1 (implemented) vs Level 2 (not implemented)

Contextual follow-ups ("just tell me the first one") work because completed turns are kept in a
session-scoped, in-memory history and passed to the next LLM call as clean role/content pairs.

**Level 1 — what exists.** Only a turn that reached the completed/spoken boundary is remembered.
That boundary is the successful `AudioGate.enqueue`, immediately before `ResponseSpoken` (locked
decision 15). A fenced generation contributes **nothing** — not its assistant answer, and not its
user text: the user moved on before hearing a reply, so that exchange is not part of the
conversation. History needs no fencing logic of its own, because the only line that writes to it
sits after every fenced path has already returned. Existing fencing stays authoritative.

History is **append-only**, and fencing never modifies or deletes it — `fence_generation()` acts on
audio and results only. Fencing invalidates *output*; it does not rewrite the past (RULES.md R5).
A turn already spoken stays remembered even if the next generation is fenced. There is deliberately
no `clear()`; the only removal is the session-length cap below.

Known limitation, accepted: a flat cap of 12 turns, dropped oldest-first. No token budgeting.

**Level 2 — deliberately NOT implemented.** If a turn is fenced *mid-speech*, the user really did
hear the first part of it, and Level 1 forgets that entirely — so a follow-up referring to a
partially-heard answer has no context. Level 2 would record the partial spoken prefix (what was
actually heard, not what was generated) and make it available as context. That needs the audio
path to report how much was played before the stop, which does not exist today. It is an upgrade
path, not a claim.

---

## 5. What has been measured

Real values from real runs. Conditions matter and are recorded with each.

**Run `traces/run-20260906T020258Z.jsonl`** — `scripts/measure_audio_kill.py --mode synthetic
--repeats 8`, n=8, Windows 11 / Python 3.13.2, WASAPI output @48 kHz, VAD aggressiveness 2:

| Metric | min | median | max |
|---|---|---|---|
| `duck_latency_ms` (SpeechOnset → AudioDucked applied) | 1.27 | 21.95 | 30.39 |
| `audio_kill_latency_ms` (SpeechOnset → AudioStopped applied) | 301.65 | 321.24 | 333.18 |

Conditions and exclusions:
- **Synthetic mode**: synthetic voiced frames were injected into `MicVAD.process_frame` at
  real-time pacing while a test tone played through the real output device. Same VAD and same
  AudioGate as the live path. **This is not a live-microphone, human-speech measurement.**
- Excludes microphone capture latency (upstream of the first frame, so upstream of `SpeechOnset.t`).
- Excludes audio already buffered in the OS/driver past the callback: measured
  `stream_output_latency_ms = 22.0` on WASAPI (vs 100.0 on MME — hence the WASAPI default).
- `audio_kill_latency_ms` is dominated by the deliberate 300 ms `MEANINGFUL_SPEECH_MS` confirmation
  policy in `aether/spike.py`. It is a policy number, not a system limit. The system's *reaction*
  time is the duck figure. Day 3's classifier replaces the duration proxy.

**Run `traces/run-20260907T070314Z.jsonl`** — `scripts/bench_turn.py --repeats 3`, the first
measurement of the **assembled pipeline** rather than of individual components. Same
`Day1Spike.handle_utterance` the live loop runs, real Gemini, real Rime `/ws3`, real AudioGate.
n=3, medians:

| Stage | Median |
|---|---|
| `stt_ms` (base.en, beam 5) | 888 |
| `llm_ttft_ms` | 1840 |
| `tts_ms` / Rime first audio | 552 |
| `turn_latency_ms` | **3488** |

Conditions and exclusions:
- **Headless.** Audio is fed from a WAV, so there is no microphone and no VAD. `SpeechEnded` is
  stamped at hand-off, which makes `stt_ms` real transcription time but means `turn_latency_ms`
  **excludes VAD endpointing** (~620 ms observed, below). A live turn is that much slower.
- 3/3 turns spoke. `llm_transport_reused=True`, `llm_raw_chunks=2`.
- This is the first run in the project where `llm_ttft_ms` and `llm_first_sentence_ms` were
  populated at all — the sentence-streaming telemetry existed but no harness had ever driven the
  path, so all 45 earlier traces have those fields empty.

**Model selection, measured 2026-09-07** (3 calls each, same prompt, through `respond_stream`):

| Model | TTFT | Total | Errors |
|---|---|---|---|
| `gemini-3.8-flash` (previous default) | n/a | 13809 ms | **2/3 ServerError** |
| `gemini-3.5-flash-lite` | 978 ms | 978 ms | 0/3 |
| **`gemini-flash-lite-latest`** (chosen) | **800 ms** | 800 ms | 0/3 |
| `gemini-2.5-flash-lite` | — | — | 3/3 ClientError |
| `gemini-2.5-flash` | — | — | 3/3 ClientError |

A full-pipeline turn on `gemini-3.8-flash` recorded `llm_ttft_ms = 23112` and the next turn died
with `ServerError`. It was not slow, it was failing the majority of calls.

**STT trade-off, measured 2026-09-07** on `tests/fixtures_stt_probe.wav` (3.13 s, n=3 each). Every
configuration returned the **identical, correct** transcript:

| Model | beam 1 | beam 5 |
|---|---|---|
| `tiny.en` | 415 ms | 571 ms |
| `base.en` (current) | 900 ms | **1058 ms** |
| `small.en` | 2929 ms | 3104 ms |

**Not acted on, deliberately.** The fixture is clean synthetic speech — the easiest possible case —
so it cannot justify lowering accuracy settings that exist for noisy live audio. Up to ~640 ms is
available here, but only a noisy real-mic evaluation can say whether it is safe to take.

**Other measured values** (single observations, not benchmarks):
- STT: `tiny.en` transcribed a 3.13 s SAPI-generated fixture correctly in **423–430 ms** on CPU.
  (Measured on `tiny.en` with `beam_size=1`. The default is now `base.en` with `beam_size=5`,
  which is slower — this number does NOT describe the current configuration and has not been
  re-measured.)
- Device latency: WASAPI 22.0 ms vs MME 100.0 ms vs DirectSound 120.0 ms (48 kHz, blocksize 480).
- WebRTC VAD hangover at aggressiveness 2: ~6 frames (~120 ms) of trailing silence still reported
  as speech. Utterance-end detection is therefore ~620 ms, not the nominal 500 ms.

---

## 6. What has NOT been measured

Still `<from_run>` and must not be quoted:

- `audio_kill_latency_ms` **with a real microphone and real human speech** (only synthetic-mode
  exists; `scripts/measure_audio_kill.py --mode mic` is written and waiting for a human)
- `classification_latency_ms`
- `tool_latency_ms`
- `response_latency_ms`
- `stale_leak_rate` (safe and unsafe control)
- `records_reused`
- `classification_accuracy`

`stale_leak_rate = 0` is currently a **target**, not a result.

---

## 7. Known bugs and limitations

- **No acoustic echo cancellation.** The mic stays open while the agent speaks, so on open
  speakers the agent's own output can retrigger the VAD. Headphones are required for the Day-1
  spike and the demo. Deliberately not solved on Day 1.
- **Day-1 barge-in confirmation is duration-based, not semantic.** 300 ms of continued voiced
  audio promotes a duck to a full stop. It is a placeholder for the Day-3 classifier and is
  explicitly *not* backchannel detection — `BackchannelDetected` is never emitted by Day-1 code.
- **Residual buffered audio.** `AudioStopped` marks when the callback stopped emitting samples;
  up to `stream_output_latency_ms` (22 ms on WASAPI) of already-queued audio still reaches the
  speaker. Disclosed on every `AudioStopped` event rather than hidden.
- **`aether/spike.py` has never been run end-to-end with a live microphone.** Its components are
  tested individually and the offline path is verified via `scripts/verify_pipeline.py` (which now
  reaches real Rime audio), but the assembled live loop remains untested.
- **Rime contract is VERIFIED and implemented** (endpoint, `Accept: audio/mp3`, four-field body,
  raw MP3 response). MP3 is decoded with PyAV, which bundles FFmpeg — no external binary needed.
- **`.env.example` used to carry inline `# TODO` comments after `=`.** python-dotenv made those
  comments the literal values, so a copied `.env` silently held `RIME_MODEL="# TODO: ..."`. Fixed:
  comments now go on their own line. Watch for this pattern in any future template.
- Trace `seq` reflects emission order while `t` may be a callback-captured timestamp, so `t` can be
  very slightly out of order relative to `seq`. Metrics use `t`. Intentional.
- **Fixed Day 1:** `AudioGate.enqueue` accepted audio for *any* generation — `_gen` was a label,
  never a check — so audio synthesised for a generation that was fenced mid-synthesis would still
  play. A bare `request_stop` flushed the queue but did nothing to stop the *next* stale chunk
  being accepted. Fix: generation-tagged chunks, `set_active_generation`, `fence_generation`
  (revokes the generation synchronously *before* requesting the flush, closing the
  enqueue-after-fence race), plus a callback-level drop as defence in depth. `enqueue` now returns
  `False` on refusal, and callers must not emit `ResponseSpoken` when it does.
- **Fixed Day 1:** a stop no longer leaves a duck/resume pending, which used to emit `AudioDucked`
  *after* `AudioStopped` and make traces read as though audio was ducked after being cut.
- **Fixed 2026-09-07 — every non-Gemini provider failed 100% of turns.** `RetryingLLM` defined
  `respond_stream` unconditionally, so the duck-typed `supports_streaming()` answered "yes" for
  adapters that cannot stream. The pipeline then committed to the streaming path and the wrapper
  raised `AttributeError` on the first token: every groq / anthropic / openai turn died as
  `llm_stream_error`. The wrapper now binds `respond_stream` per instance, only when the adapter
  underneath really streams, so the capability check is honest by construction. Found by running
  the assembled pipeline for the first time — no unit test caught it, because each half was
  individually correct.
- **Fixed 2026-09-07 — `ResponseSpoken.first_audio_ms` meant two different things.** On the
  blocking path it carried Rime's send → first-chunk *duration*; on the streaming path it carried
  `timing.first_audio`, an *absolute* monotonic mark. Traces therefore showed "first audio" rising
  monotonically across a session (7586 → 10869 → 15761 ms). Both paths now report the duration.
  Any `first_audio_ms` read from a streaming-path trace written before this date is wrong.

---

## 8. Deferred / stretch / cut

| Feature | Status | Condition |
|---|---|---|
| Suspend/resume | **Deferred.** Not implemented, not claimed. | Day-5 stretch only if the core is solid. Single slot, no stack, `RESUME` class, suspended generations cannot speak, late results held/tagged. If not solid, cut — and no `suspended` status enters the model. |
| Spoken-prefix recovery | **Deferred.** Not implemented, not claimed. | Day-5 optional. Cut if not reliable by end of Day 5. Never part of the core claim. |
| Level-2 conversation history (partial spoken prefix) | **Deferred.** Not implemented, not claimed. | Needs the audio path to report how much of a turn was actually played before a mid-speech fence. Level 1 is in place; see section 4. |
| Multiple domains | **Out of scope.** | — |
| Large warehouse application | **Out of scope.** | — |
| Elaborate UI | **Out of scope.** Observability panel only. | — |
| RAG infrastructure | **Out of scope.** | — |
| Custom model training | **Out of scope.** | — |
| Multi-agent architecture | **Out of scope.** | — |

---

## 9. Human-only tasks

Not doable by an agent. Tracked in [RIME_EVIDENCE.md](RIME_EVIDENCE.md) Part 2.

1. **Rime catalog verification** — endpoint, model, voice, language, account tier, rate limits.
   **Not done.** Every Rime config value in this repo is a placeholder.
2. **Organizer / event preflight** — eligibility, registration, submission format, deadline, demo
   length, expected evidence. **Not done.**
3. **Differentiation review** — review the current Rime Voice AI project catalog and confirm AETHER
   is not a close reproduction of an existing project. **Not done.**
4. **Fallback disclosure decision** — decide whether any TTS fallback exists and disclose it
   explicitly if so.
5. **Credentials** — obtain and place API keys in a local `.env`. Never committed.
   **Rime: DONE** — `.env` exists, the key is set, and the client has executed against the live API.
   **LLM: still blocking** — no LLM credential, so the real reasoning path has never executed.
6. **Live demo recording** — real mic, real speech, real interruption.
7. **Secrets sweep before recording** — no credential visible in any frame.
8. **Run the live-microphone latency measurement** — `python scripts/measure_audio_kill.py
   --mode mic --repeats 5`, wearing headphones. Needs a person to speak; the synthetic-mode number
   does not substitute for it.
9. **Choose an LLM provider** and set `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY`. Until then
   `aether/llm.py` runs a labelled stub that cannot answer general questions.

---

## 10. Open decisions (need a choice, not yet locked)

| Decision | Status |
|---|---|
| STT provider/library | **Decided Day 1** — faster-whisper, local CPU. Upgraded `tiny.en` -> `base.en` (beam_size 1 -> 5) for transcription accuracy; costs latency, not yet re-measured |
| VAD implementation | **Decided Day 1** — WebRTC VAD (`webrtcvad-wheels`), 20 ms frames, aggressiveness 2 |
| Audio I/O library | **Decided Day 1** — `sounddevice`/PortAudio, WASAPI preferred on Windows |
| Trace format on disk | **Decided Day 1** — JSONL, one event per line |
| LLM provider for the reasoning/knowledge path | **Decided 2026-09-07 — `gemini`, model `gemini-flash-lite-latest`**, chosen by measurement (§5). Gemini is currently the *only* working provider: the Groq key does not authenticate and its previous default model `llama-3.3-70b-versatile` is decommissioned (404) |
| Observability panel form (terminal vs minimal web) | TODO — Day 6; keep minimal per scope rule |
| Meaningful-interruption threshold (300 ms) | Placeholder — replaced by the Day-3 classifier |

---

## 11. Assumptions made during scaffolding

Recorded so they can be challenged rather than inherited silently.

1. **Python 3.11+ / asyncio** was chosen as the stack. Not specified in the brief; picked for audio
   library availability, async cancellation semantics, and pytest.
   **SUPERSEDED on Day 1 — no longer an open assumption.** The realtime audio/control path is
   threaded and callback-friendly; asyncio is not the primary mechanism inside the realtime audio
   callback, and remains available outside it. This is now locked decision 14 in §1 and is
   documented in ARCHITECTURE.md §10. Python 3.11+ stands.
2. **`AudioDucked` / `AudioResumed` were added** to the event list. The brief required duck and
   stop to be distinguishable but listed only `AudioStopped`; the duck-latency metric and the
   backchannel resume path both need their own events.
3. **JSONL** assumed for the trace format.
4. **Controlled tests use a deterministic stub for the knowledge path**, because a live model is not
   reproducible and fixtures must be. The live demo uses the real model.
5. **An acceptance scenario H was added** (general Q&A zero-salvage refinement) because the brief
   describes that case explicitly under SALVAGE and it needs a home in the test matrix.
6. This repository's `MEMORY.md` is a project document. It is unrelated to any agent memory
   directory of the same name.

Added on Day 1:

7. **`MEANINGFUL_SPEECH_MS = 300`** is an invented placeholder threshold, not a tuned or measured
   value. It exists only so a duck can become a stop before the classifier exists.
8. **WASAPI is preferred for output on Windows** because it measured 22 ms device latency against
   MME's 100 ms. WASAPI shared mode forced the gate's sample rate to 48 kHz; Rime audio is
   resampled to match.
9. **Windows SAPI was used to generate one STT test fixture** (`tests/fixtures_stt_probe.wav`).
   It is a throwaway fixture for verifying STT without a human, not a TTS provider, and it never
   touches the spoken response path. There is still no fallback TTS (R9.4).
