# AETHER FAST REFERENCE

> Retrieval sheet for answering questions about AETHER without reading the repository.
> Every number here was re-verified against the working tree on **2026-09-19**.
> Anything unverifiable is marked **`NOT VERIFIED`**. Do not fill those in.
> Synthetic vs real-phone measurements are labelled everywhere they appear.

---

## 1. One-Sentence Definition

AETHER is a multilingual (English/Hindi/Spanish) telephone voice agent for a hotel that answers hotel questions from a SQLite database through deterministic routing and spoken-form templates — never from an LLM — and guarantees, through generation fencing, that an answer the caller interrupted is never spoken and never remembered.

---

## 2. Core Problem

**What AETHER solves.** Hotel front desks lose calls to hold queues at night and in peak hours. Callers want prices, availability, policies, food orders and bookings — all facts that exist in a database and must be spoken correctly, in the caller's language, over a phone line.

**Why telephone voice agents differ from chatbots:**

| Chatbot | Telephone voice agent |
|---|---|
| Turn boundaries are explicit (Enter key) | Turn boundaries must be inferred from audio |
| User can re-read the answer | Answer exists only once, in time |
| A wrong answer is visible and correctable | A wrong answer is already in the caller's ear |
| Latency of 3 s is acceptable | 3 s of silence reads as a dropped call |
| Output can be deleted before it is seen | Audio cannot be un-spoken |
| No half-delivered state | A response can be half-spoken when interrupted |

**Why interruption/barge-in is the main engineering problem.** A caller interrupting mid-answer creates work already in flight: an LLM streaming tokens, a TTS socket producing audio, a tool about to write a row. Every one of those can complete *after* the caller has moved on. Without a fence, the caller hears the answer to the question they abandoned — or worse, a booking lands for a room they stopped asking about. This is the failure AETHER is built to make structurally impossible.

---

## 3. Golden Invariant

> **A stale/abandoned generation must never become spoken output.** (RULES.md R1)

- **Generation ID** — every turn allocates a generation id (`G1`, `G2`, …) from `GenerationRegistry`. All work for that turn carries the id.
- **Fencing** — marking a generation invalid. State in `aether.events.GenerationStatus`: exactly two values, `active` and `fenced`.
- **Fencing ≠ cancellation:**
  - *Cancellation* tries to stop work already running — racy, best-effort, and it cannot un-send audio already on the wire.
  - *Fencing* is a flag write that does not stop anything. In-flight work is allowed to finish and is then **discarded at every output boundary**. Fencing is a validity check at the door, not a kill signal — which is why it cannot lose a race.
- **When a generation becomes fenced:**
  1. `GenerationRegistry.allocate()` — allocating the next turn fences the previous one. This is the primary mechanism: starting a new turn *is* the fence.
  2. `BargeInCoordinator.fence_now(reason=…)` — explicit fence from caller voice, the console Interrupt button, or the keyboard. All three reach the same method, distinguished only by `reason` in the trace.
  3. `CANCEL` classification — fenced, and no successor task starts.
- **What is discarded from an unheard turn:**
  - the spoken text (never enters conversation history)
  - the tool result and any tool side effect (the tool body never runs)
  - queued TTS audio chunks (gate queue cleared)
  - the conversational subject — a fenced answer cannot be referred back to by "it"
  - Recorded as `ResultDiscarded`; console shows it struck through, never as a spoken turn.

---

## 4. Architecture — One Screen

```
Caller (PSTN phone)
  → LiveKit SIP            telephony transport, 48 kHz          no LLM
  → CallBridge/frames      rtc.AudioFrame ⇄ AudioChunk, resample no LLM
  → VAD (aether/audio/vad) speech onset/offset detection         no LLM
  → STT (Groq Whisper)     audio → text                          no LLM  ← Groq ONLY transcribes
  → Classifier             6-way interruption class, keyword/prefix rules  no LLM (deterministic)
  → Router                 sentence → tool name + args, keyword tables     no LLM
  → ├─ Deterministic tool  33 tools, read SQLite                 no LLM   (llm_ms = 0)
    └─ Gemini fallback     ONLY when no tool matches             LLM
  → Data source            data/aether_hotel.db (SQLite)         no LLM
  → Renderer               per-language spoken-form template     no LLM
  → Rime TTS               text → audio over /ws3 websocket      no LLM
  → AudioGate              duck / stop / fence check on playback no LLM
  → Caller
```

| Component | Implementation / provider | Responsibility | LLM involved |
|---|---|---|---|
| Telephony | LiveKit SIP + `livekit.agents` `AgentServer` | Carry the call; one job process per call | No |
| Audio bridge | `aether/telephony/frames.py`, `aether/bridge` | Frame conversion, resampling 48k ⇄ 16k | No |
| Speech detection | `aether/audio/vad.py` | Onset/offset, 500 ms trailing-silence endpoint | No |
| STT | Groq Whisper (`STT_PROVIDER=groq`); local faster-whisper fallback | Speech → text only | No |
| Interruption classification | `aether/classify` | 6 classes, closed word sets + prefixes | No |
| Router | `aether/hotel/router.py` | Sentence → tool + args | No |
| Deterministic tools | `aether/hotel/tools.py` (33 tools) | Read/write the hotel DB | No |
| LLM fallback | Gemini `gemini-flash-lite-latest` | Only what no tool can answer | **Yes** |
| Data source | SQLite `data/aether_hotel.db` | Single source of truth | No |
| Renderer | `tools.py` / `tools_hi.py` / `tools_es.py` | Row → spoken sentence | No |
| TTS | Rime, websocket `/ws3` | Text → audio | No |
| Playback gate | `aether/audio/player.py` `AudioGate` | Duck, stop, final fence check | No |
| Console | `aether/web/server.py` + static HTML over WebSocket | Observation only; sends 2 actions | No |

---

## 5. Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | Python 3.12 | Whole system |
| Telephony | LiveKit SIP, `livekit-agents` (`AgentServer`, `rtc_session`) | Inbound PSTN calls |
| STT (production) | Groq Whisper (`STT_PROVIDER=groq`) | Speech recognition |
| STT (local fallback) | faster-whisper, CPU (`base.en` English, `base` multilingual) | Offline recognition |
| VAD | Local VAD in `aether/audio/vad.py` | Speech onset/offset |
| Classification | Pure Python keyword/prefix tables | Interruption typing |
| Routing | Pure Python keyword tables + regex | Intent → tool |
| Database | SQLite (`data/aether_hotel.db`), `mode=ro` + authorizer on the write connection | Hotel facts |
| Learned answers | Separate SQLite DB (`aether/hotel/learned.py`) | Model-answer consistency cache |
| LLM | Google Gemini `gemini-flash-lite-latest` (4 providers implemented behind one interface) | Fallback answers only |
| TTS | Rime — `mistv3`/`astra` (eng), `coda`/`nadi` (hin), `mistv3`/`isa` (spa), websocket `/ws3` | Speech synthesis |
| Audio I/O | PortAudio via `sounddevice`, NumPy | Playback, gating |
| Console | `http.server` + WebSocket, single static HTML file, vanilla JS | Live observation |
| Tracing | JSONL event trace (`aether/trace.py`) | Evidence, replay |
| Tests | pytest — 1970 passing | Verification |

