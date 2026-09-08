# AETHER

**AETHER answers a hotel's telephone as its duty manager.** A customer calls, asks about the menu in
ordinary speech, interrupts mid-answer, changes their mind, or tells it to stop — and the
conversation stays coherent, with a live view of what the system is doing and why.

> **Judging:** [JUDGING.md](JUDGING.md) maps each criterion to the file, test or trace that backs
> it — including what we did not build and what is not measured.

The product is a hotel voice manager. The engineering problem underneath it is conversation
continuity during realtime voice interaction: users can interrupt, refine, replace, question, cancel
or start a new task while the agent is working, **without allowing stale state to become spoken
output**.

The hard part is not speech-to-text and it is not text-to-speech. It is what happens when a human
talks *while the agent is thinking, running a tool, or speaking*. The system must decide what the
interruption **means**, update conversational state correctly, and guarantee that work which is now
obsolete never reaches the speaker.

## The golden invariant

> **A stale result must never become spoken output.**

Every unit of work carries a generation ID. Before anything is spoken, the system verifies the
result belongs to the currently valid generation. If it does not, the result is discarded and the
discard is recorded in the event trace.

This invariant matters more than classifier accuracy. When classification is uncertain, AETHER fails
safe: it fences rather than speaks.

## Architecture in one diagram

```
 PSTN ──▶ LiveKit SIP ──▶ Room ──┬─▶ inbound track ─▶ resample 48k→16k ─▶ MicVAD.process_frame
                                 │                                              │
                                 │           Whisper ─▶ classify ─▶ HotelRouter ─┼─▶ hotel tool
                                 │                                              └─▶ Gemini
                                 │                                              │
                                 │                                    Rime /ws3 (unchanged)
                                 │                                              ▼
                                 └─◀ published track ◀─ AudioGate._callback ◀────┘
                                                              │
                    trace events ──▶ WebBridge ──▶ browser (the AETHER console)
```

**LiveKit is transport only.** It carries phone audio in and out. It is emphatically *not* the
brain: the worker uses `livekit.rtc` directly and deliberately **not** `AgentSession`, which brings
its own STT, LLM, TTS and turn detection and would bypass the GenerationRegistry, the AudioGate and
the entire fence — the parts being judged.

Everything between the two bridges is the same code the local microphone path runs. A fence that
works on a laptop works on a call because it is literally the same `AudioGate._callback` either side
of the swap.

## Interruption taxonomy (frozen — six classes)

All six are implemented in `aether/classify/`, deterministically: closed sets, whole-utterance
matching, no model, no confidence threshold. Anything the classifier does not recognise **exactly**
falls through to `REPLACEMENT`, which fences — so a misclassification can cost recall and can never
cost correctness.

| Class | Meaning | What actually happens |
|---|---|---|
| `BACKCHANNEL` | "mhm", "yeah", "okay" while AETHER is speaking | **No generation is allocated.** The answer in flight is never fenced and keeps playing. `BackchannelDetected` |
| `REFINEMENT` | An explicit correction: "actually…", "I meant…" | New generation; old one fenced. `TaskReplaced(reason=refinement)` |
| `REPLACEMENT` | Anything else said over a running task | New generation; old one fenced. `TaskReplaced(reason=replacement)` |
| `STATUS_QUERY` | "Are you still there?" during a task | **No generation is allocated.** No fence, no `TaskReplaced`; the task survives |
| `CANCEL` | "Stop", "forget it", "never mind" | Fenced with reason `cancelled_by_caller`, and **no successor task**. `CancellationResolved` |
| `NEW_TASK` | A request with nothing in flight | An ordinary turn. Nothing was displaced, so no `TaskReplaced` |

Two deliberate omissions, stated rather than papered over:

- **Salvage (`ResultSalvaged`) is not implemented.** AETHER holds no partial-result store, so a
  refinement reuses nothing. Emitting a salvage event with `records_reused = 0` on every run would
  be evidence-shaped noise (RULES.md R8), so refinement is labelled honestly and fences.
- **A status query is not answered aloud.** Speaking over an answer already in flight would need a
  second audio path able to bypass the gate's single active generation. The class protects the
  task; it does not talk over it.

Suspend/resume is **not** implemented and is not claimed.

## The two controls

They are different mechanisms, and confusing them is a bug this codebase has already had once.

| | START / STOP LISTENING | INTERRUPT |
|---|---|---|
| Question it answers | "can AETHER hear me?" | "stop what you are doing" |
| Mechanism | `MicVAD.set_listening` → `InboundBridge` drops frames | `BargeInCoordinator.fence_now` |
| Fences a generation | **never** | always (when there is one) |
| Closes the microphone | yes, that is the point | **never** |
| Ends the call | **no** — the room, track and outbound pump keep running | no |

