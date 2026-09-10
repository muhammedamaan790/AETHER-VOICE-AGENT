# AETHER — Project Memory

Authoritative state of the project. Read this first after any context loss. If this file disagrees
with anyone's recollection, this file wins until it is updated with evidence.

**Last updated:** 2026-09-10 — hotel SQLite database as the single source of truth, **23 tools
(20 read, 3 book)**, three languages (English, Hindi, Spanish) that AETHER both *understands* and
*answers* in from that one database, caller-chosen language before the hotel greeting, 27
`hotel_policies` topics, a suggestion-and-repeat path for what the recogniser mangles, and real
room and table bookings behind an authorizer that keeps every hotel FACT unwritable. The realtime voice path and the continuity engine are both built and tested; salvage and the
unsafe-mode control path remain deliberately unbuilt.

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
7b. **One hotel facts database, three language renderers.** `data/aether_hotel.db` is the only
    place a price, room or policy lives; English, Hindi and Spanish render the same rows. The
    caller chooses the language before the hotel greeting.
7c. **Database wins when it can answer; otherwise the model answers naturally.** A question with
    a deterministic route never reaches the LLM. One absent from the database does, and the
    model is expected to answer plausibly rather than refuse — reversing an earlier rule
    (RULES.md R8b). Allergens and dietary status are the one thing never guessed.
8. **General Q&A stays supported** as a capability and a test surface, and must not turn the
   architecture into a generic assistant platform.
9. **The hotel is the product**, backed by `data/aether_hotel.db` (read-only). The warehouse
   fixture survives only as the *mutable* store that keeps fenced-mutation under test -- the
   read-only hotel structurally cannot exercise "the mutation never landed".
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

The realtime voice path is built and tested, and the continuity engine on top of it now exists:
the classifier is implemented and wired, the transitions it implies are emitted, and the hotel
product runs on top of both. What remains genuinely unbuilt is salvage, the unsafe-mode control
path, and the evaluator.

**Updated 2026-09-10: 1446 tests pass, 2 skipped.** Both skips are features that do not exist, and
each names itself.