---

## 6. Generation Fencing — Exact Implementation

**Five independent checkpoints.** Each is a separate `ResultDiscarded` reason, so the trace shows exactly which door stopped it.

| # | Reason string | Location | Failure it prevents |
|---|---|---|---|
| 1 | `stale_generation_menu` | `aether/spike.py:980` | A deterministic hotel answer, resolved before the interruption, being spoken after it |
| 2 | `stale_generation_tool` | `aether/tools/__init__.py:44` | **The tool body running at all** — checked after the delay, before execution, so a stale booking never writes a row |
| 3 | `stale_generation_llm` | `aether/spike.py:1038` | A completed Gemini response for an abandoned question being spoken |
| 4 | `stale_generation_stream` | `aether/spike.py:1195` | A partially streamed LLM response continuing into TTS |
| 5 | `stale_generation` | `aether/audio/player.py:439` | The last door: audio chunks for a fenced generation being written to the device |

**TTS queue clearing.** `AudioGate.request_stop()` clears the pending chunk queue (`self._queue.clear()`) and supersedes any pending duck/resume — applying a resume after a stop would restore audio the gate had already cut. Refused chunks are drained by a watcher into `ResultDiscarded` events rather than silently dropped.

**Interruption ducking.** Duck and stop are different actions with different triggers (RULES.md R6):
- **Duck** — immediate, reversible, applied on `SpeechOnset` *before any understanding exists*. Gain reduction, not a stop.
- **Stop** — confirmed, irreversible, applied once the speech is classified as meaningful.
- On the **telephony path AETHER never ducks** — hands-free mode. A ducked-but-never-fenced utterance would take the caller's audio down for nothing.

**Why stale tool side effects are prevented.** Booking and ordering tools are declared `mutating` in `HOTEL_TOOLS`. `ToolRunner` checks generation validity *after* the artificial delay and *before* the tool body. A caller who changes their mind while a booking is in flight leaves **no row behind**. This is stronger than the headline claim: not only is a stale answer never spoken, a stale *intention* never lands.

---

## 7. Interruption Types

Six classes in `aether.classify.InterruptionClass`. The registry remains final authority: every class except `BACKCHANNEL` and `STATUS_QUERY` proceeds into the turn machinery.

| Interruption Type | Example | Behaviour |
|---|---|---|
| `BACKCHANNEL` | "mm-hm", "yeah", "okay" *while AETHER is speaking* | Does **not** fence, does not start a turn. Closed word set; only meaningful against something in flight |
| `REFINEMENT` | "actually, make that two nights" | Corrects the question in flight. Prefix-matched (`prefix:actually`). Fences with reason `REFINEMENT` |
| `REPLACEMENT` | Any new sentence spoken *while AETHER is answering* | Default when something is in flight. Fences the active generation, starts a new turn |
| `STATUS_QUERY` | "are you still there?" | Protects the task; does **not** fence and does **not** speak over it. No spoken status answer is implemented |
| `CANCEL` | "never mind", "forget it" | Whole-utterance closed set. Fences, and **no successor task starts** |
| `NEW_TASK` | Any sentence spoken into silence | Default when nothing is in flight. Normal turn |

**Observed across 106 committed traces:** `NEW_TASK` 129 (`default_idle`), `REPLACEMENT` 34 (`default_in_flight`), `REFINEMENT` 2 (`prefix:actually`), `BACKCHANNEL` 1 (`closed_set`). `STATUS_QUERY` and `CANCEL` have **never been exercised in a committed trace** — implemented and unit-tested, not trace-evidenced.

---

## 8. Router and LLM Boundary

**What deterministic routing handles.** Everything the database can answer: menu, prices, allergens, dietary filters, room types, room rates, room status and availability, hotel policies, services and hours, check-in/out, bookings (room and table), food orders and order edits, booking lookup by reference, guest-privacy refusals. 33 tools.

**Why the router is not an LLM:**
- A row is correct; a model is probable. Prices must not be probable.
- Median deterministic route+render latency is **0.90 ms** vs a measured **0.9–1.8 s** Gemini round trip.
- A router is inspectable and testable per-sentence; 93 routing cases are asserted in `tests/test_understanding.py`.
- A deterministic turn reports `llm_ms = 0` in the trace — visible, checkable proof no model was consulted.

**When Gemini is called.** Only when `route()` returns `None` — no tool matched the sentence.

**What Gemini is allowed to answer.** General hotel-and-stay questions the database has no row for (e.g. "what's the area like?", "is the Executive Suite good for a family?"). Constrained by prompt: never a text assistant/chatbot/AI self-description, brief voice-first answers, never invent a dish, scope-gated to hotel-and-stay topics (RULES.md R8b.7).

**What must never depend on Gemini.** Every price, allergen, policy, rate, room number, availability answer, booking outcome and order total. These are intercepted by the router before the model is reached.

**What happens on ambiguous routing:**
- Near-miss dish/room names → `aether/hotel/clarify.py` `nearest()` produces a "did you mean" repair.
- Unknown dishes in an order → named explicitly plus what *is* available (RULES.md R8b.12) — never silently dropped.
- No match at all → Gemini, with the scope gate.
- A model answer that is accepted is written to a **separate** learned-answers DB so the same question gets the same answer next time — consistency, explicitly **not** truth (RULES.md R8b.8).

---

## 9. Database Architecture

**Databases used — two, deliberately separate:**

| Database | Contents | Why separate |
|---|---|---|
| `data/aether_hotel.db` | The hotel: menu, rooms, policies, services, guests, reservations, orders | Source of truth |
| Learned-answers DB (`aether/hotel/learned.py`) | Model answers replayed for consistency | What the model made up must not sit in the same file as what the hotel actually charges |

**Current hotel tables (row counts verified 2026-09-19):**

`guests` 6 · `hotel` 1 · `hotel_policies` 27 · `hotel_services` 6 · `menu_categories` 5 · `menu_items` 12 · `reservations` 5 · `restaurant_order_items` 4 · `restaurant_orders` 2 · `room_types` 5 · `rooms` 50 · `service_requests` 0 · `table_bookings` 0

**Read vs write.** `HotelStore` opens the DB with `mode=ro` — a write is refused by SQLite, not by convention. Only `aether/hotel/bookings.py` opens a writable connection.

**SQLite authorizer.** The write connection installs a `sqlite3` authorizer (`bookings.py:157`, installed at `:194`) that **denies any write outside**:

| Writable | Scope |
|---|---|
| `reservations` | full table |
| `table_bookings` | full table |
| `guests` | full table |
| `restaurant_orders` | full table |
| `restaurant_order_items` | full table |
| `rooms.status` | **that one column only** |

**What cannot be modified — refused by the driver, mid-statement, whatever the code asks for:** every price, allergen, policy, room rate, room number, menu item, service hour. A tool with a bug cannot reprice the menu; a model never reaches this class at all. `unit_price_inr` on an order is **copied** from `menu_items` at order time; the menu row is never touched. `tests/test_bookings.py` proves this by trying.

