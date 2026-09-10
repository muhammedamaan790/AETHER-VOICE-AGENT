# AETHER against the judging criteria

Every claim here names the file, test or trace that backs it. Where something is not built or not
measured, it says so — a rubric that rewards transparent method punishes overclaiming, and the
"What we did not build" section at the end is not an afterthought.

**Reproduce everything:** `python -m pytest -q` → **1392 passed, 2 skipped**.

---

## Problem, and why it has to be voice — 25%

**AETHER answers a hotel's telephone as its duty manager.**

A phone call has no screen. The caller cannot tap a menu, read a price list, scroll back, or install
anything; they are on a handset, often mid-task. There is no fallback surface to degrade to, so
speech is not a nicer way to reach this product — it is the only way. Remove it and there is no
product, not a worse one.

That is also what makes the engineering problem unavoidable rather than academic. On a phone people
interrupt, change their mind mid-sentence, and talk over the answer — and there is no screen still
showing the previous answer to recover from when they do.

| | |
|---|---|
| User and context | [`PRD.md`](PRD.md) §1–§2 |
| The product | [`README.md`](README.md) |
| What a real call sounds like | [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) — 12 turns, every answer quoted from the running system |

## Hard voice engineering — 25%

**The golden invariant: a stale result must never become spoken output.** Interruption is not
treated as one event. Six classes are distinguished deterministically — refinement, replacement,
status query, cancel, backchannel, new task — and each transitions the supervisor differently. Say
"mm-hm" while AETHER is speaking and it keeps talking; talk over it and the in-flight answer is
fenced and never spoken.

Fencing is not cancellation. A generation is `active` or `fenced`, monotonically; every audio chunk
carries its generation, and four independent layers check it, so an answer computed for a
superseded turn cannot reach the speaker even if it completes.

| Challenge | Where | Evidence |
|---|---|---|
| Interruption semantics, six classes | `aether/classify/`, `aether/interruption/` | `tests/test_classifier.py`, `tests/test_interruption_coordinator.py` |
| Generation fencing, four layers | `aether/supervisor/generations.py`, `aether/audio/player.py` | `tests/test_acceptance.py` A–H |
| A fenced **mutation** never lands | `aether/tools/` | `tests/test_warehouse_tools.py:185` — needs a mutable store, which is why `aether/warehouse/` still exists |
| Telephony under real line conditions | `aether/bridge/`, `aether/telephony/` | [`RIME_EVIDENCE.md`](RIME_EVIDENCE.md) Part 6 |
| Endpointing and speech floor, calibrated from real calls | `aether/audio/vad.py` | Part 6 — floor moved 35 → **2500** on 28 pooled utterances |
| Surviving recogniser error | `aether/hotel/router.py` | `tests/test_hotel_db.py`, and below |
| **Multilingual**: caller picks English/Hindi/Spanish before the greeting, switches mid-call | `aether/lang/`, `aether/hotel/tools_hi.py`, `tools_es.py` | `tests/test_language.py` |

**Two voice-specific defects, found in traces rather than imagined.** `suite` is pronounced "sweet",
so `base.en` returns `suit` or `sweet` and the room-type match missed entirely — one real turn spent
1360 ms in the model answering what the database answers for nothing. And `"do you have room
service"`, misheard as `"Do you have room for this?"`, was being answered with the list of room
types: a confident answer to a question nobody asked. Both are fixed and tested. Replaying all 218
distinct utterances in `traces/`: **+2 answered deterministically, −1 wrong answer, 0 rerouted.**

**Multilingual routing, and it does not cost the guarantee.** AETHER answers a hotel in India, so
it speaks Hindi -- and Spanish. **The call opens by asking which language**, before any hotel
greeting: the greeting has to be spoken in some language, so greeting first would already have
chosen for the caller. Once they answer, voice, recogniser, renderer and the model's own
instructions all change together, and stay changed until they ask to switch. Crucially the Hindi answers are **still
templates over the same database rows** -- `llm_ms` stays 0 -- because handing a price to a model
to phrase in Hindi would be handing it the chance to get it wrong in a language fewer people in the
room can check.

