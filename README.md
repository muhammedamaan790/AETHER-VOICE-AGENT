# AETHER

**A hotel's telephone line where interrupting is safe.**

AETHER answers a hotel's phone as its duty manager, in **English, Hindi or Spanish**. Callers ask
about the menu, the rooms and the hotel's policies in ordinary speech, and can book a room or a
table. They can also do what people on the phone always do: interrupt, change their mind halfway
through an answer, or say *stop*. AETHER keeps up — and never says something that has stopped being
true.

> **A stale result must never become spoken output.**
>
> The one rule the whole system is built around.

| | |
|---|---|
| **See it** | The recorded demo, and its word-for-word script: [DEMO_SCRIPT.md](DEMO_SCRIPT.md) |
| **Run it** | [Quick start](#quick-start) below · every step for a new machine: [SETUP.md](SETUP.md) |
| **Judge it** | [JUDGING.md](JUDGING.md) maps each criterion to the file, test or trace behind it |
| **Check it** | [RIME_EVIDENCE.md](RIME_EVIDENCE.md): the claim, the acceptance test, the procedure, the result and the limits |

**Contents** — [What it does](#what-it-does) · [Quick start](#quick-start) ·
[The hard problem](#the-hard-problem-interruption-and-recovery) · [How it works](#how-it-works) ·
[Three languages](#one-hotel-three-languages) · [Bookings](#bookings-and-what-can-never-be-written) ·
[Speech output](#speech-output--rime) · [Results](#measured-results) ·
[Third-party services](#third-party-services) · [Failure behaviour](#failure-behaviour) ·
[Known limitations](#known-limitations) · [Repository](#repository-map)

---

## What it does

A phone call has no screen. The caller can't tap a menu, read a price list or scroll back to what
was said. For a hotel's phone line, speech isn't a nicer interface — it's the only one.

| A caller can | For example |
|---|---|
| Ask about the menu, prices, allergens and diets | *"I'm allergic to nuts, what can I eat?"* |
| Ask about rooms, rates and what's free | *"How much is an executive suite?"* |
| Ask about 27 hotel policies | *"Do you have a swimming pool?"* · *"What ID do I need?"* |
| Book a room or a table, and cancel | *"Book it for two nights."* · *"A table for four at eight."* |
| Speak Hindi or Spanish | *"चिकन कबाब कितने का है?"* · *"¿Tienen piscina?"* |
| **Interrupt, change their mind, or say stop** | Talk over any answer, at any moment |

Hotel facts come **from the hotel's own database, never from a language model**. A question the
database can answer never reaches the model at all. A database answer starts playing in about
**a third of a second**. Questions the hotel has no record of go to Gemini, which answers naturally
as the duty manager.

---

## Quick start

On Windows PowerShell (macOS and Linux are in [SETUP.md](SETUP.md)):

```powershell
git clone https://github.com/muhammedamaan790/AETHER-VOICE-AGENT.git
cd AETHER-VOICE-AGENT
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env        # then open .env and add your Rime, Gemini and LiveKit keys
python scripts/demo_full_call.py   # a whole call, with no phone and no microphone -> PASS
```

Then choose how to use it:

| To | Run |
|---|---|
| **Take real phone calls** | `python scripts/reset_hotel_db.py` then `python scripts/run_call.py`. Wait for `registered worker`, open the console address it prints, and ring your LiveKit number |
| **Talk to it on your laptop** | `python -m aether.web`, then click **Start Listening**. Wear headphones |
| **Run the tests** | `python -m pytest -q` |

**First time?** [SETUP.md](SETUP.md) goes through every step: installing Python, getting each key,
connecting a phone number to LiveKit, and recording the demo with Windows Phone Link.

---

## The hard problem: interruption and recovery

The hard part is not speech-to-text and not text-to-speech. It's what happens when a person talks
**while the agent is thinking, looking something up, or speaking**. By the time the caller has
changed their mind, the old answer may already exist — half-synthesised, or queued for the speaker.
Cancelling it is a race that can be lost. So AETHER doesn't rely on cancelling: it **refuses to
speak** anything that belongs to a request the caller has abandoned.

**Generation fencing.** Every request gets a generation ID. When the caller interrupts, the current
generation is *fenced* — permanently, and it can never be un-fenced. Four independent layers check
the generation before anything reaches the caller's ear: the tool runner, the language model's
output, the Rime client, and the audio callback that writes to the speaker. An answer computed for an
abandoned request can finish, and it still can't be heard.

**Everything the caller did not hear is forgotten.** The same rule covers memory as well as sound.
Conversation history, what "it" refers to, a *"did you mean…?"* offer and a booking are all committed
only once the answer has actually been spoken. Talk over an answer and it leaves no trace. A booking
the caller changed their mind about mid-way writes nothing.

**Interruption isn't one thing.** Six kinds are told apart deterministically — no model, no
confidence score — and each is handled differently:

| Kind | Example | What AETHER does |
|---|---|---|
| Backchannel | *"mm-hm"*, *"okay"* | Keeps talking. Nothing is fenced |
| Refinement | *"actually…"*, *"I meant…"* | Fences the old answer, answers the correction |
| Replacement | anything else said over an answer | Fences the old answer, answers the new question |
| Status query | *"are you still there?"* | Leaves the task running |
| Cancel | *"stop"*, *"never mind"* | Fences, and starts nothing new |
| New task | a question when nothing is in flight | An ordinary turn |

Anything the classifier doesn't recognise **exactly** is treated as a replacement, which fences. So
a misclassification can cost a little responsiveness and can never cost correctness.

**Stopping the voice.** The moment the caller starts speaking, AETHER's voice drops in volume within
about **22 ms**. It stops completely once the speech is confirmed as meaningful, about **321 ms**
after onset, and tells Rime to clear its queue mid-sentence. Both are medians measured in synthetic
mode; the live-microphone figures aren't recorded yet. The 300 ms confirmation is deliberate, so a *"mm-hm"* doesn't
kill the answer it was encouraging.

**The proof.** [`tests/test_full_duplex_acceptance.py`](tests/test_full_duplex_acceptance.py) is the
brief's own acceptance test, run against the real pipeline. It checks every clause separately: a
fixed delay is injected into a lookup, the caller interrupts and changes part of the request,
queued audio stops, the stale result is never spoken, and the final answer is to the question they
ended up asking. **Across 84 recorded runs, including real phone calls, the number of stale results
that reached a caller is zero.**

---

## How it works

```
 Phone ──▶ LiveKit SIP ──▶ room ──┬─▶ caller's audio ─▶ speech detection ─▶ recogniser (Whisper)
                                  │                                              │
                                  │                   classify the interruption ─┤
                                  │                                              ▼
                                  │                       hotel router ──▶ database tools
                                  │                            └──────────▶ Gemini (only if needed)
                                  │                                              │
                                  │                                   Rime, streamed over /ws3
                                  │                                              ▼
                                  └─◀ AETHER's audio ◀── audio gate (checks the generation) ◀┘
                                                                  │
                             every event ──▶ trace file + the live console in your browser
```

**LiveKit carries the phone audio, and nothing else.** AETHER uses LiveKit's low-level `rtc`
interface directly, deliberately not its agent framework. That framework brings its own recogniser,
model, voice and turn-taking, and would bypass the fencing being judged. Everything between the phone
line and the speaker is the same code the laptop-microphone path runs, so a fence proven on the
laptop is the same fence on a call.

**The console** shows the call as it happens: the transcript, which answers were cut off and never
spoken, the active Rime voice and language, the stale-leak count, and an *Interrupt* button that uses
the same fence as a spoken interruption.

The full component design is in [ARCHITECTURE.md](ARCHITECTURE.md), and the reasoning behind it in
[DESIGN.md](DESIGN.md).

---

## One hotel, three languages

The call opens by asking: *"Which language would you prefer: English, Hindi, or Spanish?"* — before
any greeting, so a Hindi speaker doesn't sit through English to be offered Hindi. The caller can also
switch mid-call by asking *"Can we switch language?"*.

AETHER both **understands** and **answers** in all three. There is **one** facts database, not one per
language: a Hindi caller asking *"चिकन कबाब कितने का है?"* reaches the same database row as an
English caller, and hears the same price. The Hindi and Spanish answers are rendered from that row —
with Hindi and Spanish numbers, times and grammar — and never phrased by a model.

| Language | Rime model | Voice | Recogniser |
|---|---|---|---|
| English | `mistv3` | `astra` | Whisper `base.en` |
| Hindi | `coda` | `nadi` | Whisper `base` (multilingual) |
| Spanish | `mistv3` | `isa` | Whisper `base` (multilingual) |

Hindi uses a different Rime model and voice because Rime's `mistv3` doesn't speak Hindi. Each voice
is its own connection, opened only when that language is first used, so an English-only call pays
nothing for the other two. First audio in the same measured run: English 320–339 ms, Spanish
353–360 ms, Hindi 372–449 ms. Hindi's is slower because of the provider's model, and it's measured.

---

## Bookings, and what can never be written

AETHER takes real bookings — rooms, restaurant tables, and cancellations. That means it writes to the
database, so it gives up as little as possible:

- **The hotel's facts can't be changed by any code path.** Prices, allergens, policies, room rates and
  room numbers are refused **by SQLite itself**, through an authorizer on the only writable connection.
  Only four places can be written: `reservations`, `table_bookings`, `guests`, and the single column
  `rooms.status`. [`tests/test_bookings.py`](tests/test_bookings.py) attempts nine forbidden writes and
  every one is refused.
- **A booking the caller abandons writes nothing.** The fence is checked before the booking runs.
  Moving that check after the booking fails seven tests.
- **Two callers can't both get the last room.** Each booking takes the database's write lock first.
  Tested with a thread per caller; without the fix, 13 bookings were made for 10 free rooms.
- **Practice never changes the demo.** The running system writes to a working copy
  (`data/aether_hotel.live.db`), never the committed database. `python scripts/reset_hotel_db.py`
  starts fresh.

The database itself — 50 rooms, 12 dishes, 27 policies, and the guests and bookings — is described in
[data/README.md](data/README.md).

---

## Speech output — Rime

Rime is the only voice AETHER has. There is no fallback voice, by design.

| | |
|---|---|
| **Model / speaker / language** | **English** `mistv3` / `astra` / `eng` · **Hindi** `coda` / `nadi` / `hin` · **Spanish** `mistv3` / `isa` / `spa` |
| **Endpoint** | `wss://users-ws.rime.ai/ws3` |
| **Transport** | WebSocket `/ws3`, one persistent connection per voice, reused across turns. `{"operation":"clear"}` stops speech mid-sentence when the caller interrupts |
| **Audio format** | `audioFormat=pcm`, `samplingRate=48000`, mono, 16-bit — the audio gate's own rate, so no decoding and no resampling |
| **Region** | Rime's default global host. No regional endpoint is configured |
| **Framework** | `websocket-client` over the raw `/ws3` protocol, in [`aether/audio/rime_ws.py`](aether/audio/rime_ws.py) |
| **HTTP fallback (disclosed)** | `RIME_TRANSPORT=http` switches to `https://users.rime.ai/v1/rime-tts` (MP3). Still Rime. Every spoken turn records which transport and voice produced it |

All three model, voice and language combinations were checked against Rime's live voice catalogue
on 2026-09-10. They were also synthesised over the shipped `/ws3` path the same day. The check
matters: Rime deleted the voice model this project first used for Hindi overnight.

---

## Measured results

| Measurement | Result | Where |
|---|---|---|
| Database answer: transcript to first sound, live, 75 turns | **337 ms** median | [RIME_EVIDENCE.md](RIME_EVIDENCE.md) |
| Answer that needed the language model, 6 turns | 1345 ms median | same |
| Rime, request to first audio | 337 ms median | same |
| Voice ducks when the caller starts speaking (synthetic mode) | **22 ms** median | same |
| Voice fully stops (synthetic mode) | 321 ms median (300 ms of it deliberate) | same |
| Speech recognition on a real phone line | 927 ms median | [evidence/demo-run.jsonl](evidence/demo-run.jsonl) |
| Stale results that reached a caller, 84 recorded runs | **0** | every trace in `traces/` |
| Turns answered with no language model, real phone demo | 11 of 16 | [evidence/](evidence/) |

`evidence/demo-run.jsonl` is a real inbound phone call. The worker log for the same call is committed
beside it, and every turn's latency matches between the two files. Newer traces record for themselves
whether their audio came from a phone or a laptop microphone. Numbers are dated where they're quoted,
and anything not yet measured is marked as such in [RIME_EVIDENCE.md](RIME_EVIDENCE.md), never
estimated.

---

## Third-party services

| Service | Used for | Credential |
|---|---|---|
| **Rime** | All speech output | `RIME_API_KEY` |
| **LiveKit Cloud** | The phone line: SIP, and the room that carries the call's audio | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| **Google Gemini** | Questions the database can't answer (`gemini-flash-lite-latest`) | `GEMINI_API_KEY` |
| Groq / Anthropic / OpenAI | Optional alternative language models, chosen with `LLM_PROVIDER` | that provider's key |

**Local, with no service and no key:** faster-whisper (speech recognition), webrtcvad (speech
detection), sounddevice / PortAudio (laptop microphone and speaker), and SQLite (the hotel database).

**No telemetry.** Traces are written to your own disk and nowhere else. The only data sent to a third
party is the text Rime speaks, the phone audio LiveKit carries, and — only for questions the database
can't answer — the caller's words sent to the language model.

---

## Failure behaviour

| Failure | What happens |
|---|---|
| The recogniser mishears a name | AETHER offers the nearest real thing: *"Did you mean the Deluxe King?"* A "yes" answers it; a "no" drops it without guessing again |
| Nothing intelligible was heard | *"Sorry, I did not catch that. Could you say it again?"* — never a guess |
| The caller says nothing after the greeting | After 8 s, once: *"Hello, are you there?…"*, and a warning in the log that the caller's audio isn't arriving |
| A language answer isn't understood twice | It carries on in English and says the caller can switch at any time — it never asks forever |
| A room that doesn't exist | *"We do not have a room nine nine nine. Our rooms are numbered one zero one to five one zero."* |
| Rime unreachable or refuses | The turn is discarded and recorded. Nothing else speaks in its place |
| Language model error or timeout | Discarded; nothing is spoken and nothing is remembered |
| A lookup or booking interrupted mid-way | Nothing is spoken and nothing is written |
| The caller hangs up | Everything in flight is fenced, and the trace is closed |
| The browser console disconnects | The call carries on. On reconnect the console gets the whole conversation back |

At the end of every call AETHER prints a **diagnosis** that names the first stage that produced
nothing: inbound audio, listening, speech detection, recognition, reply, voice or outbound audio.

---

## Known limitations

- **Phone audio quality decides recognition.** When a call's audio arrives clipped — noise
  suppression, a speakerphone, or a laptop Bluetooth link such as Windows Phone Link — only fragments
  of speech reach the recogniser. AETHER asks the caller to repeat and warns in the log, but it can't
  recover words that never arrived. Word accuracy on narrowband phone audio is not separately measured.
- **The speech-loudness threshold is calibrated from earlier calls.** Very quiet callers may need it
  adjusted (`AETHER_SPEECH_FLOOR`).
- **No echo cancellation on the laptop path.** Wear headphones, or AETHER hears itself.
- **Switching language a second time, while speaking Hindi or Spanish, is untested on a phone line.**
  The demo switches once per call.
- **One caller at a time is the tested case for conversation.** Simultaneous bookings are safe; full
  simultaneous calls haven't been run.
- **The Hindi and Spanish wording hasn't been reviewed by native speakers**, and the Hindi and
  Spanish voices haven't been judged by listening.
- **Not built:** partial-result reuse when a request is refined ("salvage"), and suspending one task to
  resume later. Both are stated in the tests that would cover them, which are skipped rather than
  faked.

---

## Repository map

```
aether/
  audio/         the audio gate, speech detection, the Rime /ws3 client
  classify/      the six interruption classes
  interruption/  barge-in: when a caller's speech fences an answer
  supervisor/    generation IDs and fencing
  hotel/         the database, the router, 23 tools, bookings, clarification, memory ("it")
  lang/          English, Hindi and Spanish: names, greetings, voices
  telephony/     the LiveKit phone worker
  web/           the live console
  spike.py       the turn loop both the phone and the laptop path run
scripts/         run_call.py, demo_full_call.py, reset_hotel_db.py, measurements
tests/           the test suite
data/            the hotel database
evidence/        a real phone call's trace and worker log
```

## Status

**1525 tests pass, 2 are skipped.** Both skips are features that genuinely don't exist, and each test
says which: the unsafe-mode control condition, and salvage.

## Documents

| File | What it's for |
|---|---|
| [SETUP.md](SETUP.md) | Every step to install and run AETHER on a new computer, and to record with Phone Link |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | The recorded demo, word for word, in all three languages |
| [DEMO.md](DEMO.md) | The demo runbook: flow, commands, and what to do when something fails |
| [JUDGING.md](JUDGING.md) | Each judging criterion mapped to the evidence behind it |
| [RIME_EVIDENCE.md](RIME_EVIDENCE.md) | The hard voice claim, acceptance test, procedure, results and limitations |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, the generation model, the event vocabulary |
| [DESIGN.md](DESIGN.md) | Why the system is shaped the way it is |
| [RULES.md](RULES.md) | The invariants, and the rules that enforce them |
| [PRD.md](PRD.md) | Product requirements and acceptance criteria |
| [PHASES.md](PHASES.md) | The build plan and its checkpoints |
| [MEMORY.md](MEMORY.md) | The engineering journal: decisions, what's measured, what isn't |