**Concurrency protection.** `BEGIN IMMEDIATE` takes SQLite's database-wide write lock *before* the room is chosen (`bookings.py:222`), plus a conditional claim — the UPDATE only succeeds if the room is still free — plus a `threading.Lock`. Two callers cannot take the same last room.

---

## 10. Supported Capabilities

**33 tools: 25 read-only, 8 mutating.**

### Hotel Information
`hotel_info`, `hotel_policy`, `list_services`, `service_hours`, `check_in_out`, `room_amenities`, `list_room_types`. 27 policies, 6 services in the DB.

### Room Availability
`check_availability`, `room_availability`, `room_status`, `room_free_from`, `reservation_for_room`. Availability derived from `rooms.status` everywhere. Rooms numbered 101–510 (50 rooms); an out-of-range number is refused by name ("we do not have a room nine nine nine").

### Room Booking
`reserve_room` (mutating), `cancel_booking` (mutating). Writes `reservations` + `guests` + `rooms.status`. Returns a spoken reference number. Caller is recorded as `"Telephone booking"` — a name is never invented and a guest name is never spoken (`tests/test_language.py` enforces).

### Table Booking
`reserve_table` (mutating), `table_availability`. Party-size cap enforced with a refusal that names the limit.

### Food Ordering
`add_to_order`, `place_order`, `cancel_order` (all mutating), `repeat_order`, `menu_overview`, `list_category`, `price_of`, `describe_item`, `find_by_diet`, `check_allergens`, `safe_for`. Multi-turn: add across turns, repeat, place, cancel. Quantities with unit words ("two plates of biryani") are parsed. Max 20 per dish.

### Order Modification
`remove_from_order` (mutating). Handles removal, quantity-specific removal ("remove one" ≠ "remove the"), and replacement ("replace X with Y" removes **and** adds). RULES.md R8b.11: **edits are never additions.**

### Booking/Order Memory
`my_booking`, `booking_status`, `cancel_my_booking`. Session memory recalls the caller's own booking within a call; `booking_status` looks a booking up by reference across calls, searching both `reservations` and `table_bookings`.

### General Hotel Questions
Anything with no matching tool → Gemini, scope-gated to hotel-and-stay topics. Answers are cached in the learned-answers DB for consistency.

### Privacy/Safety Behaviour
`guest_privacy` — declines to reveal who is staying in a room. **Reads no guest record at all**; this is enforced structurally by inspecting the function's own source in the test suite. Responds with a warm one-sentence refusal that names what *can* be answered (RULES.md R8b.13).

### Multilingual Behaviour
English, Hindi, Spanish. Same router, same tools, same database; per-language renderers. See §12.

---

## 11. Multi-Turn State

- **Pronouns ("it", "that one").** `aether/hotel/context.py` keeps a conversational subject — the last dish/room/booking actually spoken about. "How much is it?" resolves against that subject.
- **Open food order state.** An order under construction lives in session state and is written to `restaurant_orders`/`restaurant_order_items` as it is built; `place_order` moves it to the kitchen's own status vocabulary (a CHECK constraint over four values).
- **Booking reference memory.** `last_booking` is held on the session for same-call recall (`my_booking`); `booking_status` retrieves by reference across calls straight from the DB.
- **Interrupted/unheard turns do not enter memory.** Conversation history is appended only *after* the gate confirms audio was actually spoken — `spike.py` returns early on every fenced path before the history line is reached. A fenced answer also cannot become the conversational subject, so "it" never refers to something the caller never heard.
- **Session state vs LLM context.** Session state (subject, open order, last booking) is Python state owned by AETHER and is authoritative. LLM context is a transcript window handed to Gemini only on fallback turns. Hotel facts are never recovered from LLM context.

---

## 12. Multilingual Architecture

| | English | Hindi | Spanish |
|---|---|---|---|
| Code | `eng` | `hin` | `spa` |
| Rime model | `mistv3` | `coda` | `mistv3` |
| Rime voice | `astra` | `nadi` (female) | `isa` (female, Mexican) |
| Whisper model | `base.en` (monolingual) | `base` (multilingual) | `base` (multilingual) |
| Whisper code | `en` | `hi` | `es` |

Defined once in `aether/lang/__init__.py`; nothing else decides what a language is.