Nothing here was assumed. Rime's public catalogue says `mistv3` speaks eng/fra/ger/spa and that
`astra` speaks no Hindi on any model, so Hindi is a **different model and a different voice**, and
`speaker`/`modelId`/`lang` are baked into the `/ws3` URL so it is also a second socket. The voice
was picked by measurement over `/ws3`. The first choice, `arcana/anaya`, had to be abandoned:
**Rime deleted the entire `arcana` model between 2026-09-08 and 2026-09-09** (863 catalogue entries
down to 594), taking all three of its Hindi voices with it. It still answered after being delisted,
which is precisely the trap this project refuses -- an undocumented endpoint that merely happens to
respond. Hindi is now `coda/nadi`, which is catalogued. Devanagari over romanised was decided by
listening to both, not by preference.

The honest cost: **Hindi is roughly 3x slower to first audio than English** (~1.5 s against
~0.4 s). It is recorded rather than hidden, and it is why the switch is opt-in -- an English call
never opens the Hindi socket and never pays for it.

**Latency is the reason the model is not in the path** for a fact the hotel holds. Those are read
from SQLite and rendered by template — worth ~1.8 s per turn, and it makes it *impossible* for the
model to quote a price the hotel does not charge, because it is never asked.

For anything the hotel has no record of the model **is** used, and is expected to answer plausibly
as the duty manager rather than refuse (RULES.md R8b.3). The one carve-out is allergens and dietary
status, which are never guessed — a wrong opening time is corrected next call, a wrong allergen
answer is not.

## Rime integration and voice experience — 20%

Rime is the only voice. There is no fallback TTS: unconfigured means silence, not substitution.

| | | |
|---|---|---|
| Model | `mistv3` | chosen by measurement against `mistv2` — [Part 1a](RIME_EVIDENCE.md) |
| Voice | `astra` | verified against the live catalog |
| Language | `eng`, and `hin` on request | a second voice, not a parameter: see multilingual above |
| Transport | **WebSocket `/ws3`**, persistent | streaming, and stoppable mid-utterance (`{"operation":"clear"}`) — which is what makes barge-in feel instant |
| Endpoint | `wss://users-ws.rime.ai/ws3` | the HTTP endpoint `https://users.rime.ai/v1/rime-tts` is kept as a transport fallback (`RIME_TRANSPORT=http`), still Rime |
| Audio format | `audioFormat=pcm` at **48 kHz**, the AudioGate's own rate | no MP3 decode and no resample — [Part 1b](RIME_EVIDENCE.md) |
| **First audio** | **280 ms median** (270–346) | [`evidence/demo-run.jsonl`](evidence/) |

All four values are configuration, never literals. The API key is read from `.env` and never
printed, committed or written to a trace.

**Output is built to be spoken.** Rime is never handed a digit, a symbol or markdown: prices become
"four hundred and twenty rupees", times become "two in the afternoon", and a room number is said as
a door — "three oh five", not "three hundred and five". Asserted in `tests/test_hotel_db.py` and
`tests/test_demo_script.py`.

## Evidence and reproducibility — 20%

**1394 automated tests — 1392 passing, 2 skipped** — and both skips are deliberate, documented in the test body, and
refuse to fake a result: `AETHER_UNSAFE_MODE` has no bypass path to exercise, and `ResultSalvaged`
is not implemented and is not emitted to look like evidence.

| Claim | Backed by |
|---|---|
| Latency, fencing and no-leak **on a real phone call** | [`evidence/demo-run.jsonl`](evidence/) + [`demo-call-worker.log`](evidence/) — same 16 turns in both, committed, secret-audited |
| One track produces one pump; a second is refused | `demo-call-worker.log`: `pumps=1`, and the log line refusing the second subscription |
| Telephony audio levels | [Part 6](RIME_EVIDENCE.md) — 28 utterances, 3 calls |
| Rime model choice | [Part 1a](RIME_EVIDENCE.md) — both renders measured, both kept |
| The demo script says what AETHER says | `tests/test_demo_script.py` — parses `DEMO_SCRIPT.md` and re-runs every line against the database |
| Hotel answers match the database | `tests/test_hotel_db.py` — expectations read from SQLite, not written down |
| Hindi says the same facts as English | `tests/test_language.py` — same rows, both renderers, no model in either |
| Which Hindi voice, and why | `scripts/verify_rime_hindi.py` — four voices measured over `/ws3` |

**The evidence file records being wrong.** A speech-floor claim made from one call was contradicted
by two later calls; the original is struck through and left in place rather than deleted. A
`diagnose()` verdict of `OK` on a call that had failed is written up as the defect it was.

