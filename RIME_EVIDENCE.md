# AETHER — Rime + Acceptance Evidence

Created Day 1. Acceptance tests are pre-registered here **before** any demo run. Result fields stay
blank until an actual execution fills them in. Nothing in this file may be filled in from
expectation — see [RULES.md](RULES.md) R10.

---

## Summary — claim, test, procedure, result, limitations

*The five things the submission checklist asks this file to contain. Everything below this section
is the detail behind them.*

**THE HARD VOICE CLAIM.** *A stale result never becomes spoken output.* When a caller interrupts,
AETHER stops queued Rime audio, fences the obsolete generation, and discards any model or tool
result belonging to it — so the abandoned answer is never spoken, never remembered, and cannot
re-enter the conversation. The application keeps accepting the caller's audio throughout, including
while Rime is speaking and while a tool is running.

**ACCEPTANCE TEST**, defined before the demo. The brief's own full-duplex example: inject a fixed
delay into a tool call, interrupt mid-lookup, change one part of the request, and verify five
things — queued audio stops, the updated instruction lands, stale results are not spoken, background
work is cancelled, and the final spoken answer is the answer to the *revised* question. Plus the
eight pre-registered scenarios A–H in Part 3, and a normal uninterrupted control.

**PROCEDURE** — repeatable, no phone required:

```bash
python scripts/demo_full_call.py                           # ONE command: the whole claim, end to end
python -m pytest tests/test_full_duplex_acceptance.py -q   # the brief's example, 9 checks
python -m pytest tests/test_acceptance.py -q               # scenarios A-H, pre-registered
python -m pytest -q                                        # everything
python scripts/verify_rime_hindi.py                        # live Rime, all languages
python scripts/render_delivery_variants.py                 # delivery A/B clips (Part 1d)
python scripts/measure_audio_kill.py                       # interrupt -> silence latency
```

**RESULT.**

