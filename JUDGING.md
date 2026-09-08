# AETHER against the judging criteria

Every claim here names the file, test or trace that backs it. Where something is not built or not
measured, it says so — a rubric that rewards transparent method punishes overclaiming, and the
"What we did not build" section at the end is not an afterthought.

**Reproduce everything:** `python -m pytest -q` → **945 passed, 2 skipped**.

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
| **Multilingual routing**, English ↔ Hindi mid-call | `aether/lang/`, `aether/hotel/tools_hi.py` | `tests/test_language.py` (51 tests) |

**Two voice-specific defects, found in traces rather than imagined.** `suite` is pronounced "sweet",
so `base.en` returns `suit` or `sweet` and the room-type match missed entirely — one real turn spent
1360 ms in the model answering what the database answers for nothing. And `"do you have room
service"`, misheard as `"Do you have room for this?"`, was being answered with the list of room
types: a confident answer to a question nobody asked. Both are fixed and tested. Replaying all 218
distinct utterances in `traces/`: **+2 answered deterministically, −1 wrong answer, 0 rerouted.**

**Multilingual routing, and it does not cost the guarantee.** AETHER answers a hotel in India, so
it speaks Hindi. The greeting offers it (*"For Hindi, just say Hindi"*), the caller asks mid-call,
and voice, recogniser and templates all change together. Crucially the Hindi answers are **still
templates over the same database rows** -- `llm_ms` stays 0 -- because handing a price to a model
to phrase in Hindi would be handing it the chance to get it wrong in a language fewer people in the
room can check.

Nothing here was assumed. Rime's public catalogue says `mistv3` speaks eng/fra/ger/spa and that
`astra` speaks no Hindi on any model, so Hindi is a **different model and a different voice**, and
`speaker`/`modelId`/`lang` are baked into the `/ws3` URL so it is also a second socket. The voice
was picked by measurement (warm first audio, `/ws3`): `arcana/anaya` 1355-1716 ms, `coda/taru`
1585-2044, `coda/nadi` 1861-1910, `arcana/arya` 1948-2764. Devanagari over romanised was decided by
listening to both, not by preference.

The honest cost: **Hindi is roughly 3x slower to first audio than English** (~1.5 s against
~0.4 s). It is recorded rather than hidden, and it is why the switch is opt-in -- an English call
never opens the Hindi socket and never pays for it.

**Latency is the reason the model is not in the path.** Hotel facts are read from SQLite and
rendered by template, never phrased by an LLM — worth ~1.8 s per turn, and it removes the chance of
a model saying a price the hotel does not charge.

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

**947 automated tests — 945 passing, 2 skipped** — and both skips are deliberate, documented in the test body, and
refuse to fake a result: `AETHER_UNSAFE_MODE` has no bypass path to exercise, and `ResultSalvaged`
is not implemented and is not emitted to look like evidence.

| Claim | Backed by |
|---|---|
| Latency, fencing and no-leak on a real run | [`evidence/demo-run.jsonl`](evidence/) — committed, secret-audited |
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

## What we did not build, and what is not measured

- **Salvage / partial-result reuse.** Not implemented. `ResultSalvaged` is never emitted, and the
  acceptance scenario that would report it is skipped rather than faked.
- **`AETHER_UNSAFE_MODE`.** Parsed, honoured nowhere. There is no code path that lets a stale result
  reach output, so the control condition cannot be run.
- **Acoustic echo cancellation.** Not implemented. On the local path, wear headphones.
- **STT word accuracy on narrowband audio.** Not separately measured.
- **The input path is not in the trace.** A trace does not record whether its audio came from the
  telephone or the local microphone, and it cannot be inferred afterwards. So
  `evidence/demo-run.jsonl` is quoted as a property of the pipeline, not of the telephone. This is a
  real gap in the evidence model and is stated rather than glossed.
- **Concurrency.** One caller at a time is the tested case.
- **Reservations are read-only.** Nothing can be booked, cancelled or changed; the database is
  opened `mode=ro`, so a write is refused by SQLite rather than by convention.