## Demo clarity — 10%

[`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) is the call word for word — twelve turns, AETHER's exact replies,
the one barge-in beat worth rehearsing, and a bank of verified fallback questions. It is
machine-checked, because the previous runbook silently went stale and would have been read aloud on
camera with a price the database contradicted.

[`DEMO.md`](DEMO.md) covers setup, the console and the failure paths.

---

## Differentiation review — against the real catalog (2026-09-10)

The brief makes this a precondition: *"review the Voice AI with Rime project catalog … do not
submit a close reproduction. Extend an idea, combine approaches, improve the voice experience, or
solve a harder problem."*

**The catalog is [github.com/rimelabs/rime-dev-projects](https://github.com/rimelabs/rime-dev-projects)**
— 16 projects, read from `data/projects.json`, the repository's own machine-readable list. Fourteen
of the sixteen have a public source repository; **Continuum** and **NOVA** return 404, so those two
are assessed from their catalog summary alone and could be closer to AETHER than this review can
see. Continuum in particular — *"a multilingual commerce agent that … uses the caller's words to
resolve interruptions"* — is the nearest by description of anything here, and its code is not
readable.

> **An earlier draft of this section reviewed the wrong list.** The catalog link in the brief is a
> hyperlink whose target does not survive text extraction from the PDF, so a previous pass reviewed
> the AssemblyAI × LiveKit × Rime showcase as the closest reachable equivalent and said so. That was
> a different set of 13 projects. The findings below replace it entirely, and they are less
> flattering to AETHER — which is why the old ones are gone rather than kept alongside.

### What the catalog actually contains

Every one of the fourteen readable READMEs was scanned for the claims AETHER makes:

| Concern | Projects that raise it | Of 14 readable |
|---|---|---|
| Interruption / barge-in | EIRA, Jan Vaani, AI4 Booth Edition, Roger AI, RoutineCraft AI, Saathi, Vaani, Client playbook | **8** |
| Multilingual | AARVI, Jan Vaani, Nuvia, Saathi, Vaani, WageLens | **6** |
| A measured latency figure | EIRA, FieldMate, Roger AI, RoutineCraft AI, VIRA | **5** |
| A test suite | FieldMate, Jan Vaani, Saathi, Client playbook | **4** |
| Telephony (SIP / Twilio / PSTN) | Jan Vaani, Saathi | **2** |
| **Stale work discarded by fencing** | **FieldMate** | **1** |

**So the earlier claim that "not one addresses interruption, telephony, or multilingual routing" was
simply wrong, and is withdrawn.** Interruption is the single most common theme in this catalog.

### The three closest projects, and where AETHER actually differs

**FieldMate** is the closest by mechanism, and it is close. It states: *"Turn/generation fencing
prevents stale asynchronous retrieval tasks from corrupting current state"*, backed by immutable
domain events, atomic rollback, a pytest suite, and a dual-track router that discards a speculative
generation when grounded evidence arrives. That is the same idea as AETHER's generation fencing,
arrived at independently.

Where AETHER differs is **what the guarantee protects**. FieldMate fences *state* — a stale
retrieval must not corrupt the diagnostic record. AETHER fences *speech* — a stale result must never
become spoken output — and enforces it down into the audio layer, where duck and stop are separate
actions and every audio chunk carries the generation it belongs to. Those are adjacent problems, not
the same one: FieldMate's README makes no claim about audio already queued for playback, which is
the case AETHER's `AudioGate` exists for. FieldMate is also WebRTC and browser-based, monolingual,
and pairs voice with camera vision — three things AETHER is not.

**FieldMate is faster.** ~600 ms warm end-to-end including STT, against AETHER's ~1.26 s
(927 ms STT + 337 ms to first audio). AETHER does not claim to be the low-latency entry here, and
FieldMate's speculative prefetch and semantic cache are techniques AETHER does not implement.

**Saathi** is the closest on transport and language: real outbound Twilio calls, Hindi and English,
Rime **Coda / `nadi`** — the same model and voice AETHER uses for Hindi. Its "interruption" is a
conversational state, though: *"an interruption, correction, or 'call me later' is stored as
`deferred`"* and resumed on a later call. Twilio's `<Play>`/`<Gather>` is turn-based by
construction, so barge-in during playback is not the problem it is solving. AETHER runs over
LiveKit SIP with the microphone open throughout, which is a different transport choice with a
different failure mode.

**RoutineCraft AI** is the closest on barge-in mechanics, and makes the sharpest claim in the
catalog: clause-based streaming in 4–6 word chunks with *"Barge-In Latency < 60 ms — instantly halts
voice playback when the user speaks."* That is a playback claim. It is a good one, and AETHER's
own duck is in the same range. The difference is again scope: halting playback is not the same as
guaranteeing that the work already in flight cannot be spoken when it completes.

### What AETHER contributes that this catalog does not contain

Stated narrowly, because the honest version is narrow:

- **The invariant is about speech, not state**, and it is checked at four independent layers
  including the audio gate — so an answer computed for a superseded turn cannot reach the speaker
  even if it completes. No readable project in the catalog makes a claim about queued audio.
- **Interruption is not one event.** Six classes — backchannel, refinement, replacement, status
  query, cancel, new task — are distinguished deterministically and transition the supervisor
  differently. Say "mm-hm" and AETHER keeps talking; talk over it and the in-flight answer is
  fenced. No project here classifies interruption types; the eight that handle interruption treat
  it as one thing.
- **Conversation state is committed only at the spoken boundary**, so the agent never "remembers"
  an answer the caller did not hear.
- **Tool results are fenced too, not just model output** — including a fenced mutation that must
  never reach the store.
- **All of it over a real inbound telephone call, in three languages**, with the trace and the SIP
  worker log for the same run committed as evidence.

### What is NOT claimed

- Not that AETHER is the only project doing fencing. **FieldMate does it too**, and says so.
- Not that it is the fastest. It is not.
- Not that interruption, telephony or multilingual support are unusual here. Interruption is the
  most common theme in the catalog and multilingual is close behind.
- Not that the hotel domain is novel; it is a concrete setting for the voice problem.
- Not that the two unreadable projects were assessed. They were not, and one of them is the nearest
  match by description.

**Sources:** [catalog](https://github.com/rimelabs/rime-dev-projects) ·
[`data/projects.json`](https://github.com/rimelabs/rime-dev-projects/blob/main/data/projects.json) ·
each project's own README, fetched 2026-09-10.

## What we did not build, and what is not measured

- **Salvage / partial-result reuse.** Not implemented. `ResultSalvaged` is never emitted, and the
  acceptance scenario that would report it is skipped rather than faked.
- **`AETHER_UNSAFE_MODE`.** Parsed, honoured nowhere. There is no code path that lets a stale result
  reach output, so the control condition cannot be run.
- **The LLM fallback's language adherence had a root cause, now fixed.** It looked like a model
  limitation -- Spanish answering in English 3 of 5 times -- and it was a missing assignment:
  `build_llm()` returns a `RetryingLLM` wrapper, and the active language was being set on the
  wrapper while every adapter read it from itself. No directive reached a single live call. Hindi
  masked it, because a Devanagari question elicits a Hindi answer regardless. After forwarding the
  attribute: **12/12 across English, Hindi and Spanish** on four questions each, measured. Pinned by
  `tests/test_language.py::test_the_active_language_reaches_the_provider_through_the_retry_wrapper`.
- **Acoustic echo cancellation.** Not implemented. On the local path, wear headphones.
- **STT word accuracy on narrowband audio.** Not separately measured.
- **The input path is now IN the trace, but only for runs recorded from now on.** A trace records
  `input_path` -- `telephony` or `local_microphone` -- stamped once by whichever entry point owns
  the session (`tests/test_trace_input_path.py`). `evidence/demo-run.jsonl` predates the field and
  was deliberately **not** back-filled: writing a true value into an old file would still be
  claiming the run observed something it never observed. That call's path is settled by a second
  artifact instead -- `evidence/demo-call-worker.log` is the LiveKit worker's log for the same run,
  and all sixteen turns carry identical latencies in both files. So: new traces are self-attributing;
  the committed demo evidence needs its worker log, and has it.
  `input_path` is deliberately never `browser`: the web console is a viewer over the same
  local-microphone session the CLI runs, and no audio travels from the browser.
- **Concurrency.** One caller at a time is the tested case.
- **Reservations are read-only.** Nothing can be booked, cancelled or changed; the database is
  opened `mode=ro`, so a write is refused by SQLite rather than by convention.