| Component | Status |
|---|---|
| Documentation set | Written |
| `aether/events.py` — canonical vocabulary | Implemented; used by every module below |
| `aether/config.py` — config | Implemented; Rime values verified and set in `.env` |
| `aether/trace.py` — append-only JSONL trace | **Implemented, tested** |
| `aether/audio/vad.py` — always-open mic + WebRTC VAD | **Implemented, tested** |
| `aether/audio/player.py` — AudioGate duck/stop/resume | **Implemented, tested**; now also **generation-aware** (see below) |
| `aether/stt.py` — faster-whisper (local, `base.en`) | **Implemented, tested on a speech fixture** |
| `aether/llm.py` — reasoning path | **Implemented, tested, and executed against the live API.** Four providers (groq/anthropic/openai/gemini), `RetryingLLM`, and Gemini sentence streaming via `respond_stream`. Gemini is the only provider with a working credential — the Groq key does not authenticate. Default model `gemini-flash-lite-latest`, chosen by measurement (§5) |
| `aether/conversation.py` — session history (Level 1) | **Implemented, tested.** In-memory, session-scoped, committed only at the completed/spoken boundary |
| `aether/audio/rime.py` — Rime HTTP client | **Implemented and executed against the live API** — real MP3 audio returned, decoded and played. Now the fallback transport (`RIME_TRANSPORT=http`) |
| `aether/audio/rime_ws.py` — Rime `/ws3` streaming | **Implemented, tested, and the default judged transport.** PCM at the gate's own rate (no MP3 decode, no resample), persistent socket reused across turns, `{"operation":"clear"}` on fence. Deliberately never sends `eos` — measured, it closes the socket |
| `aether/sentences.py` — sentence accumulator | **Implemented, tested.** Streams whole sentences to TTS; handles decimals, abbreviations, ellipses |
| `aether/timing.py` — per-turn latency | **Implemented, tested.** Rides on the existing `ResponseSpoken` event; no new event types |
| `aether/interruption/` — BargeInCoordinator | **Implemented, tested.** Owns *when* a barge-in becomes a fence; arms on a turn in flight, not only on audible playback |
| `aether/tools/`, `aether/warehouse/` — tools + fixture | **Implemented, tested** (see row below) |
| `aether/web/` — the AETHER console | **Implemented, tested.** Folds the canonical event stream into a snapshot including the transcript, so a reconnecting browser is handed the conversation rather than an empty screen. The emitting thread only does `put_nowait`, and the bridge still cannot fence, enqueue or allocate — it holds exactly two injected callables. Siri-style animated orb with nine states, one listening toggle, a separate INTERRUPT, and an evidence strip. The page is a fixed viewport-height frame with the conversation as the only scroller, so a long call can never push the controls off screen; every ink clears 4.5:1 against its panel, asserted in tests |
| `aether/supervisor/generations.py` — generation IDs | **Implemented, tested.** Monotonic allocation plus `mark_fenced` / `is_active`, which is the authority every stage consults. The *supervisor transitions* built on top of it are still Day 3 |
| `aether/spike.py` — the voice loop | Implemented and **run end to end headless** (`scripts/bench_turn.py`: WAV → STT → Gemini → Rime WS3 → AudioGate, 3/3 turns spoke), and since run with a live microphone. Now also carries the classifier hook and the listening controls. **Never run over a real phone call** |
| `aether/classify/` — six-class classifier | **Implemented, tested, wired (2026-09-08).** Closed sets, whole-utterance matching, no model, no confidence score. Runs after STT and **before** `begin_turn`, because allocation is what fences. `BACKCHANNEL` and `STATUS_QUERY` withhold a turn entirely; `CANCEL` fences with no successor; the other three proceed and label `TaskReplaced`. Anything unrecognised falls to `REPLACEMENT`, which fences |
| Supervisor transitions | **Implemented for all six classes** in `Day1Spike.handle_utterance` / `_resolve_without_a_turn`, emitting `InterruptionClassified`, `BackchannelDetected`, `TaskReplaced` and `CancellationResolved`. There is still no separate supervisor *module*: the transitions live at the turn boundary, and `GenerationRegistry` remains the authority. **Salvage is not implemented and is not faked** |
| `aether/hotel/` — database, tools, router | **Implemented, tested, wired (2026-09-07/08; SQLite from 2026-09-08).** `data/aether_hotel.db` is the source of truth: 12 menu items across five categories, 50 rooms, 5 room types, 6 services, **27 policy topics**, hotel timings and 3 reservations. Facts opened `mode=ro`; bookings written through an authorizer that refuses everything else (`aether/hotel/bookings.py`). **23** tools through the existing `ToolRunner` -- 20 read, 3 write (`reserve_room`, `reserve_table`, `cancel_booking`); deterministic keyword/slot routing with spoken templates in **three** languages. A hotel fact the database holds never reaches the LLM -- the router intercepts first. A question it does NOT hold does reach the LLM, which may answer plausibly (RULES.md R8b). Replaced a hand-written 29-dish fixture — prices moved, and the spice column does not exist, so `spice_of`/`find_by_spice` were removed rather than faked |
| `aether/bridge/`, `aether/telephony/` — LiveKit transport | **Implemented, tested against synthetic audio only.** `InboundBridge`/`OutboundBridge` carry a transport into the unchanged pipeline; the worker uses `livekit.rtc` directly and never `AgentSession`. **Calls now connect, are answered, are understood and are transcribed** — telephony audio measured from 28 utterances across three calls, which is what moved the speech floor from 35 to 2500. STT word accuracy on narrowband audio is still not separately measured |
| `aether/prewarm.py` — process warm-up | **Implemented, measured.** 3810 ms cold, 672 ms warm; run before the worker registers |
| `aether/telephony/diagnostics.py` — failure-stage report | **Implemented, tested.** Names the first stage that produced nothing, from bridge counters and the trace |
| Fencing / Output Gate | **Audio-side fencing landed early** (pulled forward from Day 4 to fix a real defect): the AudioGate tags every queued chunk with its generation, refuses `enqueue` for a non-active generation, flushes on fence, and drops in-flight chunks in the callback — each recorded as `ResultDiscarded`. The *result*-side Output Gate (tool results, salvage, unsafe mode) is still Day 4 |
| Warehouse dataset + tools | **Implemented, tested** (2026-09-07). `aether/warehouse/` holds the deterministic fixture (7 products, 7 bins, 12 orders — 8 high priority, 3 of them in aisle 9) plus `WarehouseStore`, the only mutable state. `aether/tools/` holds 9 structured tools and `ToolRunner`, with an injectable delay and a fence check placed **after** the delay and **before** the tool body — so a fenced generation's `skip`/`cancel`/`select` never reaches the store at all, not merely its output. Emits `TaskStarted` → `ResultReceived` \| `ResultDiscarded`; no new event types. **Not yet wired into `spike.py`** — routing an utterance to a tool is classifier/supervisor work (Day 3) |
| Evaluator | Not started (Day 4) |
| Observability panel | Not started (Day 6) |
| Acceptance tests A–H | **Six of eight now execute** (2026-09-08): A, B, C, D, E, F and safe-mode G run against a real pipeline with fake IO. G's unsafe control and H's salvage case stay skipped because neither feature exists. Three original assertions were narrowed rather than dropped, each recorded in its test docstring and in RIME_EVIDENCE Part 3 |

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

**Push-to-talk interrupt latency, measured 2026-09-07** (n=20, real `AudioGate`, real
`BargeInCoordinator`, real output callback):

| Mark | Median | Max |
|---|---|---|
| press → generation fenced | 0.004 ms | 0.006 ms |
| press → gate stops emitting samples | 0.011 ms | 0.017 ms |

Against the voice path's measured **321 ms** (`onset → AudioStopped`), which is dominated by the
deliberate 300 ms `MEANINGFUL_SPEECH_MS` confirmation. A deliberate press needs no confirmation,
so that entire cost disappears.

Excluded, and material: the browser click → websocket → Python hop (local, unmeasured), and the
~22 ms of audio already buffered past the callback (`stream_output_latency_ms` on WASAPI). Real
press-to-silence is therefore roughly **25 ms**, not 0.01 ms — still about an order of magnitude
faster than the voice path, but the sub-millisecond figure is the software path only.

**Other measured values** (single observations, not benchmarks):
- STT: `tiny.en` transcribed a 3.13 s SAPI-generated fixture correctly in **423–430 ms** on CPU.
  (Measured on `tiny.en` with `beam_size=1`. The default is now `base.en` with `beam_size=5`,
  which is slower — this number does NOT describe the current configuration and has not been
  re-measured.)
- Device latency: WASAPI 22.0 ms vs MME 100.0 ms vs DirectSound 120.0 ms (48 kHz, blocksize 480).
- WebRTC VAD hangover at aggressiveness 2: ~6 frames (~120 ms) of trailing silence still reported
  as speech. Utterance-end detection is therefore ~620 ms, not the nominal 500 ms.