**One toggle**, alternating between `START LISTENING` and `STOP LISTENING`; never both on screen at
once. It is not push-to-talk, and INTERRUPT is never the way to begin speaking — the caller can talk
whenever listening is on.

Natural barge-in and the INTERRUPT button converge on the same `fence_now`. One fence, two triggers,
distinguished only by `reason` in the trace (`voiced_duration_confirmed` vs `button_interrupt`).

## The hotel database

`data/aether_hotel.db` (SQLite) is the **source of truth** for every hotel fact AETHER states. It
replaced a hand-written Python fixture, and the differences are real rather than cosmetic: prices
moved, the menu is 12 items rather than 29, "vegetarian mains" became a category of its own, and the
database records **no spice level at all** — so "is it spicy?" is now answered by reading the
hotel's own description back instead of inventing a rating.

| Table | Rows | What a caller can ask |
|---|---|---|
| `menu_items` / `menu_categories` | 12 / 5 | price, category, diet, allergens, availability, description |
| `rooms` / `room_types` | 50 / 5 | status of a room number, what is free, nightly rate, amenities |
| `hotel_services` | 6 | what exists, its hours, its extension |
| `hotel` | 1 | check-in and check-out times, address, currency |
| `reservations` / `guests` | 3 / 4 | which dates a room is held for |

**Read-only, and enforced by SQLite rather than by convention.** `aether/hotel/db.py` opens the file
with the `mode=ro` URI, so a write is rejected by the driver, not by a code path that could be
edited around. There is no tool that creates, cancels or changes anything, and a test asserts that
every result is stamped `mutates: false`.

Seventeen read-only tools run through the **existing** `ToolRunner`, so fencing, injectable delay
and result identity are not reimplemented:

| | |
|---|---|
| menu | `menu_overview`, `list_category`, `price_of`, `find_by_diet`, `check_availability`, `check_allergens`, `safe_for`, `describe_item` |
| rooms | `room_status`, `room_availability`, `list_room_types`, `room_price`, `room_amenities` |
| hotel | `list_services`, `service_hours`, `check_in_out`, `reservation_for_room` |

`menu_overview` answers the broadest and usually first question — "what's on the menu?", "what
dishes do you have?" — with the *shape* of the menu rather than its contents, computed from the
database: "We have starters, mains, vegetarian mains, desserts and drinks, with vegetarian, vegan
and non-vegetarian options." Reading every dish name down a telephone is not an answer, and **there
is one hotel and one menu, so AETHER never asks which restaurant the caller means.**

Numbers are rendered for speech, and a room number is not a quantity: `say_price(420)` gives "four
hundred and twenty rupees", `say_room_number("305")` gives "three oh five", `say_time("14:00")`
gives "two in the afternoon". Rime is never handed a digit or a colon. A reservation lookup
deliberately never speaks the guest's name.

**Hotel facts do not go through the LLM.** `aether/hotel/router.py` maps a sentence to a tool by
keyword and slot, and a template renders the answer for speech. The router answers only when
confident and returns `None` otherwise, so anything ambiguous still reaches Gemini. Two reasons: an
LLM stage measured ~1.8 s that a lookup does not, and a model asked to read back `{"price": 420}`
can still say a number the hotel does not charge — or an allergen that could put somebody in
hospital.

That fallback is itself defended. On a real call `base.en` transcribed "dessert" as "Desert", no
rule matched, and Gemini answered from nothing — inventing three dishes and three prices. The
system prompt now carries the entire hotel, generated from the database, so a router miss costs a
slower answer rather than a fabricated one.

## Two languages

AETHER answers a hotel in India, so it speaks Hindi. The greeting offers it — *"For Hindi, just say
Hindi"* — the caller asks mid-call, and the voice, the recogniser and the templates all change
together. *"English"* switches back.

**The Hindi answers are still templates over the same database rows.** `llm_ms` stays 0. Handing a
price to a model to phrase in Hindi would hand it the chance to say the wrong one, in a language
fewer people in the room can check — which is the exact failure the English path already refuses.

Nothing was assumed. Rime's public catalogue says `mistv3` speaks eng/fra/ger/spa and that `astra`
speaks no Hindi on any model, so Hindi is a **different model and a different voice** — and since
`speaker`, `modelId` and `lang` are baked into the `/ws3` connect URL, a second language is a second
socket, opened only if it is used. `arcana`/`anaya` was chosen by measuring all four Hindi voices;
Devanagari over romanised was chosen by listening to both. Both are in
[RIME_EVIDENCE.md](RIME_EVIDENCE.md) Part 1c.