| | |
|---|---|
| Full-duplex acceptance (the brief's example) | **9/9 pass** |
| Whole suite | **1446 passed, 2 skipped** (both skips are unbuilt features, not failures) |
| `ResultLeaked` across **84** trace files | **0** |
| Committed real phone call | 16 turns; **11 answered with `llm_ms = 0`**; 2 generations fenced; 0 leaks |
| Turn latency, that call | median **1262 ms** |
| Rime first audio, that call | median **280 ms** |
| One track → one pump, second refused | `pumps=1`, `frames_failed=0` (`evidence/demo-call-worker.log`) |

**LIMITATIONS.** Stated because they are the part that is easy to leave out:

- The measured path ends at the first chunk **received from Rime**. Gate queue and device playback
  are outside it — now measured separately (see *Playback latency*): **warm median 9.2 ms** from
  enqueue to the device callback, plus a **22 ms** device buffer that is reported and never added.
  So the 1262 ms above is time-to-first-chunk, not time-to-first-sound, and AETHER claims no
  ear-to-ear figure anywhere.
- **STT word accuracy on narrowband telephony audio is not measured.**
- Hindi and Spanish have **never been exercised over the telephone** — only through the full local
  pipeline. Browser-quality results do not prove telephone performance.
- The Hindi and Spanish wording has **not been fully reviewed by a native speaker**. One review has
  happened and it found a real defect: room numbers were being said digit by digit in all three
  languages, because the English convention was copied into the other two. Hindi says a room number
  as a cardinal -- "एक सौ एक", not "एक शून्य एक", which reads as a phone number. Fixed 2026-09-10.
  **The Spanish equivalent was changed by analogy, NOT on a native speaker's correction**, and a
  Spanish speaker still needs to hear it. Everything else in both languages remains unreviewed, and
  this correction is evidence that the gap is worth closing rather than evidence that it is small.
- **The shipped Hindi voice has not been listened to.** Hindi was chosen on `arcana`/`anaya`, and
  Rime then deleted the whole `arcana` model (Part 1c). The replacement, `coda`/`nadi`, is
  catalogued and measured but the migration was made on catalogue and latency evidence, not on a
  listening judgement. Same for Spanish (`mistv3`/`isa`).
- The delivery A/B clips in Part 1d have been **rendered and measured but not yet listened to**, so
  the spoken-form rules remain a reasoned choice with evidence available, not a listening result.
- Rime latency varies by day: English warm first-audio measured 382–508 ms on 2026-09-08 and a
  1928 ms median on 2026-09-09. Numbers are dated wherever they are quoted.
- No regional endpoint is pinned, and no regional comparison has been run.
- Concurrency (two simultaneous callers) is untested.

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
- [x] Record the date of verification and re-check before the demo — **re-checked 2026-09-10**

**Final pre-submission re-check (2026-09-10).** All three shipped combinations were queried against
the live catalogue again, immediately before finalising. 594 entries — the same count as after Rime
deleted `arcana`, so nothing moved between 09-09 and 09-10:

| Shipped as | `modelId` / voice | Catalogue says | Present |
|---|---|---|---|
| English | `mistv3` / `astra` | `eng` | yes |
| Hindi | `coda` / `nadi` | `hin` | yes |
| Spanish | `mistv3` / `isa` | `spa` | yes |

Each voice's catalogued language matches the `lang` AETHER sends for it. This check is cheap and
must be repeated before any live demo: the `arcana` deletion showed the catalogue can change
overnight, and a delisted voice keeps answering for a while, so "it still works" is not evidence
that it is still supported.

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

### Part 1c — Hindi, and why it is a different voice (2026-09-08)

Queried the same public catalogue used to verify `mistv3`
(`users.rime.ai/data/voices/voice_details.json`, 863 entries, **no API key sent**):

| | |
|---|---|
| `mistv3` languages | `eng`, `fra`, `ger`, `spa` — **no Hindi** |
| `astra` | `eng` on every model it appears under, `arcana` included |
| Hindi voices | `anaya`, `anil`, `arya` (`arcana`, flagship, India); `nadi`, `taru` (`coda`) |

So Hindi is **not** a language-code change on the English voice: it is a different model and a
different voice. And because `speaker`, `modelId` and `lang` are query parameters on the `/ws3`
connect URL, one socket is one voice — a second language is a second connection, opened lazily.

**The voice was chosen by measurement, not preference.** Same sentence set, `/ws3`, PCM @ 48 kHz,
warm first-audio over three utterances (`scripts/verify_rime_hindi.py`):

| model / voice | cold | warm |
|---|---|---|
| **`arcana` / `anaya`** | 2820 ms | **1355 / 1716 ms** — chosen |
| `coda` / `taru` | 4730 ms | 1585 / 2044 ms |
| `coda` / `nadi` | 4006 ms | 1861 / 1910 ms |
| `arcana` / `arya` | 4638 ms | 1948 / 2764 ms |
| `mistv3` / `astra` (English control) | 2182 ms | 382 / 508 ms |

**Hindi costs roughly 3x the first audio of English on this provider.** That is stated because it is
true, not because it flatters the design. It is also why the switch is opt-in: a call that never
asks for Hindi never opens the `arcana` socket and never pays the cost.

### Devanagari or romanised — decided by listening

The catalogue says `anaya` speaks Hindi. It does not say what to send her. Both were rendered and
listened to; **Devanagari was correct**, and it also came back materially shorter for identical
content — 9.47 s against 12.21 s — which is the shape of an engine parsing a script natively rather
than falling back to spelling it out. Every Hindi template is therefore Devanagari
(`aether/hotel/tools_hi.py`).

R9.5 says Rime settings come from a human verifying the live service, never from an assumption. A
script is a setting, and this is that verification.

**Scope of that verification.** It was performed on `arcana`/`anaya`, the voice Rime has since
deleted. It settles the SCRIPT question — Devanagari, not romanised — and the duration measurement
behind it is reproducible with `scripts/verify_rime_hindi.py`. It does **not** carry over as a
listening judgement of the voice AETHER now ships (`coda`/`nadi`), which has not been listened to.
Both facts are listed in the caveats above rather than left for a judge to infer.

### Part 1d — Delivery claims, two text variants (2026-09-10)

The brief asks that a delivery claim be evidenced by holding the model and voice constant, rendering
**at least two text variants**, saving the clips, and explaining which wording changed the result.
`render_rime_compare.py` does the opposite experiment — it varies the MODEL against one fixed
sentence — so `scripts/render_delivery_variants.py` was added for this.

Held constant: `mistv3` / `astra` / `eng`, `/ws3`, PCM @ 48 kHz. Varied: the text only. All runs
warm (a warm-up render precedes the first pair), so none of these mix a cold socket with a warm one.

| # | Claim in the code | A (shipped) | B (variant) | A audio | B audio |
|---|---|---|---|---|---|
| 1 | `say_price` — never hand Rime a digit | "four hundred and twenty rupees" | "420 rupees" | 3050 ms | 2970 ms |
| 2 | `say_room_number` — a door, not a count | "Room three oh five" | "Room three hundred and five" | 2610 ms | 2830 ms |
| 3 | `say_time` — never gamble on a colon | "two in the afternoon … twelve noon" | "14:00 … 12:00" | 4330 ms | 3910 ms |
| 4 | extension read like a phone number | "extension one oh one" | "extension one hundred and one" | 2330 ms | 2850 ms |

Clips: `delivery_1_a.wav` … `delivery_4_b.wav` (gitignored; regenerate with the script).

**What is measured and what is not.** The durations above are measured. Which variant *sounds*
right is a listening judgement and has NOT been made — length is not quality, and the differences
these pairs exist to expose are audible rather than numeric. Until somebody listens and records the
outcome here, the spoken-form rules remain a reasoned design choice with A/B material available,
not a claim backed by a listening result.

### Listening comparison — `mistv2` vs `mistv3`
- [ ] Play both renders of the identical sentence and choose one

Both files are produced by `scripts/render_rime_compare.py` into the repository root
(`rime_mistv2.wav`, `rime_mistv3.wav`; `*.wav` is gitignored, so they are never committed). The
sentence exercises exactly what the demo needs — identity, a spelled-out price, and a negative:

> "You've reached AETHER, the hotel manager. The chicken kebab is three hundred and eighty rupees,
> and the seafood platter is not available today."

That sentence predates the hotel database, and neither the price nor the dish matches
`data/aether_hotel.db` any more. **It is left exactly as it was, in the script and here**, because
these are recordings of what was actually synthesised and the numbers below were measured from
them. Rewriting the sentence would silently invalidate the measurements it produced. What is being
compared is a voice, and that comparison does not depend on the menu being current.

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
- [x] **Review the Rime Voice AI project catalog** and confirm AETHER is not a close reproduction.
      **DONE 2026-09-10** against `github.com/rimelabs/rime-dev-projects` — 16 projects, read from
      the repository's own `data/projects.json`, plus each project's README. 14 readable;
      `Continuum` and `NOVA` return 404 and were assessed from their catalog summary only.
      Conclusion and the full comparison are in [JUDGING.md](JUDGING.md); the short version is that
      interruption is the most common theme in this catalog (8 of 14), **FieldMate implements
      generation fencing against stale async work independently**, and AETHER's remaining
      contribution is narrower than previously claimed. A prior review of the AssemblyAI showcase
      was of the wrong list and has been withdrawn.

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

### One full call, measured end to end (2026-09-10)

`python scripts/demo_full_call.py --mode live --repeats 3` — three complete calls, **81 answered
turns**, real Gemini and real Rime over `/ws3`. Text is injected where the recogniser would put it,
so **STT is not in these numbers**; everything downstream of the transcript is real.

`heard` is what the caller actually waits: transcript in until the first audio of the answer. It is
`llm_ms + tts_ms`, both stamped by the pipeline. Routing and the SQLite lookup sit between them and
measure under a millisecond, so they round away — the offline run of the same script shows them at
0.4–5.7 ms with no network at all.

| | n | avg | median | min | max |
|---|---|---|---|---|---|
| **Heard — answered from the database** | 75 | 344.7 | **337.2** | 320.3 | 448.5 |
| **Heard — answered by the model** | 6 | 1368.8 | **1344.9** | 1282.6 | 1551.7 |
| Heard — every turn | 81 | 420.5 | 337.6 | 320.3 | 1551.7 |
| Rime alone, to first audio | 81 | 343.8 | 336.9 | 320.0 | 448.5 |
| Model alone, when it was used | 6 | 1036.1 | 1016.3 | 944.9 | 1231.7 |
| Whole turn, including streaming the complete reply | 81 | 1383.6 | 1208.1 | 363.6 | 4057.4 |

**75 of 81 turns were answered with no model call at all.** On those, Rime *is* essentially the
entire wait — the database costs under a millisecond, so the number a judge should hold AETHER to is
Rime's own time to first audio.

The last row is deliberately included and deliberately not the headline: `speak()` blocks until the
whole reply has streamed, which is longer than the caller waits. Two earlier drafts of this script
reported that figure as the answer latency, which credited the wrong stage by about 320 ms a turn.

Per-language first audio in the same run: English 320–339 ms, Spanish 353–360 ms, Hindi 372–449 ms.
Hindi is a different model (`coda`) and remains the slowest, consistent with Part 1c.

The same run also carries the interruption and the language switches — see *What one call proves*
below.

### What one call proves

The script is one call, so these are properties of the same run that produced the table above:

- the caller asked for starters, interrupted mid-lookup and asked for desserts instead;
- generation `G1` was fenced, **the abandoned turn spoke nothing**, the discard was recorded, and
  `ResultLeaked` stayed at **0**;
- the final answer was the desserts answer, from the database;
- the language changed three times mid-call, and the Rime voice changed with it every time:
  `nadi`/`coda` for Hindi, `isa`/`mistv3` for Spanish, `astra`/`mistv3` back in English.

---

### Playback latency — the gap between "accepted" and "audible"

Measured with `python scripts/measure_playback_latency.py --repeats 8` on 2026-09-10, Windows 11 /
Python 3.13.2, WASAPI output device 8 @ 48 kHz, blocksize 480. No model, no network, no Rime: this
isolates the buffering the application itself adds.

| Enqueue → device callback | Value |
|---|---|
| **Cold** (first enqueue after `open()`, includes output-stream spin-up) | 221.4 ms |
| **Warm** (n=8) | min 5.9 / median 9.2 / max 12.6 ms |
| Device buffer after that (`stream_output_latency_ms`) | 22.0 ms — reported, never added |

**Why this number exists.** Every other latency figure in this document ends at `first_audio`: the
moment the TTS client accepts Rime's first chunk. That is upstream of the output queue and of the
device buffer, so it is *not* "when the caller heard it". `TurnTiming.output_latency_ms` ends at
`first_output` instead — stamped inside the PortAudio callback the moment it hands real samples for
that generation to the device — and the table above is the difference between the two.

**What is still excluded, and stays excluded.** The device's own 22 ms buffer sits after the last
point this process can observe, and the telephone network sits after that. Neither is folded in:
adding an unobserved constant to a measured number makes the result look more precise than it is.
So AETHER does not claim an ear-to-ear figure anywhere — it claims what it can stamp.

Cold and warm are reported separately and never averaged. A caller pays the spin-up once per
session; averaging it across turns would present a one-off setup cost as a per-turn cost.

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

## Part 6 — Real phone call — **PARTIALLY VERIFIED**

**Calls connect, are answered, are understood and are transcribed.** The telephony audio below is
measured from real calls. What is still *not* separately measured is STT word accuracy on
narrowband audio, and the interruption table at the end of this part still carries placeholders.

**The committed run is a phone call, and that is now provable.** Earlier revisions of this part
said its input path could not be established, because a trace records latency and fencing but not
whether audio arrived from a handset. The worker log for the same run was found afterwards and is
committed as [`evidence/demo-call-worker.log`](evidence/): all sixteen turns carry identical
`stt`/`llm`/`tts`/`turn` latencies in both files, and the log is unambiguously the LiveKit/SIP
worker (`agent name : aether-hotel`, `[4/7] inbound pump starting`, `pumps=1`,
`inbound audio captured`). The caller's number is redacted; no secret appears in it.

What the log establishes directly, on a real call rather than in a test:

| Claim | Line in the log |
|---|---|
| One track produces one pump, and a second subscription is refused | `track already has a pump (already-subscribed); not starting a second`, then `pumps=1` |
| The inbound pump survives the call without dropping frames | `frames_failed=0` |
| Diagnostics report the rate actually observed | `source_rate=48000  observed_rate=48000  vad_rate=16000` |
| Optional call capture works, and is off unless asked for | `inbound audio captured: call3.wav` |
| Deterministic answers really are free of the model, over the phone | 11 of 16 turns logged `llm=0` |

The first attempt reached the worker and AETHER never replied; four defects were found and fixed
(entrypoint deadlock on an Event only teardown could set; 9051 ms of blocking pipeline construction
on the event loop; `track_subscribed` registered after `connect()` so the event landed in the gap;
and no greeting existed). Those fixes are tested, and **they have since been exercised by real calls** — the call-path table below is filled in from them.

### Call path

| Item | Status |
|---|---|
| Worker registers as `aether-hotel` | **YES** — `run-20260908T023320Z` |
| Inbound call reaches the entrypoint | **YES** — stages [1/7]…[7/7] all logged |
| Caller's audio track subscribed | **YES** — and twice, which was the defect |
| Greeting heard on the handset | **YES** — confirmed by the caller |
| Caller transcribed correctly (word accuracy) | **NO** — audio doubled and interleaved |
| Rime audio heard on the handset | **YES** — 6710 ms audible outbound |
| Call ends cleanly, trace closed | **YES** — caller hangup, teardown, trace written |
| `diagnose()` verdict for the call | `OK` — **and it was wrong.** See below |

**The verdict was `OK` on a call that failed**, because `diagnose()` walks the pipeline for the
first stage that produced *nothing*, and every stage produced something: audio arrived, speech was
detected, a transcript existed, a turn was spoken. It has no test for audio that arrives *corrupt*.
The `transport:` line added afterwards carries `pumps`, which is the fact that would have named
this defect immediately — `pumps=2` for a single caller.

### Telephony audio — FIRST REAL MEASUREMENT, `run-20260908T023320Z`

A call was answered on 2026-09-08 (23 s, inbound +919840870678). The caller heard the greeting and
AETHER could not understand them; the cause was a transport defect, not levels. **The audio itself
was measured, and this is the first telephony evidence in this repository.**

Every utterance the VAD saw on that call:

| duration | voiced | peak rms | mean rms | ambient | floor | outcome |
|---|---|---|---|---|---|---|
| 2440 ms | 960 ms | **15.8** | 10.5 | 4.1 | 35.0 | rejected `below_noise_floor` |
| 1320 ms | 540 ms | **6372.5** | 1478.6 | 8.0 | 35.0 | accepted |
| 3100 ms | 2280 ms | **10174.2** | 2121.0 | 11.1 | 35.0 | accepted |
| 3000 ms | 1220 ms | **12.9** | 8.1 | 5.6 | 35.0 | rejected `below_noise_floor` |
| 1520 ms | 300 ms | **207.4** | 75.2 | 13.1 | 39.3 | accepted |
| 3700 ms | 3040 ms | **10071.8** | 3378.4 | 11.9 | 35.7 | accepted |

| Item | Laptop value | Real-call value |
|---|---|---|
| `AETHER_SPEECH_FLOOR` | 35 | **2500** — derived from 28 utterances across 3 calls |
| Inbound `peak_rms` during speech | 8422–12441 | **6372–10174** |
| Inbound `floor_rms` (ambient) | 3.6–24.8 | **4.1–13.1** |
| Line noise peak (rejected) | — | **12.9–15.8** |
| `min_speech_ms` | 250 | 250 — no utterance was rejected as `too_short` |
| Endpointing (trailing silence) | 500 ms | 500 ms — every utterance endpointed |
| STT word accuracy | — | `<from_run>` — audio was garbled by the transport defect |

**CORRECTED 2026-09-08 after two further calls.** The paragraph below was written from the first
call alone, where line noise happened to sit at 12.9-15.8 RMS. Two later calls showed noise reaching
**1986** -- call-setup bursts and carrier artefacts that the first call did not contain. Against a
floor of 35, eleven of fifteen noise utterances were accepted, transcribed by Whisper into
hallucinations ("Good job.", "We'll see you in the next one.") and answered as if the caller had
said them.

Pooling all 28 utterances from three calls gives a clean separation:

| | n | min | max |
|---|---|---|---|
| Real speech | 13 | **5560.3** | 14971.0 |
| Noise / hallucination | 15 | 12.1 | **1986.2** |

A factor of 2.8 between the loudest noise and the quietest real speech. **`AETHER_SPEECH_FLOOR` is
now 2500**, which admits 0 of 15 noise utterances and loses 0 of 13 real ones. Any value in
2000-3500 achieves that; 2500 is biased toward the low end because dropping genuine speech is the
worse failure (RULES.md R2.3), leaving 1.26x margin above the loudest observed noise and 2.2x below
the quietest observed speech.

This is the recalibration the plan predicted would be needed. The original claim below is left in
place, struck through by this note, because being wrong in public is the point of an evidence file.

~~**The prediction that thresholds would not transfer was WRONG, and that is worth stating
plainly.**~~
Telephony speech peaked at 6372–10174 RMS against a floor of 35 — a margin of 180x to 290x, close
to the laptop's own 8422–12441. Line noise sat at 12.9–15.8 and was correctly rejected. `35` was
derived on a laptop and happens to be right for this carrier and handset.

**No threshold was changed, and none should be** until a call is observed rejecting real speech.
The separation here is wide enough that recalibration would be tuning against noise.

Still unmeasured: **STT word accuracy on narrowband audio.** Both transcripts from this call came
from doubled, interleaved audio, so they say nothing about Whisper on clean telephony. `base.en`
fell through all six temperature fallbacks (compression ratio 9.82, i.e. near-total repetition) on
the garbled input — which is evidence of the transport defect, not of narrowband performance.

### Pipeline latency, measured — `evidence/demo-run.jsonl`

Sixteen turns of real speech, real `base.en`, real Gemini, real Rime `/ws3`. Committed, so every
number here can be recomputed from the file rather than taken on trust. (Input path not recorded —
see the scope note at the top of this part.)

| Measurement | Median | Min | Max |
|---|---|---|---|
| Turn latency (speech ended → answer decided) | **1262 ms** | 1119 | 2549 |
| Response latency (speech ended → audio playing) | **2455 ms** | 1656 | 3784 |
| STT (`base.en`) | 927 ms | 841 | 1147 |
| **Rime first audio (`mistv3`, `astra`, `/ws3`)** | **280 ms** | 270 | 346 |

**11 of 16 turns ran at `llm_ms = 0`** — answered from `data/aether_hotel.db` with no model in the
path, which is the latency argument for the deterministic router made concrete. **2 generations were
fenced** and **`ResultLeaked` never appears**: the golden invariant holding on a real run, not only
in a test.

### Recogniser error, and what the caller heard instead

The most useful part of that run. `base.en` mangled five inputs, and **not one produced a fabricated
fact**:

| Heard | Spoken back | llm_ms |
|---|---|---|
| "How much is a daily speed?" | "I am not quite sure what you mean by a daily speed, but I can help you with our room rates or menu if you like." | 1386 |
| "How much is deluxe heat?" | "I am not sure what you mean by deluxe heat, but our Deluxe King room is eight thousand five hundred rupees a night." | 1004 |
| "How much is daylight skis?" | "I am sorry, I did not catch that. Could you please repeat it?" | 1090 |
| "is room one, three," | "Room numbers run from one oh one to five ten." | 775 |
| "What comes with an executive suit?" | the exact Executive Suite amenities from the database | 1360 |

Three admitted confusion; two recovered the correct fact from the hotel injected into the prompt by
`menu_for_prompt`. That is the safety net working on real audio rather than on a phrase somebody
chose.

Two of those were defects rather than bad luck, and both are fixed with tests
(`tests/test_hotel_db.py`): `suite` is pronounced "sweet", so the recogniser returns `suit`/`sweet`
and the room-type match missed — repaired before matching, so the turn now costs 0 ms instead of
1360 ms. And separately, `"do you have room service"` misheard as `"Do you have room for this?"` was
being answered with the list of room types; it now falls through. Replaying all 218 distinct
utterances in `traces/`: **+2 answered deterministically, −1 confidently-wrong answer, 0 rerouted to
a different tool.**

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