- **Playback latency** (`scripts/measure_playback_latency.py --repeats 8`, 2026-09-10, WASAPI
  device 8 @ 48 kHz, blocksize 480): enqueue -> device callback, **cold 221.4 ms**, **warm n=8:
  min 5.9 / median 9.2 / max 12.6 ms**; device buffer 22.0 ms reported separately and never added.
  Cold includes the output stream spinning up, which a caller pays once per session -- averaging it
  with the warm runs would present a setup cost as a per-turn cost, so the two are never combined.
  This is the piece every other latency figure omits: `turn_latency_ms` ends at `first_audio` (the
  TTS client accepting Rime's first chunk, upstream of the output queue), while
  `TurnTiming.output_latency_ms` ends at `first_output`, stamped inside the PortAudio callback.
  AETHER still claims no ear-to-ear number: the device buffer and the phone network sit after the
  last point this process can observe.

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

### Fixed 2026-09-08, recorded so the reasoning is not lost

- **A backchannel killed the answer it was encouraging.** `handle_utterance` allocated a generation
  before transcribing, and allocation is what fences the previous generation. So "mm-hm" said over
  an answer destroyed it. Fixed by transcribing and classifying **before** `begin_turn`.
  `TranscriptFinal` now carries `gen: null`, because at the moment STT runs the generation this
  utterance belongs to does not exist yet — and may never exist.
- **A noise burst that transcribed to nothing killed the answer too**, by exactly the same
  mechanism: the empty-transcript check returned early, but the generation had already been
  allocated on the way in. Same fix.
- **`in_flight` is not `turn_in_flight`.** `rime.speak` returns once audio is *enqueued*, not once
  it has been heard, so `handle_utterance` finishes and clears the flag while several seconds of a
  menu answer are still playing. `Phase` reported LISTENING mid-sentence, which told the console
  the agent was idle and told the INTERRUPT button there was nothing to interrupt at the exact
  moment there was. Both now also ask the gate — `phase` additionally requires an **active**
  generation, so audio about to be flushed after a fence is not called "speaking".
- **`WebBridge.stop()` left its ports bound.** `shutdown()` stops the serve loop; `server_close()`
  releases the socket, and only the first was being called. The second call in a process — the next
  phone call starting its own console — would fail to bind with no obvious cause.

- **The classifier swallowed turns whose transcript was correct.** Reported as "STT got worse";
  it was not. `aether/audio/*`, `aether/stt.py` and `aether/config.py` are byte-identical to
  e4360ea, and the same fixture transcribes identically, so recognition never changed. What
  changed is that a new layer sat between the transcript and the answer: `in_flight` includes
  `gate.is_playing`, which stays true for the several seconds an answer takes to play, and during
  that window any transcript in the backchannel or cancel closed set was withheld or fenced. A
  garbled question that Whisper renders as "Okay." or "Stop." — which it does readily — therefore
  produced silence where the old build produced an answer.

  Fixed by gating every closed set on `SpeechEnded.voiced_ms`, which the VAD already measures
  (voiced frames only; preroll and trailing silence excluded). The bound is **measured, not
  guessed**: `tests/fixtures_stt_probe.wav` through the real detector is 2320 ms of voiced audio
  for 7 words = **331 ms per word**, and the bound is 1000 ms per word — three times slower than
  natural speech, so it admits drawled speech and rejects only what cannot be a faithful reading
  of the audio. No VAD, STT or capture parameter was touched.
- **An explicit price question containing a spice word answered about heat.** Ordering `spice_of`
  before `price_of` in the router meant "how much is the hot chicken kebab" returned a spice
  level. Price now wins; a spice question with no price words still reaches `spice_of`.
  *Superseded 2026-09-08:* the database has no spice column, so `spice_of` and `find_by_spice` were
  removed rather than faked. "Is it spicy?" now reaches `describe_item`, which reads the hotel's own
  description back. The ordering fix still stands and is still tested — price must beat description
  — because STT inserts "hot" and "medium" readily.

- **The model volunteered rooms, stays and invented facts.** Live web test: "Hello, how are you
  doing today?" came back as "...how can I help make your stay comfortable today?", and "what time
  do you close?" as "our main dining room closes at eleven in the evening, but room service is
  available twenty-four hours a day" -- two specific facts that exist nowhere in this system. The
  prompt now forbids introducing rooms, reservations, stays, check-in/out or restaurants unless
  the caller raises them, forbids asking which restaurant (there is one), models the greeting
  reply it wants, and widens "never invent" from dishes/prices/allergens to hours, rates and
  services. Verified live against Gemini, not only by prompt assertion.
- **The router had no rule for the broadest menu question.** "What type of dishes are available on
  the table?" reached the model, which asked which restaurant the caller meant -- a question with
  no answer. `menu_overview` now answers it from the fixture's shape. The rule is tried LAST and
  needs BOTH a food noun and a list cue, so "is the food good", "where is the food court" and
  "can I order a taxi" still fall through to the model.

- **A real call: greeting heard, AETHER deaf.** The caller's audio track is discovered on two
  paths -- the `track_subscribed` event, and the sweep of already-subscribed publications after the
  ~9 s pipeline build -- and in ordinary call timing BOTH fire for the same track. There was no
  deduplication, so two `rtc.AudioStream` readers pushed every frame into ONE `InboundBridge`
  sharing one `_carry` buffer.

  **Measured**, because the first simulation was misleading: neat duplication (block A, A, B, B)
  transcribes *correctly* -- Whisper is robust to a clean stutter. But two independent asyncio
  tasks drift, and with realistic interleaving the same fixture transcribed **0 of 6 runs**
  correctly, producing exactly the reported symptom: "Find the product I already ordered is 8
  orders in alumni" for "Find the priority orders in aisle 9". Do not conclude a double pump is
  harmless from the tidy case.

  Fixed by deduplicating in `CallBridge.start_inbound` by `track.sid`, falling back to object
  identity. Both discovery paths are kept -- losing the track entirely is the bug they were written
  to fix. **The fix has not been exercised by a call.**
- **One bad frame ended inbound audio for the whole call.** `pump_inbound` wrapped the entire
  `async for` in a single `try`, so a malformed frame left AETHER deaf for the remainder while the
  room, the outbound pump and the turn loop kept running -- the call looked alive. Frames are now
  counted and skipped individually; only a stream failure ends the pump, and `frames_failed`
  appears in the call diagnostics.
- **Diagnostics quoted a sample rate they never received.** `InboundBridge` reported the expected
  `source_rate` (48000), so audio arriving at another rate would have had its duration misstated by
  the ratio -- 8 kHz audio reading as one sixth of its true length, i.e. "almost nothing arrived".
  It now records and reports `observed_rate`.

- **Confirmed by the real call log** (`run-20260908T023320Z`, 2026-09-08): `[4/7] inbound pump
  starting` appears twice, once `(queued)` and once `(already-subscribed)`. The diagnostics report
  `audio_ms=45860` for a call that lasted ~23 s — exactly double, which is the defect stated
  arithmetically. `base.en` fell through all six temperature fallbacks with a compression ratio of
  9.82 (near-total repetition), the signature of interleaved audio.
- **The speech floor did NOT need recalibrating, and predicting otherwise was wrong.** Real
  telephony speech on that call peaked at 6372–10174 RMS against `AETHER_SPEECH_FLOOR=35` — a
  180x–290x margin, close to the laptop's own 8422–12441. Line noise sat at 12.9–15.8 and was
  correctly rejected as `below_noise_floor`. The plan called this the most likely day-of failure;
  the measurement says it is not a problem at all. Nothing was changed.
- **`diagnose()` returned `OK` for a call that failed.** It finds the first stage that produced
  *nothing*, and every stage produced something — the audio was corrupt, not absent. Fixed by
  adding a `transport:` line carrying `pumps` and `frames_failed`; `pumps=2` for one caller names
  this defect immediately. The lesson generalises: a stage-completion check cannot detect
  corruption, only absence.
- **The console crashed the call log by binding in a worker thread.** A port already in use raised
  a bare `OSError` from a dying daemon thread into the middle of a call's output, where it read as
  a fault in the call — and `start_console`'s `try` could not catch it, because it was raised on
  another thread. `WebBridge.start()` now binds both ports synchronously. The port was held by
  `python -m aether.web` running alongside the worker, which is not merely a port clash: it is a
  second complete pipeline, with its own Whisper model and microphone, competing for CPU with the
  live call. The warning now says so.
- **`stop()` could deadlock.** `shutdown()` blocks until the serve loop acknowledges it and hangs
  forever if that loop was never entered — reachable when a call ends immediately after starting.
  Each server now announces that `serve_forever` has begun, and `stop()` waits on that with a
  bounded timeout.

- **The dedup fix is CONFIRMED by a real call** (`run-20260908T041603Z`): the log reads
  `[4/7] track already has a pump (already-subscribed); not starting a second`, the diagnostics
  read `transport: pumps=1 frames_failed=0`, and `audio_ms=34620` for a 35-second call -- 1:1,
  where it was exactly 2:1 before. The captured audio transcribes cleanly.
- **The speech floor DID need recalibrating after all, and my earlier "it does not" was wrong.**
  That conclusion came from one call whose line noise happened to sit at 12.9-15.8 RMS. Two later
  calls showed noise reaching **1986** -- call-setup bursts the first call did not contain. Against
  a floor of 35, eleven of fifteen noise utterances were accepted and Whisper hallucinated words
  onto them ("Good job.", "We'll see you in the next one."), which AETHER then answered.

  Pooled across 28 utterances from three calls: real speech 5560-14971, noise 12-1986, a factor of
  2.8 apart. `AETHER_SPEECH_FLOOR=2500` admits 0 of 15 noise and loses 0 of 13 speech. Biased to
  the low end of the viable 2000-3500 band because dropping genuine speech is the worse failure
  (RULES.md R2.3).

  **The lesson is about sample size, not about telephony.** One call is not a calibration, and a
  threshold that separates cleanly on one call can be wrong by two orders of magnitude on the next.

- **Endpointing raised 500 ms -> 1000 ms, then REVERTED after the caller reported it worse.** (`AETHER_ENDPOINT_MS`, `DEFAULT_ENDPOINT_MS`). At
  500 ms a mid-sentence pause ended the caller's turn: one question arrived as "Can you tell me
  what are the...", "put available as", "The middle." and got three useless answers. Measuring the
  synthetic fixture showed something worse -- at 500 ms the utterance ended **before any trailing
  silence at all**, i.e. mid-speech, because 25 consecutive unvoiced frames occurred inside the
  speech itself.

  **Two costs, both paid on every turn.** Turn latency rises by the difference, and in hands-free
  mode voice interruption lands at end-of-utterance so barge-in slows by the same amount.

  **Why it was reverted.** It fixed fragmentation and broke something worse. A longer window stops
  the detector closing BETWEEN sentences as well as within them, so buffers grew from 1.2-4.2 s to
  6.7, 6.5 and 8.3 s -- `dur=6680 voiced=3900` for "Hi, can you hear me?" -- and Whisper handed six
  seconds of mostly silence started guessing: "Yes, can I help you with that?" for something never
  said. **The real fix for fragmentation is not a longer window, it is not sending the silence to
  STT.** Not attempted; recorded so the next attempt starts there rather than at the window again.

  **One measured side effect, which is why the tests changed.** `_ambient_rms` only updates on unvoiced frames while no utterance
  is active, so the longer the window, the quieter a room must be before it is ever measured. A
  synthetic room at sigma 0.02 was measured at 500 ms and is not at 1000 ms, widening the
  already-documented "loud room never measured" limitation. Acceptable because the absolute floor
  (2500 on telephony) does the work there, and ambient WAS measured on every real call (4.1-24.8).
  The noise-gate tests now pin `offset_frames` so they keep testing the gate rather than the
  endpoint, and `test_a_longer_endpoint_widens_the_unmeasured_room_limitation` owns the interaction.

### Fixed 2026-09-10

- **AETHER can take a booking, and the read-only guarantee survived it.** Room bookings, restaurant
  table bookings, table availability and cancellation -- 23 tools now, 3 of which write. The facts
  stay unwritable and SQLite still says so: the one read-write connection sits behind an authorizer
  permitting `reservations`, `table_bookings`, `guests` and the single column `rooms.status`, and
  refusing everything else at the driver. `mode=ro` narrowed, not abandoned.
  `rooms.status` had to be writable or the hotel contradicts itself one turn after a booking --
  and `record_change()` drops the room cache for the same reason, since rooms are cached on first
  read and "reserve room one zero one" followed by "is room one zero one free" would otherwise
  answer *yes* from a snapshot taken before the booking. `state_version` finally moves; it was
  stamped on every result from day one and had stayed at zero because nothing could write.
  A fenced booking never lands -- mutation-tested by moving the fence check after the tool body,
  which fails seven tests.
  Four things went wrong on the way and are worth keeping: `book*` as a stem matched "booking", so
  **"is there a booking on room two zero two" took a new booking on room 202** -- a lookup becoming
  a write, the worst failure a mutating tool has; "for two nights" matched the party-size frame and
  booked a table for two; a refusal built from an exception message spoke "room 305" with a bare
  numeral straight into Rime; and `\w+` matched only "द" of "दो", so every Hindi count failed --
  the Devanagari-matra fact, found for the third time.
  **And the suite wrote two reservations into the committed database.** Restored with
  `git checkout`; `tests/conftest.py` now redirects every run to a private copy via
  `AETHER_HOTEL_DB`, resolved at construction time because `router` and `clarify` build a store at
  import and a fixture would be too late.

- **Every keyword table was matched with `in`, and one of them finally bit.** Substring matching
  inside a sentence is a latent wrong-answer generator, and "night" is inside "tonight". The same
  trap was sitting unexploded elsewhere: "any" is inside "company", `"no "` is inside "casino ",
  "rate" is inside "corporate". Fixing it by making everything a whole word was not available --
  some tables MEAN a prefix, and `"allerg"` exists to catch allergy/allergic/allergen at once.
  So intent is now declared per entry: a trailing `*` marks a stem, everything else is a whole
  word or a phrase, and `_says()` compiles it. `tests/test_keyword_matching.py` walks EVERY table
  in the router and proves no entry can fire inside a longer word, so a table added later is
  covered without anyone remembering to come back.
  Verified by replaying all **414** distinct utterances from real traces: **2 changed**, both
  AETHER's own output lines that had been routing wrongly, both now correctly unrouted.

- **"Is it available tonight?" answered "we have forty-one rooms free".** Two separate defects, one
  symptom. First, `_ROOM_WORDS` was matched with `in`, and **"night" is a substring of "tonight"**,
  so any sentence containing "tonight" was a room question. Second, the room branch runs before the
  menu branch (deliberately -- "how much is an executive suite" contains a price word), so it
  claimed the sentence even when a dish was named. Room words are now matched as whole words, and
  the room branch yields when the caller has NAMED a dish. `_ALLERGEN_WORDS` and `_AVOIDANCE_WORDS`
  deliberately keep substring matching -- they hold prefixes like "allerg" and "no " with its
  trailing space -- so the whole-word helper is applied surgically rather than everywhere.
  "tonight" is now itself a room word, so "do you have anything free tonight" still routes to rooms.

- **The router had no conversational memory, and the demo sheet had a rule telling the presenter to
  work around it.** A rule telling a human to avoid a defect is not a fix. `aether/hotel/context.py`
  holds ONE subject -- the last concrete thing the caller was told about -- and splices it in where
  a referring word sits. Deliberately the smallest version that fixes the real complaint: a sentence
  that names its own subject is never overridden, a sentence with no referring word is left alone to
  fall through to the model, and "one" is not treated as a pronoun because it appears in "room one
  zero one" and "one night".
  **Committed at the spoken boundary, next to `history.commit_turn`, and cleared at the start of
  every turn.** So a fenced turn leaves no subject: "it" can only mean something the caller actually
  heard. That is the golden invariant applied to reference rather than to output, and putting the
  update beside history rather than in the router is what makes it fall out for free. The subtle
  leak -- a fenced turn's pending subject still sitting there when the NEXT turn commits -- has its
  own test.

- **An unknown room sounded like a mishearing.** "Is room four one two free" rendered the generic
  "I could not find that, could you say it again?", so a caller who spoke clearly heard the agent
  fail to hear them and repeated the same impossible number louder. `_room_status` now treats an
  absent room as an ANSWER: it names the room and offers the range that does exist, in all three
  languages, with the bounds DERIVED from the rooms table like `floors()` rather than written down.

- **Room numbers were said digit by digit in all three languages**, because the English convention
  was copied into the other two -- the Hindi docstring literally said "for the same reason as in
  English", which is the reasoning error rather than a typo. A Hindi speaker asks for room 101 as
  "एक सौ एक"; "एक शून्य एक" reads as a phone number or a PIN. Hindi and Spanish now delegate to
  `say_number`; English keeps digit-by-digit, which is genuinely the English convention.
  English also changed "oh" to "zero": both are ordinary English, but "one oh one" is the same short
  vowel three times and recognised badly on a real line -- the demo sheet carried a paragraph
  warning against saying it.
  The input side had to follow, or a caller could not repeat what they just heard: the router now
  recognises cardinal room numbers, **generated from the same `say_number` functions** rather than
  written out, for 100-999 rather than only the fifty rooms that exist -- so an unknown room still
  reaches the tool that says "I could not find that" instead of the availability rule answering
  "forty-one rooms are free" about a room the hotel does not have.
  **Found by the user, a Hindi speaker, reading the output.** No test could have caught it: every
  test asserted the digit-by-digit form, because the tests were written from the same wrong
  assumption as the code. The Spanish change follows by analogy and is NOT native-verified.

- **AETHER answered in three languages but only understood one.** `route()` matches English
  keywords, so a caller actually speaking Hindi or Spanish matched nothing and fell through to
  Gemini. The reply came back in the right language -- which is exactly why nobody noticed -- but
  it came from a model rather than from the database, so the project's central claim was false for
  every non-English caller. `test_language.py` could not have caught it: all 179 of its cases feed
  **English** questions and assert the **answer** is in the target language, which is a different
  property.
  Underneath it was a worse bug: `normalise()` used `[^\w\s-]`, and Python's `\w` is
  `str.isalnum()` plus underscore -- a Devanagari matra is category Mn/Mc, for which `isalnum()` is
  False. So "मेन्यू में क्या है" normalised to "म न य म क य ह" before any table was consulted.
  Hindi routing was **impossible**, not merely unimplemented, and adding keywords alone would have
  fixed nothing. `normalise` now keeps combining marks, tested by Unicode category rather than by
  codepoint range so the next script works without another edit.
  `aether/hotel/_foreign.py` maps Hindi (Devanagari and romanised) and Spanish keywords onto the
  English the router already keys on -- a vocabulary table, not a second router, so all the tested
  ordering rules stay tested exactly once. Dish and room names needed Devanagari entries even though
  they are never *spoken* in Devanagari: the renderers keep them in Latin because they are the
  hotel's proper nouns, but Whisper writes what the caller says. That asymmetry is why the input
  table cannot be a mirror of the output table. Pinned by `tests/test_foreign_routing.py` (62
  tests), including that English input passes through untouched -- a table that rewrote English
  would invalidate every English routing test at once.

- **Twelve hotel facts a caller asks for and the database could not answer.** Swimming pool, gym,
  spa, extra bed, doctor on call, taxi booking, conference room, power backup, restaurant hours,
  bar hours, deposit and ID at check-in. Each used to fall through to the model -- permitted by
  R8b.3, but a fact the hotel definitely knows should not be improvised differently on two calls.
  27 policy topics now, one authoritative row each, rendered in all three languages.
  Three of them needed their own branch rather than the generic template: "Yes, we offer the bar
  free of charge from five in the evening" is what the template produces for an opening time, and
  the accepted ID documents are an "or", not the "and" that `say_list` builds -- a checklist would
  tell a caller to bring all three. Router keywords for `restaurant` are all time-cued
  (`restaurant open`, `restaurant timings`) because policy words are matched BEFORE menu routing,
  and a bare "restaurant" would swallow "what is on the restaurant menu" -- the same defect
  "do you have room for this?" produced on a real call. Pinned by
  `tests/test_hotel_policies_expanded.py` (82 tests), which tests the theft case explicitly.
  Two fixtures elsewhere named "swimming pool" and "gym" as examples of things the database has no
  row for; both were moved to topics that are genuinely absent, and the absent-policy test now
  DERIVES its topic from the store so it cannot silently become a test of something else.

- **A trace could not say where its audio came from.** Latency and fencing were recorded; the input
  path was not, so a trace on its own could not distinguish a real telephone call from a laptop
  microphone -- the single most important thing a judge wants to know about a piece of evidence.
  `Trace.input_path` now carries `telephony` or `local_microphone`, declared by whichever entry
  point owns the session and stamped ONCE, on the next event after it is declared. "Next event"
  rather than "first event" is deliberate: telephony learns its path after pipeline setup has
  already emitted, and a first-event-only rule would have silently recorded nothing there -- which
  is indistinguishable from a run that was never told, and so would have destroyed the field's
  meaning. Never `browser`: the web console is a viewer over the same local-microphone session the
  CLI runs, and no audio travels from the browser, so "browser" would name the screen an operator
  was watching rather than the route the caller's voice took. `evidence/demo-run.jsonl` was NOT
  back-filled -- writing a true value into an old file would still be claiming the run observed
  something it never observed. Pinned by `tests/test_trace_input_path.py`, including an AST check
  that each entry point really assigns it (a grep would be satisfied by a docstring).

### Standing limitations

- **The real phone path is only partially validated.** Calls connect, are answered, are understood
  and are transcribed; see RIME_EVIDENCE Part 6. The prediction that laptop thresholds would not
  transfer was correct and the speech floor was re-derived on telephony audio (35 → 2500). **STT
  word accuracy on narrowband audio is still not separately measured**, and a trace does not record
  whether its audio came from the phone or the microphone — so no run can be quoted as telephony
  evidence unless it was identified as a call at the time.
- **Salvage does not exist.** A refinement reuses nothing, so it behaves exactly as a replacement
  and only the `TaskReplaced.reason` differs. `ResultSalvaged` is never emitted (RULES.md R8).
- **`AETHER_UNSAFE_MODE` is inert.** Parsed into `RuntimeConfig` and read by nothing, so the
  unsafe control condition for acceptance scenario G cannot be run and that test is skipped.
- **A status query is not answered aloud.** It protects the task in flight; speaking over it would
  need a second audio path able to bypass the gate's single active generation.
- **One caller at a time is the tested case.** Each call builds its own pipeline and its own menu
  store, so calls share no state, but concurrency has not been exercised.
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
- **Fixed 2026-09-07 — background noise was fencing turns, and the noise gate ran too late to
  stop it.** Reported live as "it is catching external noise and taking it as an interruption".
  The ordering was the bug: `on_onset` ducks at 40 ms, `on_voiced_progress` promotes that to a
  **fence** at 300 ms, but `_rejection_reason` only ran at the utterance *offset*, ~800 ms later.
  Measured on real runs: **6 of 7 rejected utterances had already ducked, stopped or fenced audio**
  before the gate declared them noise. The gate decided correctly and arrived after the turn was
  already dead. Fix: `on_voiced_progress` is now gated by `_is_credible_speech()`, which applies
  the same level test acceptance uses. **Ducking stays unconditional** (locked decision 6 — duck
  is not stop; ducking on a door slam is cheap and self-correcting, fencing on one is not).
- **Fixed 2026-09-07 (second pass) — the absolute floor I added rejected genuine speech.**
  Live runs showed real speech at 37-59 RMS being rejected as `below_noise_floor` against a floor
  of 60. Three compounding causes. (1) **My error:** `ambient_floor_min = 20` (floor 60) was
  calibrated from one session in which every *transcribed* utterance measured 61+; that is exactly
  the "absolute number that would need retuning per environment" this module's docstring warned
  against. (2) **`speech_rms` was a MEAN over voiced frames**, which is biased against long
  utterances — every extra word adds quiet voiced frames (inter-word gaps, trailing consonants)
  that drag the mean down, so 880-1640 ms utterances measured 52-59 while shorter comparable
  speech at the same distance measured 61-68 and passed. (3) Across all 47 transcribed
  utterances, real speech (61-2818) and junk (19-1315) **overlap on level**, so no absolute
  threshold separates them; duration separates far better (real min 500 ms, junk median 680 ms).
  Fixes: gate on **peak** voiced RMS rather than the mean (speech has vowel peaks, low-level noise
  is flat, and peak has no length bias); make the floor **configurable and calibratable**
  (`AETHER_SPEECH_FLOOR`, `MicVAD(speech_floor=...)`, `scripts/calibrate_mic.py`) with a low
  conservative default of 12.0; and record `speech_peak_rms` and `speech_floor` on `SpeechEnded`
  so a rejection can be explained from the trace. Replayed against the six wrongly-rejected
  utterances: **all six now accepted**, even scoring them by the pessimistic mean. The fence-time
  credibility gate is unchanged — and in push-to-talk it is moot for fencing anyway, because the
  mic is closed for the whole of the agent's turn, so external noise cannot fence a generation
  regardless of the floor.
- **Fixed 2026-09-07 — the relative noise floor was meaningless on a quiet microphone.**
  Live runs measured ambient at **0.5–9.7 RMS**, so `ambient x 3` was a bar of ~2, and background
  noise at RMS 10–40 cleared it twentyfold while real speech sat at 60–2800. A relative test
  against a near-zero floor is not a test. `speech_floor()` now clamps ambient by
  `ambient_floor_min = 20.0`, on the reasoning that a microphone reporting near-silence is
  describing its own noise floor rather than the room. The clamp is a *minimum*, so a genuinely
  loud room still dominates. Replayed against the real traces: **5 of the 6 turn-killing noise
  events no longer fence, and no genuine utterance is lost** (the single flagged loss was the
  Whisper hallucination `'This. This. This. This.'` at RMS 21.9). This **changes** the previous
  "when ambient has never been measured, accept unconditionally" contract; the test that encoded
  it was updated deliberately, not weakened.
- **Known limitation, found 2026-09-07 by the adverse-audio sweep — the noise floor disables
  itself in a loud room.** `_ambient_rms` is only updated on frames webrtcvad reports as *not*
  voiced. Once room noise is loud enough that the detector calls it speech, that branch stops
  running, ambient stays `None`, and `_rejection_reason` takes its documented
  "never measured → accept" path. So the `below_noise_floor` gate contributes nothing in exactly
  the loud room it exists for, and only `min_speech_ms` still filters. Measured: at room sigma
  0.02 ambient tracks to ~679 RMS and the gate works; at 0.05 and above ambient is never measured
  at all. **Not fixed** — the VAD architecture is deliberately untouched, and choosing between an
  energy-based floor, a decaying estimate or a warm-up sample needs real-room evidence rather than
  synthetic tone. Pinned by `tests/test_adverse_audio.py` so it is visible rather than surprising.
  Headphones and a close mic remain required for the demo.
  **Three fixes were attempted on 2026-09-07 and all three reverted** — minimum statistics over a
  trailing RMS window (landed on the offset silence, floor ~0); snapshotting the floor at onset
  (in a loud room webrtcvad flags the room as voiced from frame 1, so onset fires with two frames
  of history and never fires again); and a silence-filtered window minimum (engaged the floor, but
  made the gate **non-monotonic** — at sigma 0.05, amp 0.02 accepted while amp 0.35 was rejected,
  which is worse than the permissive default). Root cause, measured: **from sigma 0.02 upward
  webrtcvad calls 100% of room-noise frames voiced** (50/50 at 0.02, 0.05, 0.10), so `speech_rms`
  is contaminated by room noise and any floor from the same frames is contaminated by speech. No
  RMS statistic can separate signals the detector has already merged. A fix needs a *different
  signal* — AEC, spectral features, or a separate noise estimator — plus real-room evidence.
- **Fixed 2026-09-07 — two credential-disclosure paths.** (1) All three config dataclasses held
  `api_key` as an ordinary field, so `repr(RimeConfig.from_env())` rendered the live Rime key in
  plaintext — any traceback or log line carrying a config would have put it on screen, against the
  PHASES.md Day 6 rule. Fixed with `field(repr=False)`. (2) That was **not sufficient**:
  `Event.to_json` flattens the payload with `asdict()`, which recursively expands nested
  dataclasses and re-includes fields excluded from `repr`, so passing a config as an event field
  wrote the key into `traces/*.jsonl` — a file the evaluator reads and that gets quoted as
  evidence. Fixed with `aether.trace.redact()`, applied at the single point every event passes
  through; it replaces the value and preserves structure, so traces stay valid JSONL. Both pinned
  by `tests/test_secret_redaction.py` using a sentinel, so no real credential is involved.
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
   **Mostly done; two items remain human-only.** The endpoint, and all three shipped
   model/voice/language combinations, were queried against Rime's live public catalogue and
   re-checked on **2026-09-10** (594 entries): `mistv3`/`astra`/`eng`, `coda`/`nadi`/`hin`,
   `mistv3`/`isa`/`spa` are all present, and each voice's catalogued language matches the `lang`
   AETHER sends. **Still not done: account tier and rate limits**, which the public catalogue does
   not expose, and **a listening judgement of the Hindi and Spanish voices by a speaker of each** —
   neither can be established by an agent. The earlier note here ("every Rime config value is a
   placeholder") described the state before the key existed and is no longer true.
2. **Organizer / event preflight** — eligibility, registration, submission format, deadline, demo
   length, expected evidence. **Not done.**
3. **Differentiation review** — **REDONE 2026-09-10 against the real catalog**, written up in
   JUDGING.md. The catalog is `github.com/rimelabs/rime-dev-projects` (the user supplied the link
   the PDF hides): **16 projects**, read from its own `data/projects.json`. 14 have public source;
   **Continuum** and **NOVA** are 404 and were assessed from their summaries only — Continuum is
   the nearest by description and its code is unreadable, which is stated rather than glossed.
   The earlier pass reviewed the AssemblyAI showcase as the closest reachable equivalent. **That
   was the wrong list**, and its finding ("not one addresses interruption, telephony or
   multilingual routing") was **false** against the real catalog and has been withdrawn.
   What the real catalog contains: interruption/barge-in in **8 of 14**, multilingual in **6**,
   a measured latency figure in **5**, a test suite in **4**, telephony in **2**.
   **FieldMate implements turn/generation fencing against stale async work** -- the same idea as
   AETHER's, arrived at independently -- and is faster (~600 ms warm end-to-end vs AETHER's
   ~1.26 s). **Saathi** uses Rime Coda/`nadi` for Hindi over real Twilio calls, the same voice
   AETHER uses. **RoutineCraft AI** claims barge-in halting playback in <60 ms.
   AETHER's remaining, narrower contribution: the invariant is about **spoken output** rather than
   state and reaches into the audio gate; interruption is split into **six classes** rather than
   treated as one event; conversation state commits only at the spoken boundary; tool results are
   fenced as well as model output; over a real PSTN call in three languages.
4. **Fallback disclosure decision** — decide whether any TTS fallback exists and disclose it
   explicitly if so.
5. **Credentials** — obtain and place API keys in a local `.env`. Never committed.
   **Rime: DONE** — `.env` exists, the key is set, and both the HTTP and `/ws3` clients have
   executed against the live API.
   **LLM: DONE for Gemini** — `GEMINI_API_KEY` is set and the reasoning path has executed end to
   end. `GROQ_API_KEY` is present but does **not** authenticate; anthropic/openai are unset.
6. **Live demo recording** — real mic, real speech, real interruption.
7. **Secrets sweep before recording** — no credential visible in any frame.
8. **Run the live-microphone latency measurement** — `python scripts/measure_audio_kill.py
   --mode mic --repeats 5`, wearing headphones. Needs a person to speak; the synthetic-mode number
   does not substitute for it.
9. **~~Choose an LLM provider~~** — DONE (2026-09-07): `LLM_PROVIDER=gemini`, model
   `gemini-flash-lite-latest`, chosen by measurement (§5). Historical note: until then
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