`base.en` is a monolingual model and cannot transcribe Hindi at all, so the multilingual recogniser
is loaded lazily on the first Hindi turn. An English-only call never loads it, and the English path
— model, voice, latency — is byte-for-byte what it was.

The honest cost: **Hindi is about 3x slower to first audio** (~1.5 s against ~0.4 s). It is
measured, it is written down, and it is why the switch is opt-in.

## Evidence model — three tiers, never mixed

1. **Local / laptop** — measured on this machine with a real microphone or a WAV, real STT, real
   Gemini, real Rime. Every latency number in this repository is from this tier unless it says
   otherwise.
2. **Synthetic bridge** — the LiveKit-shaped adapters driven from a WAV or generated audio, with no
   phone in the loop. Proves the plumbing and the fence; proves nothing about telephony.
3. **Real phone call — PARTIALLY VERIFIED.** Calls connect, are answered, are understood and are
   transcribed; telephony audio levels are measured from 28 utterances across three calls, and the
   speech floor was re-derived from them (35 → 2500). Still **not** separately measured: STT word
   accuracy on narrowband audio.

One thing a trace cannot tell you, and it is worth knowing before quoting any number: **the input
path is not recorded.** A trace does not say whether its audio came from the telephone or the local
microphone, and it cannot be inferred afterwards — the event vocabulary is identical on both, and
`AETHER_SPEECH_FLOOR=2500` is set in `.env` so the telephony-derived floor applies to the laptop
too. Numbers from `evidence/demo-run.jsonl` are therefore quoted as properties of the pipeline, not
of the telephone.

**No measurement here is real until it carries a `<from_run>` value.** See
[RIME_EVIDENCE.md](RIME_EVIDENCE.md).

## Speech output

Rime is the primary and sole TTS in the judged path — there is no fallback TTS by design (RULES.md
R9.4). Configuration, verified against Rime's live catalog:

| | |
|---|---|
| Model | `mistv3` |
| Speaker | `astra` |
| Language | `eng` |
| Transport | WebSocket `/ws3`, one persistent socket reused across turns |
| Format | raw PCM at the AudioGate's own sample rate |