- **One database.** No per-language fact tables. A Hindi price and an English price come from the same row.
- **Same tools.** The router and all 33 tools are language-independent.
- **Different renderers.** `tools.py` / `tools_hi.py` / `tools_es.py` hold spoken-form templates; `speech_hi.py` / `speech_es.py` hold number, price, time and room-number speech. RULES.md R8b.6: an English-only capability is a defect.
- **Router understanding.** The router matches all three languages, including Hinglish. Verified at **93/93 (100%)** — 31 sentences per language across 4 capability suites. `tests/test_understanding.py` also asserts the three languages are tested *equally* and that no question reaches a mutating tool.
- **Language switching.** Mid-call, on request. Greeting offers it. Detection runs on the transcript. Hindi opens a second Rime socket lazily — an English-only call never pays for it (speaker, model and language are baked into the `/ws3` connect URL, so one socket is one voice).
- **Current limitations.** Hindi/Spanish have **never been exercised over the telephone**. Wording is **not fully native-speaker reviewed** (one review found a real defect — room numbers spoken digit-by-digit; the Spanish equivalent was fixed *by analogy*, not on a native speaker's correction). The shipped Hindi voice **has not been listened to** — `arcana`/`anaya` was chosen, then Rime deleted the entire `arcana` model; `coda`/`nadi` was selected on catalogue and latency evidence only. **No automatic language detection** — by design.

---

## 13. Speech Pipeline

### STT
- **Provider/model:** Groq Whisper (`STT_PROVIDER=groq`, `STT_MODEL=base.en`). Local faster-whisper (`base.en` / `base`) is the alternative; selection is explicit and an unknown value **raises** rather than silently substituting.
- **Protocol:** HTTPS to Groq; local path is in-process.
- **Sample rate:** transport 48 kHz (LiveKit); VAD and STT at 16 kHz; the bridge resamples both ways.
- **Streaming:** utterance-based, not token-streaming. Endpointed on 500 ms trailing silence.
- **Interruption behaviour:** onset drives duck/fence decisions before any text exists.
- **Configurable:** `STT_PROVIDER`, `STT_MODEL`, `GROQ_STT_MODEL`.

### TTS
- **Provider/model:** Rime. `mistv3`/`astra` (eng), `coda`/`nadi` (hin), `mistv3`/`isa` (spa). Configurable via `RIME_MODEL`, `RIME_VOICE`.
- **Protocol:** websocket `/ws3`. Speaker, model and language are in the connect URL — one socket is one voice; another language needs another socket, opened lazily.
- **Sample rate:** 48 kHz out to LiveKit.
- **Streaming:** sentence-streamed; connection reuse matters — handshake 920 ms vs synthesis 520 ms cold; reuse took one turn's TTS from **2849 ms to 389 ms**.
- **Interruption behaviour:** `AudioGate` clears the queue on stop and refuses chunks belonging to a fenced generation.
- **No fallback TTS.** Unconfigured means silence, not substitution — deliberate.

---

## 14. Current Verified Metrics

| Metric | Verified Value | Evidence Source |
|---|---|---|
| Tests passing | **1970 passed, 2 skipped** (1905 collected) | `python -m pytest -q`, run 2026-09-19 |
| Skip 1 | `AETHER_UNSAFE_MODE` control condition — no code path allows a stale result out, so the control cannot be run | `tests/test_acceptance.py:283` |
| Skip 2 | `ResultSalvaged` not implemented and deliberately not faked | `tests/test_acceptance.py:319` |
| Full-duplex acceptance | **9/9 pass** | `tests/test_full_duplex_acceptance.py` |
| Committed trace files | **106** `.jsonl` | `traces/` (directory also holds a README) |
| **Stale outputs spoken (`ResultLeaked`)** | **0**, across all 106 traces | Re-counted 2026-09-19; RIME_EVIDENCE.md updated to match |
| `ResultDiscarded` events | **4136** | 106 traces |
| `ResponseSpoken` events | **319** | 106 traces |
| Hotel tools | **33** (25 read-only, 8 mutating) | `aether.hotel.tools.HOTEL_TOOLS` |
| Multilingual routing accuracy | **93/93 = 100%** (31 per language) | `python scripts/measure_understanding.py` |
| Deterministic route+render latency | median **0.90 ms**, mean 2.05 ms, p95 **7.14 ms**, max 22.38 ms | same script — *no audio, no network, no model* |
| … by language | eng median 0.98 ms · hin 0.90 ms · spa 0.81 ms | same |
| **Real phone call** — turns | **16 turns, 11 with `llm_ms = 0`**, 2 generations fenced, 0 leaks | `evidence/demo-run.jsonl` + `evidence/demo-call-worker.log` |
| **Real phone call** — turn latency | median **1262 ms** | same |
| **Real phone call** — deterministic turns | 1119–1443 ms | same |
| **Real phone call** — LLM fallback turns | 1967–2549 ms | same |
| **Real phone call** — STT | 841–1147 ms | same |
| **Real phone call** — Rime first audio | median **280 ms** | same |
| Playback (enqueue → device callback) | warm median **9.2 ms** + a 22 ms device buffer, reported and never added | `scripts/measure_playback_latency.py` |
| Interrupt → silence (`audio_kill_latency_ms`) | **SYNTHETIC**: n=8, min 301.65 / median **321.24** / max 333.18 ms | `run-20260906T020258Z` |
| Duck latency | **SYNTHETIC**: n=8, min 1.27 / median **21.95** / max 30.39 ms | `run-20260906T020258Z` |
| Interrupt→silence and duck, **live mic / human speech** | **NOT VERIFIED** | `<from_run>` in RIME_EVIDENCE.md |
| Rime model choice (`mistv3` vs `mistv2`) | `mistv3` **180 ms (37%) faster** to first audio | RIME_EVIDENCE Part 1 |
| Connection reuse | 2849 ms → **389 ms** for one turn's TTS | RIME_EVIDENCE |
| STT word accuracy, **narrowband phone audio** | **NOT VERIFIED — never measured** | RIME_EVIDENCE.md, JUDGING.md, DEMO.md all state this |

**STT recogniser comparison — SYNTHETIC** (`aether/stt_groq.py:180-188`). Method: each language's real deterministic answers spoken by the Rime voice that would speak them, transcribed by each recogniser, digit notation normalised. **This is a Rime→Whisper round trip, not human phone speech.**

| Language | Local | Groq | Note |
|---|---|---|---|
| English | 95.4% | 95.4% | identical words; Groq 2–4× faster |
| Spanish | 92.4% | 98.6% | |
| Hindi | **21.7%** | **89.2%** | local returns Urdu script / romanised transliteration — unusable |

**Untested:** concurrency (two simultaneous callers). No regional Rime endpoint pinned; no regional comparison run.

---

## 15. Safety / Reliability Mechanisms

| Mechanism | Implementation |
|---|---|
| Stale-generation fencing | 5 independent checkpoints (§6); 0 leaks in 106 traces |
| Database authorizer | `sqlite3` authorizer denies writes outside 5 tables + `rooms.status` |
| Read-only default | `HotelStore` opens `mode=ro`; only `bookings.py` can write |
| Concurrency control | `BEGIN IMMEDIATE` before room selection + conditional claim + `threading.Lock` |
| Deterministic hotel facts | 33 tools; `llm_ms = 0` is visible per turn in the trace |
| Privacy restrictions | `guest_privacy` reads **no** record; enforced by source inspection in tests. Caller number redacted in committed evidence and asserted redacted |
| Secret handling | `repr=False` on every key field so a traceback/log/trace never renders a credential; `missing_config()` reports by NAME only; `tests/test_secret_redaction.py` |
| Allergen behaviour | `check_allergens`, `safe_for` read the DB; the answer names dishes to avoid **and** warns that other dishes may contain the allergen — never an unqualified "safe" |
| Hallucination mitigation | Router intercepts before the model; "never invent a dish" reinforced by demonstration (instructions alone measured unreliable at 2/5 on `gemini-flash-lite-latest`); scope gate; learned answers stored separately |
| Router fallback | Near-miss → `clarify.nearest()` "did you mean"; unknown order items named explicitly with what *is* available; no match → scope-gated Gemini |
| Doc/reality drift | `tests/test_docs_are_current.py` asks pytest for the real test count and fails any doc quoting a stale one; also checks dead claims, removed tools, broken links |
| Test DB isolation | `tests/conftest.py` copies the shipped DB per session — the suite once mutated the committed database |
| Mutation testing | **NOT VERIFIED** — no mutation-testing harness found in the repository |

---

## 16. Known Failure Modes and Fixes

| Failure Observed | Root Cause | Fix |
|---|---|---|
| "dessert" transcribed as "Desert"; model answered from thin air | Router missed, LLM filled the gap; "never invent a dish" instruction alone did not hold | Demonstration-based prompt (small models follow examples, not rules) + router repair |
| "aisle 9" vs "IL-9" | Multilingual `base` worse than monolingual on English | `base.en` kept for English permanently |
| Hindi returned Urdu script and romanised transliteration | Local multilingual `base` unusable for Hindi (21.7%) | Hosted Groq Whisper (89.2%); multilingual product depends on it |
| Room numbers spoken digit-by-digit in Hindi ("एक शून्य एक" = a phone number) | English convention copied into other languages | Cardinal form ("एक सौ एक"); found by the one native-speaker review, 2026-09-10 |
| `reserve_room` triggered by *questions* in Hindi/Spanish ("is there a booking on room 305", "cancel booking 1004") | English protected by exact-form verbs; Hindi/Spanish use the same word for the noun | Built protection per language; guarded by `test_no_question_is_answered_by_a_mutating_tool_unless_it_asks_for_one` |
| "remove paneer butter masala" **added** it, compounding 1→2→4 | No removal rule existed; the sentence matched the dish and fell through to add-order | `_REMOVE_WORDS` + dedicated `remove_from_order`, checked before the add rule |
| "two plates of biryani" ordered as 1 | Number not adjacent to the dish name | `_UNIT_WORDS` allows a unit phrase between number and dish |
| Shorthand dish names always quantity 1 | Quantity searched the *canonical* menu name, not the caller's words | `_find_dish_spans()` returns the caller's actual surface words |
| "remove one X" removed **all** X | `_find_quantity` defaulted missing to 1, so "no number" and "1" were indistinguishable | `_stated_quantity()` returns `None` for "no number stated" |
| Unknown dishes silently dropped | No detector | `_find_unknown_items()` + `_what_we_could_not()` — names what is missing *and* what is available, in all three languages |
| "replace X with Y" only removed X | No swap-preposition table | `_SWAP_TO_WORDS` + `_split_remove_and_add()`; `>=` tie-break so "instead of" beats "instead" |
| "who is staying in room X" answered as availability | No privacy rule; matched room-status keywords | `guest_privacy` tool that reads nothing, routed before reference lookup |
| Booking 1009 misread as room 100 | `_find_room_number()` matched a 3-digit run inside a longer number | Rejects a 3-digit run that is part of a longer digit sequence |
| Language-selection greeting missing from the console transcript | `speak_greeting()` never emitted `RESPONSE_SPOKEN` | Emits it, only when `result.accepted` |
| Caller waited 13.0 s and 17.3 s before the greeting (2026-09-10) | Job executor created cold; caller waited through faster-whisper import and weight load | `num_idle_processes=1` + `warm_job_executor`; warm build ≈ 2 s |
| Caller heard nothing — audio published before the phone subscribed | No wait for SIP subscription | `wait_for_subscription` + 1.5 s settle |
| Suite mutated the committed hotel database | Tests wrote to the shipped file | `tests/conftest.py` copies it per session |
| Console rendered unstyled; caller card froze | A CSS comment contained `*/` inside `--ink*/--panel`, killing `:root`; `els.factLang` read but never defined | Both fixed 2026-09-19 |

---

## 17. What AETHER DOES NOT Do

- **No automatic language detection.** Deliberate — detection on short narrowband utterances can flip mid-call.
- **No fallback TTS.** Rime unconfigured means silence, not substitution.
- **No partial stale-work salvage.** `ResultSalvaged` is not implemented and deliberately not faked; AETHER holds no partial-result store.
- **No concurrent-call support in the console.** The backend runs one job process per call, but the console is a single global bound to one call (`attach_console` "forgets the previous one") and binds fixed ports, so a second simultaneous call gets no UI. Call *audio* isolation is absolute — separate OS processes. **Two simultaneous callers are untested.**
- **No caller identity.** No phone number, caller name or CRM anywhere. `participant.identity` is logged, never surfaced.
- **No per-call duration from the backend.** The console clock is client-side.
- **STT word accuracy on narrowband telephony audio is unmeasured.**
- **Hindi and Spanish have never run over the telephone** — only the full local pipeline.
- **Hindi/Spanish wording is not fully native-speaker reviewed**; the shipped Hindi and Spanish voices have **not been listened to**.
- **Delivery A/B clips rendered and measured but not listened to.**
- **No ear-to-ear latency figure is claimed** — measurement ends at the first chunk received from Rime.
- **No spoken status answer** for `STATUS_QUERY`; it protects the task silently.
- **`STATUS_QUERY` and `CANCEL` have no trace evidence** — unit-tested only.
- **No mutation testing.** **NOT VERIFIED** — no harness in the repo.
- **No regional Rime endpoint pinned**, no regional comparison.
- **Rime latency varies by day** — English warm first-audio 382–508 ms (2026-09-08) vs 1928 ms median (2026-09-09). Always date the number.

---

## 18. Demo Flow

From `DEMO_SCRIPT.md`; the 14 quoted replies are asserted by `tests/test_demo_script.py`.

| # | Scene / time | User says | Expected AETHER behaviour | Feature demonstrated |
|---|---|---|---|---|
| 0 | 0:00 | *(call connects)* | Greeting, offers Hindi | Telephony, language discoverability |
| 1 | 0:20 | "Can you tell me what's on the menu?" | "We have starters, mains, vegetarian mains, desserts and drinks, with vegetarian, vegan and non-vegetarian options." | Deterministic route, `llm_ms = 0` |
| 2 | | "What does the chicken kebab cost?" | "The Chicken Kebab is four hundred and twenty rupees." | DB price + spoken-form number (never a digit) |
| 3 | | "I'm allergic to nuts, what can I eat?" | Suggests Chicken Kebab, Butter Chicken, Vegetable Biryani; warns one other dish contains nuts | Allergen safety, qualified answer |
| 4 | | "Do you have a swimming pool?" | "Yes, we offer a swimming pool free of charge from six in the morning until eight in the evening." | Services + spoken time format |
| 5 | | "Do you have anything vegetarian?" | Lists 6 dishes "plus two more" | Dietary filter, list truncation |
| 6 | 1:25 | "**Actually**, how much is an executive suite?" *(spoken over AETHER)* | Abandons the in-flight answer, answers the suite: twelve thousand five hundred rupees, sleeps up to three | **`REFINEMENT` interruption + fencing** |
| 7 | 1:40 | "Book it for two nights." | Reserves Executive Suite, room 301, reference one zero zero eight | Pronoun resolution ("it"), mutating tool, authorizer, reference |
| 8 | | "Is room nine nine nine free?" | "We do not have a room nine nine nine. Our rooms are numbered one zero one to five one zero." | Refusal grounded in the DB, not invention |
| 9 | 2:20 | "I'll have the chicken kebab." | "I have added Chicken Kebab. That is Chicken Kebab, four hundred and twenty rupees so far." | Order state begins |
| 10 | | "And two masala chai." | "…two Masala Chai. That is Chicken Kebab and two Masala Chai, seven hundred rupees so far." | Quantity parsing, running total |
| 11 | | "Repeat my order." | "Your order is Chicken Kebab and two Masala Chai. That comes to seven hundred rupees." | Multi-turn order memory |
| 12 | | "That's all, place the order." | "That is with the kitchen… Your order number is five zero zero three." | Mutating write, kitchen status vocabulary |
| 13 | | "What was my booking reference?" | "You have the Executive Suite, room three zero one, for two nights, reference one zero zero eight." | Session booking memory |
| 14 | 2:55 | "When will room three zero five be available?" | "Room three zero five is booked until the nineteenth of September." | Derived availability + spoken date |
| 15 | 3:15 | *(switch to Hindi, then Spanish)* | Same facts, different renderer, different Rime voice/model | Multilingual: one DB, three renderers |

**Point at the console throughout:** `llm_ms = 0` on database turns, and `Stale leaks: 0`.

---

## 19. Top Judge Questions

**Q: Why is this not just a chatbot with voice?**
**30s:** A chatbot has explicit turn boundaries and its output can be deleted before it's read. On a phone, audio cannot be un-spoken, so the hard problem is what happens when the caller interrupts mid-answer. AETHER's whole architecture is built around one invariant — an abandoned answer must never be spoken — enforced at five independent checkpoints, with 0 violations across 106 recorded runs.
**Evidence:** `RULES.md` R1; `aether/spike.py`, `aether/audio/player.py`; `traces/`

**Q: Why not use an LLM for everything?**
**30s:** A row is correct; a model is probable. Prices, allergens and availability must not be probable. Deterministic routing also answers in a median of 0.90 ms versus a 0.9–1.8 s Gemini round trip, and every deterministic turn reports `llm_ms = 0` in the trace, so you can *check* no model was consulted.
**Evidence:** `scripts/measure_understanding.py`; `evidence/demo-run.jsonl` (11 of 16 turns at `llm_ms = 0`)

**Q: Why SQLite?**
**30s:** It gives a statement-level authorizer, which is the mechanism behind the guarantee that nothing can reprice the menu. The read connection is `mode=ro` so a write is refused by the driver, not by convention, and the write connection can only touch five tables plus `rooms.status`. It also gives `BEGIN IMMEDIATE` for the double-booking problem, with no server to run.
**Evidence:** `aether/hotel/bookings.py:157-194`; `tests/test_bookings.py`

**Q: Why deterministic routing rather than an LLM router?**
**30s:** It's inspectable and testable sentence by sentence — 93 routing cases across three languages, all passing, and a guard that no *question* may reach a mutating tool. An LLM router would make booking someone's room a probabilistic event.
**Evidence:** `tests/test_understanding.py`; `scripts/measure_understanding.py` → 93/93

**Q: How is interruption handled?**
**30s:** Onset ducks or fences before any text exists. The utterance is then classified into one of six types — a backchannel like "mm-hm" doesn't interrupt anything, "actually…" is a refinement, "never mind" cancels with no successor task. Anything that fences marks the generation invalid, and five checkpoints downstream discard its work.
**Evidence:** `aether/classify/__init__.py`; `RULES.md` R6

**Q: Why fencing instead of cancellation?**
**30s:** Cancellation races — it tries to stop work already running and cannot un-send audio already on the wire. Fencing is a flag write; in-flight work is allowed to finish and is then refused at every output boundary. A validity check at the door cannot lose a race.
**Evidence:** `aether/supervisor/generations.py`; five `stale_generation_*` reasons in the trace

**Q: Can two people book the same last room?**
**30s:** No. `BEGIN IMMEDIATE` takes SQLite's write lock *before* the room is chosen, and the claim is conditional — the update only succeeds if the room is still free. Belt and braces, plus a thread lock.
**Evidence:** `aether/hotel/bookings.py:220-225`; `tests/test_bookings.py`
*Caveat to state honestly: two simultaneous phone callers are untested end-to-end.*

**Q: Can the model hallucinate prices?**
**30s:** It never sees the question. The router intercepts every price, allergen, policy and availability question before the model, and the answer is a template filled from a row. Even if it did, the authorizer would refuse any write to `menu_items` mid-statement.
**Evidence:** `aether/hotel/router.py`; `aether/hotel/tools.py`; authorizer in `bookings.py`

**Q: How does memory work?**
**30s:** Three kinds. A conversational subject resolves "it". Session state holds the open food order and the last booking. And a booking reference can be looked up across calls straight from the database. Critically, an answer the caller interrupted never enters any of them — history is appended only after the gate confirms audio was actually spoken.
**Evidence:** `aether/hotel/context.py`; `aether/hotel/bookings.py`; `aether/spike.py` (early return on fenced paths)

**Q: What happens when STT mishears?**
**30s:** Near-misses get a "did you mean" repair from a nearest-match over real menu and room names. Unknown order items are named explicitly along with what *is* available, never silently dropped. It has caught us out: `base.en` once heard "dessert" as "Desert" and the model filled the gap — which is why the prompt now teaches by demonstration and the router repairs first.
**Evidence:** `aether/hotel/clarify.py`; `tests/test_clarification.py`

**Q: How do multilingual answers stay consistent?**
**30s:** One database, one router, 33 shared tools — only the final renderer differs per language. A Hindi price and an English price come from the same row, so they cannot drift. Routing is verified at 93 of 93 sentences, 31 per language, and a test enforces that all three are tested equally.
**Evidence:** `aether/hotel/tools_hi.py`, `tools_es.py`; `tests/test_understanding.py`

**Q: Why Rime?**
**30s:** Measured, not preferred. `mistv3` reached first audio 180 ms (37%) sooner than `mistv2`, and connection reuse took one turn's TTS from 2849 ms to 389 ms. We also chose voices from the live catalogue per language rather than assuming — Hindi isn't on `mistv3` at all.
**Evidence:** `RIME_EVIDENCE.md` Part 1; `aether/lang/__init__.py`

**Q: What happens if an API fails?**
**30s:** STT provider selection is explicit — an unknown value raises rather than silently substituting, because a recogniser that isn't the one you configured is invisible until a demo. There is deliberately no fallback TTS: unconfigured means silence, not a substituted voice. Failures surface rather than degrade quietly.
**Evidence:** `aether/stt_groq.py:190`; `RIME_EVIDENCE.md:105`

**Q: What are the limitations?**
**30s:** STT word accuracy on narrowband phone audio is unmeasured. Hindi and Spanish have never run over a phone. The Hindi and Spanish voices haven't been listened to, and the wording isn't fully native-reviewed. Two simultaneous callers are untested. We publish all of that.
**Evidence:** `JUDGING.md`; `RIME_EVIDENCE.md` LIMITATIONS; §17 here

**Q: How would this scale beyond one hotel?**
**30s:** The hotel is entirely data — menu, rooms, policies, services are rows, not code. A second hotel is a second database file. What is *not* multi-tenant today: the console is a single global bound to one call, and there's no tenant routing on the SIP side.
**Evidence:** `data/aether_hotel.db`; `aether/web/server.py`

**Q: What would production deployment require?**
**30s:** Honestly: per-session console state instead of one global binding fixed ports; measured narrowband STT accuracy; native review of Hindi and Spanish; concurrency testing; a pinned Rime region; and an on-call story for Rime day-to-day latency variance, which we've measured swinging from 382 ms to 1928 ms.
**Evidence:** §17; `RIME_EVIDENCE.md`

**Q: Privacy and security concerns?**
**30s:** AETHER refuses to say who is staying in a room, and the refusal tool reads no guest record at all — enforced by inspecting the function's own source in the test suite, not by trusting the author. Credentials use `repr=False` so a traceback can never render one, and the committed call log has the caller's number redacted, asserted by a test.
**Evidence:** `guest_privacy` in `aether/hotel/tools.py`; `tests/test_secret_redaction.py`; `tests/test_docs_are_current.py:174`

**Q: What's your latency?**
**30s:** On a real phone call, median turn latency 1262 ms, with Rime first audio at 280 ms median. Deterministic turns ran 1119–1443 ms; the ones that fell through to Gemini ran 1967–2549 ms — that gap is the argument for the router. We do not claim an ear-to-ear number; measurement ends at the first chunk from Rime.
**Evidence:** `evidence/demo-run.jsonl`, `evidence/demo-call-worker.log`

**Q: What's your testing methodology?**
**30s:** 1970 tests. Pre-registered acceptance scenarios A–H, the brief's full-duplex example at 9/9, per-sentence routing in three languages, and structural tests that read the *source* to prove a function doesn't touch data it shouldn't. Two tests are skipped and both skips are documented as unbuilt features we refused to fake.
**Evidence:** `python -m pytest -q`; `tests/test_acceptance.py`

**Q: How do you know the docs aren't lying?**
**30s:** A test reads them. `test_docs_are_current.py` asks pytest for the real test count and fails any document quoting a stale one — because three files once claimed three different numbers. It also fails dead claims, removed tool names and broken links.
**Evidence:** `tests/test_docs_are_current.py`

**Q: What's novel here?**
**30s:** Not the pipeline — that's standard. It's that the correctness property is *structural and provable*: fencing at five checkpoints, a database authorizer that refuses a repriced menu mid-statement, tools that check validity before their body runs so a stale booking leaves no row, and 0 leaks across 106 recorded runs. Most voice agents promise this; we can show you the trace.
**Evidence:** `traces/`; `RULES.md` R1

**Q: Is Groq answering the hotel questions?**
**30s:** No — Groq is only the speech recogniser. Speech becomes text, the router picks a tool, SQLite returns a row, a template speaks it. Groq never sees a price and never produces an answer.
**Evidence:** `aether/stt_groq.py`; `aether/hotel/router.py`

**Q: Why is one measurement synthetic and another real?**
**30s:** Because we label them. The recogniser comparison is a Rime→Whisper round trip, not human phone speech, and it says so. The duck and kill latencies are synthetic-mode; the live-mic equivalents are still blank in our evidence file rather than filled with the synthetic ones.
**Evidence:** `aether/stt_groq.py:176-188`; `RIME_EVIDENCE.md:546-549`

**Q: What happens on a question the database can't answer?**
**30s:** It goes to Gemini, scope-gated to hotel-and-stay topics, and the answer is stored in a *separate* database so the next caller asking the same thing gets the same answer. That's consistency, not truth — and we keep it in a different file from what the hotel actually charges, on purpose.
**Evidence:** `aether/hotel/learned.py`; `RULES.md` R8b.8; `scripts/review_learned.py`

**Q: Can I see it fail?**
**30s:** Yes — interrupt it mid-answer and watch the console. The abandoned answer appears struck through and marked DISCARDED with its generation id, never as something spoken. That's the fence working, visible in real time and in the trace afterwards.
**Evidence:** live console; `ResultDiscarded` events (4136 across the traces)

---

## 20. Architecture Components — Quick Lookup

| Component | File/Module | Responsibility |
|---|---|---|
| LiveKit worker entrypoint | `aether/telephony/agent.py` | One inbound call → one pipeline; only module importing the SDK |
| Telephony config | `aether/telephony/__init__.py` | `LiveKitConfig` from env; 48 kHz transport constants |
| Frame conversion | `aether/telephony/frames.py` | `rtc.AudioFrame` ⇄ `AudioChunk`; no LiveKit import |
| Audio bridges | `aether/bridge/__init__.py` | Inbound/outbound pumps |
| Playback gate | `aether/audio/player.py` | Duck, stop, queue clear, final fence check, output gain |
| VAD | `aether/audio/vad.py` | Speech onset/offset |
| TTS client | `aether/audio/rime_ws.py`, `rime.py` | Rime `/ws3`; speaker/model/lang in the URL |
| STT | `aether/stt.py`, `aether/stt_groq.py` | Provider selection; local and Groq |
| Interruption classifier | `aether/classify/__init__.py` | 6 classes, closed sets + prefixes |
| Generation registry | `aether/supervisor/generations.py` | Allocate, `mark_fenced` |
| Barge-in coordination | `aether/interruption/__init__.py` | `fence_now`, duck/stop policy |
| Turn orchestration | `aether/spike.py` | The turn loop; 3 of the 5 fence checkpoints |
| Tool runner | `aether/tools/__init__.py` | Validity check *before* the tool body (`STALE_TOOL`) |
| Router | `aether/hotel/router.py` | Sentence → tool + args, 3 languages |
| Tools + English renderer | `aether/hotel/tools.py` | `HOTEL_TOOLS` (33), `SPEAK` templates |
| Hindi / Spanish renderers | `aether/hotel/tools_hi.py`, `tools_es.py` | Per-language templates |
| Number/time speech | `aether/hotel/speech_hi.py`, `speech_es.py` | Spoken forms |
| Read-only DB access | `aether/hotel/db.py` | `HotelStore`, `mode=ro` |
| Writes + authorizer | `aether/hotel/bookings.py` | The only writable connection |
| Clarification | `aether/hotel/clarify.py` | `nearest()` did-you-mean |
| Conversational subject | `aether/hotel/context.py` | Pronoun resolution |
| Learned answers | `aether/hotel/learned.py` | Separate DB + its own authorizer |
| Language records | `aether/lang/__init__.py` | The three `Language` records |
| LLM providers | `aether/llm.py` | Gemini + 3 alternatives behind one interface |
| Config from env | `aether/config.py` | `repr=False` on every secret |
| Tracing | `aether/trace.py`, `aether/events.py` | JSONL events, `GenerationStatus` |
| Console server | `aether/web/server.py` | HTTP + WebSocket, snapshot fold |
| Console UI | `aether/web/static/index.html` | Single static file, vanilla JS |

---

## 21. Commands

```bash
# Setup
python -m venv .venv                       # then activate
python -m pip install --upgrade pip
python -m aether.prewarm                   # warm the Whisper model before a demo

# The whole claim, end to end — no phone, no microphone
python scripts/demo_full_call.py           # offline / free
python scripts/demo_full_call.py --mode live   # real Gemini and Rime

# Tests
python -m pytest -q                                      # 1970 passed, 2 skipped
python -m pytest tests/test_full_duplex_acceptance.py -q # the brief's example, 9 checks
python -m pytest tests/test_acceptance.py -q             # scenarios A-H, pre-registered

# Live call
python scripts/run_call.py                 # start the worker
python -m aether.web                       # console at http://127.0.0.1:8760

# Database
python scripts/reset_hotel_db.py           # reset to shipped state
python scripts/dump_hotel_db.py            # inspect contents

# Measurement
python scripts/measure_understanding.py    # multilingual routing: 93/93 + route latency
python scripts/measure_audio_kill.py       # interrupt -> silence latency
python scripts/measure_playback_latency.py # enqueue -> device callback
python scripts/measure_tts_first_audio.py  # Rime first audio
python scripts/compare_recognisers.py      # STT comparison (synthetic round trip)
python scripts/verify_rime_hindi.py        # live Rime, all languages
python scripts/render_delivery_variants.py # spoken-form A/B clips

# Learned answers
python scripts/review_learned.py                        # most-asked first
python scripts/review_learned.py --confirm "<question>"
python scripts/review_learned.py --forget  "<question>"
```

---

## 22. Claim → Evidence Map

| Claim | Evidence | Exact file / test / trace |
|---|---|---|
| Stale answers never reach the caller | 0 `ResultLeaked` across every recorded run | `traces/` (106 `.jsonl`); `tests/test_acceptance.py` scenario A |
| Fencing is enforced at five independent points | Five distinct discard reasons | `spike.py:980`, `:1038`, `:1195`; `tools/__init__.py:44`; `player.py:439` |
| A stale booking leaves no row | Validity checked before the tool body | `aether/tools/__init__.py`; `tests/test_bookings.py` |
| Hotel facts never come from the LLM | `llm_ms = 0` on database turns | `evidence/demo-run.jsonl` — 11 of 16 turns |
| Nothing can reprice the menu | `sqlite3` authorizer denies it mid-statement | `aether/hotel/bookings.py:157-194`; `tests/test_bookings.py` |
| Two callers cannot take the same last room | `BEGIN IMMEDIATE` + conditional claim | `aether/hotel/bookings.py:220-225` |
| The phone path works end to end | Trace and worker log match turn for turn | `evidence/demo-run.jsonl` + `evidence/demo-call-worker.log`; `tests/test_docs_are_current.py:146` |
| One track → one pump | `pumps=1`, `frames_failed=0` | `evidence/demo-call-worker.log` |
| Three languages are equally capable | 93/93 routed; equality asserted | `scripts/measure_understanding.py`; `tests/test_understanding.py` |
| Questions never reach a mutating tool | Per-language guard | `tests/test_understanding.py::test_no_question_is_answered_by_a_mutating_tool_unless_it_asks_for_one` |
| AETHER never reveals who is staying | The tool reads no record; proved by source inspection | `guest_privacy`; structural test in the hotel test suite |
| No credential can reach a log or trace | `repr=False`; redaction asserted | `aether/config.py`; `tests/test_secret_redaction.py`; `tests/test_docs_are_current.py:174` |
| The demo script is not aspirational | Every quoted reply is asserted | `tests/test_demo_script.py`; `DEMO_SCRIPT.md:314-327` |
| The documents are not stale | A test reads them and asks pytest for the real count | `tests/test_docs_are_current.py` |
| `mistv3` was chosen on measurement | 180 ms / 37% faster to first audio | `RIME_EVIDENCE.md` Part 1 |
| Groq was chosen on measurement | Hindi 89.2% vs 21.7% local (synthetic) | `aether/stt_groq.py:180-188` |
| The suite cannot corrupt the shipped DB | Per-session copy | `tests/conftest.py` |

---

## 23. Pitches

**15 seconds.**
AETHER is a multilingual hotel voice agent that answers phone calls in English, Hindi and Spanish. Every hotel fact comes from a database, never from a language model — and when a caller interrupts, the abandoned answer is structurally prevented from ever being spoken. Zero stale answers across 106 recorded runs.

**30 seconds.**
AETHER answers a hotel's phone in three languages. The hard part isn't the pipeline — it's what happens when a caller talks over the agent. Work is already in flight: a model streaming, a voice synthesising, a booking about to write. AETHER gives every turn a generation id and fences it at five independent checkpoints, so an abandoned answer is never spoken, never remembered, and never writes a row. Hotel facts bypass the model entirely — the router hits SQLite and a template speaks the row, in 0.9 milliseconds, with `llm_ms = 0` visible in the trace. 1970 tests, and zero stale outputs across every run we've ever recorded.

**60 seconds.**
Hotels lose calls at night and in peak hours, and callers want things that are already in a database: prices, availability, policies, food orders, bookings. AETHER answers the phone in English, Hindi and Spanish and gets those from SQLite — the router matches the sentence, a tool reads the row, a per-language template speaks it. The model is never asked, so a price cannot be hallucinated, and a deterministic turn reports `llm_ms = 0` so you can verify that rather than trust it.
The engineering problem is interruption. On a phone, audio can't be un-spoken, and when a caller cuts in there's work already running. AETHER classifies the interruption into six types — a "mm-hm" doesn't interrupt, "actually…" refines, "never mind" cancels — and anything that fences marks the generation invalid. Five independent checkpoints then discard its work, including one *before* a tool body runs, so a booking the caller changed their mind about never lands.
The guarantees are structural, not promised: SQLite's authorizer refuses any write to a price mid-statement; `BEGIN IMMEDIATE` means two callers can't take the last room. 1970 tests, 93 of 93 routing cases across three languages, a real recorded phone call at 1262 ms median turn latency — and zero stale answers across 106 runs.
What we don't claim: word accuracy on narrowband audio is unmeasured, Hindi and Spanish have never run over a real phone, and two simultaneous callers are untested.

---

## 24. Glossary

| Term | Definition |
|---|---|
| **Generation** | One attempt to answer one turn, identified by an id (`G7`). All work for that turn carries it |
| **Generation id** | The handle joining STT, routing, tool, LLM, TTS and audio for one turn; visible in the trace and the console |
| **Fence** | Marking a generation invalid. A flag write — it stops nothing, but every output boundary then refuses that generation's work |
| **Stale generation** | A generation that was fenced while its work was still in flight |
| **Cancellation** | Trying to *stop* running work. Racy; AETHER does not rely on it |
| **Router** | Deterministic keyword/regex mapping from a caller's sentence to a tool and arguments. No model |
| **Tool** | A Python function that reads (25) or writes (8) the hotel database. Mutating tools are fenced before their body runs |
| **Renderer** | Per-language template turning a database row into a spoken sentence (`tools.py`, `tools_hi.py`, `tools_es.py`) |
| **Spoken form** | Text shaped for the ear: "four hundred and twenty rupees", not "420"; "Room three oh five", not "305" |
| **Session** | One call's state: conversational subject, open food order, last booking |
| **Barge-in** | The caller speaking while AETHER is speaking |
| **Duck** | Immediate, reversible gain reduction on speech onset, before understanding exists. Not used on the phone path |
| **Stop** | Confirmed, irreversible halt: queue cleared, audio cut |
| **Deterministic path** | Router → tool → database → template. No model, `llm_ms = 0` |
| **Learned answer** | A model answer stored in a separate database and replayed for the same question. Consistency, explicitly not truth |
| **Authorizer** | SQLite callback that approves or denies each statement's table/column access at the driver level |
| **ResultDiscarded** | Trace event: a fenced generation's work was refused at an output boundary. 4136 recorded |
| **ResultLeaked** | Trace event: a stale result *did* reach output. **0 recorded, ever.** One would fail the run |
| **Conversational subject** | The last thing actually spoken about, used to resolve "it". A fenced answer can never become it |

---

*Verified against the working tree on 2026-09-19. Test counts, tool counts, trace counts, routing accuracy and route latency are derived by `scripts/metrics.py` rather than copied, and `tests/test_reference_integrity.py` fails the suite when any document disagrees with it. Three disagreements were found that way and corrected at the source: a stale trace count in RIME_EVIDENCE.md and README.md, and a fence-checkpoint count that said four in README.md and JUDGING.md where the source has five.*