`mistv3` + `astra` + `eng` is confirmed present in Rime's catalog
(`https://users.rime.ai/data/voices/voice_details.json`: 863 entries, 83 under `mistv3`, with
`astra` listed under `mistv3`, `mistv2` and `arcana`). Which of `mistv2`/`mistv3` *sounds* better
has not been judged and is not claimed; `RIME_MODEL` switches between them in one line.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in Rime, an LLM key, and LiveKit
```

The demo call is scripted word for word in [DEMO_SCRIPT.md](DEMO_SCRIPT.md), with every answer
quoted from the running system and re-verified by a test, so the sheet cannot drift from the
database.

**The console (local microphone).** The full product without a phone: orb, transcript, both
controls, evidence strip.

The console is a **fixed frame the height of the viewport, with one scroller inside it** — the
conversation. That is deliberate: when the page itself grew with the transcript, a long call pushed
the orb, `START LISTENING` and `INTERRUPT` below the fold, so the controls disappeared exactly when
a demo needed them, and scrolling back through the conversation moved the whole console. Now only
the conversation moves, the controls are always on screen, and the transcript keeps a visible
scrollbar so it is obvious there is history above. Scrolling up to re-read stays put — the
stick-to-bottom only fires if you were already at the bottom. Below 960 px the frame is released
and the page scrolls normally, because two stacked panels cannot share a phone screen.

Type and colour are sized to be read at a distance by someone who is not at the keyboard. Every ink
in the palette clears 4.5:1 against the panel it sits on, asserted in `tests/test_web_bridge.py`;
`--ink-faint` used to be 3.29:1, which is why the labels, the hint and the recording path were hard
to see.

```bash
python -m aether.web
# then http://127.0.0.1:8760/index.html?ws=8761   (opened automatically)
```

**The LiveKit worker (real calls).** Registers as `aether-hotel` and waits. Prewarms the process
before registering, and serves the same console for the duration of each call.

```bash
python -m aether.telephony.agent dev        # register and wait for calls
python -m aether.telephony.agent start      # production run
```

**Everything else.**

```bash
python -m aether.spike                      # terminal only: ENTER interrupts, m toggles the mic
python -m aether.prewarm                    # measure model/import warm-up on this machine
python scripts/diagnose_mic_turn.py --phrases   # say a phrase, see which stage changed it
python scripts/calibrate_mic.py             # derive AETHER_SPEECH_FLOOR for your microphone
python scripts/bench_turn.py                # headless end-to-end turn, no mic
pytest -q
```

**Wear headphones on the local path.** The microphone stays open while the agent speaks and there is
no acoustic echo cancellation; on open speakers the agent retriggers its own VAD. On a phone call
the handset and the carrier do their own AEC, so this is expected not to apply — *expected*, not
verified.

Without Rime credentials the agent runs but cannot speak, and says so.

## Failure behaviour

| Failure | What happens |
|---|---|
| Rime unreachable / rejects | The turn is discarded with `ResultDiscarded(stage=tts)`. The session survives. Nothing is substituted — silence, not a different voice |
| LLM error, timeout or empty reply | `ResultDiscarded(stage=llm)`. Nothing is spoken and nothing enters history |
| Empty transcript (noise) | Ignored. **Does not fence** the answer in flight — the transcript is taken before a generation is allocated |
| Menu lookup fenced mid-flight | Nothing spoken; the stale answer is not handed to the LLM either |
| Caller hangs up | Everything in flight is fenced (`call_disconnected`), both pumps stop, the trace is closed |
| Browser disconnects | The engine is unaffected. On reconnect the browser is handed the whole conversation again from the backend |
| Console cannot bind its port | Logged; the call proceeds without a UI |

## Diagnostics

`aether/telephony/diagnostics.py` walks the pipeline in order and names the **first** stage that
produced nothing, from the bridge counters and the trace: `inbound_audio`, `listening_gate`, `vad`,
`stt`, `reply`, `tts`, `outbound_audio`. It is printed at the end of every call. "AETHER never
replied" is six different faults wearing the same coat, and this is what tells them apart.

Inbound level metering (`peak_rms`, `mean_rms`, `floor_rms`) is reported next to the VAD's current
`speech_floor`, so the threshold can be re-derived from real call audio when a call finally happens.
Nothing tunes itself.

## Known limitations

- **The real phone path is unvalidated.** See the evidence model above.
- Every VAD threshold is calibrated on a laptop microphone. Telephony audio is narrowband and
  codec-compressed; `AETHER_SPEECH_FLOOR` is expected to need re-deriving and has not been.
- `base.en` on narrowband phone audio may be materially worse than on clean 16 kHz. Unmeasured.
- One caller at a time is the tested case. Each call constructs its own pipeline and its own hotel
  store, so calls do not share state, but concurrency has not been exercised. The database is opened
  read-only, so concurrent readers are safe by construction.
- `AETHER_UNSAFE_MODE` is parsed and honoured nowhere, so the "unsafe control condition" acceptance
  scenario is skipped rather than passing.
- No AEC on the local path.

## Status

**945 tests pass, 2 are skipped.** Both skips are features that genuinely do not exist, and each one
says which: the unsafe-mode control condition, and salvage.

| Built and tested | Not built |
|---|---|
| Hotel SQLite database, 17 read-only tools, deterministic routing | Salvage / partial-result reuse |
| Six-class deterministic classifier, wired into the turn path | Suspend/resume |
| Generation registry, barge-in coordinator, four-layer fence | Evaluator (offline trace scoring) |
| AudioGate with per-chunk generation tagging | Unsafe-mode control path |
| Rime `/ws3` streaming, persistent socket, mid-utterance stop | |
| LiveKit↔AETHER audio bridges (synthetic-verified) | |
| Telephony worker: lifecycle, greeting, teardown, diagnostics | |
| One listening toggle + separate INTERRUPT, both through the real bridge | |
| Siri-style console: orb, transcript, evidence strip, reconnect, contrast-tested palette | |
| Process prewarm (3810 ms cold → 672 ms warm) | |
| Append-only JSONL trace with secret redaction | |

See [MEMORY.md](MEMORY.md) for the authoritative record of locked decisions, and what is measured
versus deferred.

## Documents

| File | Purpose |
|---|---|
| [DEMO.md](DEMO.md) | The demo runbook: exact flow, exact commands, what to do when it fails |
| [PRD.md](PRD.md) | Product requirements and acceptance criteria |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, generation model, canonical event vocabulary |
| [RULES.md](RULES.md) | Non-negotiable invariants and the rules that enforce them |
| [PHASES.md](PHASES.md) | Canonical plan with checkpoints and cut rules |
| [DESIGN.md](DESIGN.md) | Design decisions and the reasoning behind them |
| [MEMORY.md](MEMORY.md) | Locked decisions, status, measured vs unmeasured, human tasks |
| [RIME_EVIDENCE.md](RIME_EVIDENCE.md) | Rime checklist and pre-registered acceptance tests |
