<div align="center">

# AETHER

### A hotel's telephone line where interrupting is safe.

**English · हिन्दी · Español**

Multilingual hotel voice agent with deterministic hotel facts and generation-fenced real-time speech.

![stale outputs](https://img.shields.io/badge/stale%20outputs%20spoken-0%20in%20112%20runs-B3261E?style=flat-square)
![tests](https://img.shields.io/badge/tests-1994%20passing-0B6B58?style=flat-square)
![routing](https://img.shields.io/badge/multilingual%20routing-93%2F93-0B6B58?style=flat-square)
![turn latency](https://img.shields.io/badge/real--phone%20turn-1262%20ms%20median-454C57?style=flat-square)
![python](https://img.shields.io/badge/python-3.12-454C57?style=flat-square)

**[The moment that matters](#the-moment-that-matters)** ·
**[How it works](#how-aether-works)** ·
**[Evidence](#measured-not-estimated)** ·
**[Quick start](#quick-start)** ·
**[Judge Q&A](AETHER_FAST_REFERENCE.md#19-top-judge-questions)**

</div>

---

> ### Core invariant
>
> **A stale result must never become spoken output.**
>
> On a phone, audio cannot be un-spoken. If a caller interrupts, the answer they abandoned may
> already exist — half-synthesised, or queued for the speaker. AETHER does not race to cancel it. It
> refuses to speak it.

---

## How AETHER works

<div align="center">

<a href="docs/architecture.svg"><img src="docs/architecture.svg" width="100%"
     alt="AETHER system architecture, left to right: a PSTN caller through LiveKit SIP, turn
     detection and Whisper STT, an interruption classifier and generation supervisor, into a
     deterministic router that splits into a dominant SQLite tool path and a small Gemini fallback,
     then a per-language renderer, Rime TTS and back to the caller, overlaid with the five
     generation-fencing checkpoints."></a>

<sub><a href="docs/architecture.svg"><b>Click to enlarge</b></a></sub>

</div>

**Hotel facts → SQLite.** A router matches the sentence to one of 33 tools; a template speaks the row.
**Everything else → Gemini.** Reached only when no tool matches, and never for a price or a policy.
**Every abandoned generation → fenced**, at five checkpoints, before anything reaches the caller.

### Key numbers

| **0** | **1994** | **93 / 93** | **0.90 ms** |
|:-:|:-:|:-:|:-:|
| stale outputs spoken | tests passing | multilingual routes | deterministic answer |

| **5** | **33** | **3** | **1262 ms** |
|:-:|:-:|:-:|:-:|
| fence checkpoints | hotel tools | languages, one database | real-phone median turn |

<details>
<summary><b>How each of these is verified</b></summary>

<br>

| Claim | Why you should believe it |
|:--|:--|
| **0** stale outputs spoken, 112 recorded runs | `ResultLeaked` has never fired in [`traces/`](traces/) · [scenario A](tests/test_acceptance.py) asserts the fence *and* asserts the event absent |
| **5** independent fence checkpoints | Five distinct `ResultDiscarded` reasons in source — [`aether/tools/__init__.py`](aether/tools/__init__.py), [`aether/spike.py`](aether/spike.py) ×3, [`aether/audio/player.py`](aether/audio/player.py) · counted by [`scripts/metrics.py`](scripts/metrics.py) |
| **33** hotel tools, **0.90 ms** median answer | [`aether/hotel/tools.py`](aether/hotel/tools.py) · timed by `python scripts/measure_understanding.py` |
| **93 / 93** multilingual routing | [`tests/test_understanding.py`](tests/test_understanding.py) — which also enforces that all three languages are tested *equally* |
| Hotel facts never touch the LLM | `llm_ms = 0` on 11 of 16 turns in [`evidence/demo-run.jsonl`](evidence/demo-run.jsonl) |
| Nothing can reprice the menu | SQLite authorizer in [`aether/hotel/bookings.py`](aether/hotel/bookings.py) · [`tests/test_bookings.py`](tests/test_bookings.py) proves it by trying |
| A real phone call, **1262 ms** median turn | [`evidence/demo-run.jsonl`](evidence/demo-run.jsonl) + [`evidence/demo-call-worker.log`](evidence/demo-call-worker.log), asserted to be the same call |

Every public number above is derived from the repository by [`scripts/metrics.py`](scripts/metrics.py),
and [`tests/test_reference_integrity.py`](tests/test_reference_integrity.py) fails the suite if any
document disagrees with it.

</details>

---

## Why AETHER is different

| A typical voice agent | AETHER |
|---|---|
| The model answers most questions | Stored hotel facts bypass the model entirely — `llm_ms = 0` |
| Interruption is handled by cancelling in-flight work | Work is *fenced*: it may finish, and is then refused at five checkpoints |
| Interrupted output can still land in conversation memory | Only what the caller actually heard becomes memory |
| Tools hold broad database permissions | A SQLite authorizer refuses every write outside five tables and one column |
| One language pipeline, translated at the edges | One database, three renderers — a Hindi price and an English price are the same row |

---

## The moment that matters

Turns 5 to 7 of the scripted demo. Every reply below is asserted by
[`tests/test_demo_script.py`](tests/test_demo_script.py).

```text
CALLER   Do you have anything vegetarian?
AETHER   Yes. For vegetarian we have Paneer Tikka, Paneer Butter—

CALLER   Actually, how much is an executive suite?

         ── G13 FENCED · the vegetarian answer is discarded, never spoken ──

AETHER   The Executive Suite is twelve thousand five hundred rupees a night,
         and sleeps up to three.

CALLER   Book it for two nights.

AETHER   Done. I have reserved the Executive Suite, room three zero one, for two
         nights at twelve thousand five hundred rupees a night. Your reference is
         one zero zero eight.
```

The suite is booked because it is the last thing the caller actually heard. The interrupted
vegetarian answer never enters conversation memory, never becomes what *"it"* refers to, and leaves
no row behind. Generation ids are allocated per turn; `G13` here is illustrative of the numbering,
not a quotation from one specific run.

---

## AETHER in 60 seconds

1. A caller rings the hotel; LiveKit SIP carries the audio.
2. Speech detection finds the utterance and ends it on 500 ms of silence.
3. Groq Whisper transcribes it — and does nothing else.
4. A deterministic classifier decides what kind of interruption, if any, this was.
5. A keyword router matches the sentence to one of 33 hotel tools.
6. SQLite answers; a per-language template speaks the row. No model is consulted.
7. Gemini is reached only when no tool matches, and never for a stored fact.
8. If the caller interrupts, the current generation is fenced — and fenced work can never be spoken, remembered, or write a row.

---

## What it can do

| | Capabilities | For example |
|---|---|---|
| **Hotel** | Rooms · rates · availability · 27 policies · services | *"How much is an executive suite?"* · *"Do you have a swimming pool?"* |
| **Food** | Menu · prices · allergens · dietary filters | *"I'm allergic to nuts, what can I eat?"* |
| **Ordering** | Add · repeat · modify · place · cancel | *"And two masala chai."* · *"Repeat my order."* |
| **Transactions** | Room booking · table booking · cancellation | *"Book it for two nights."* · *"A table for four at eight."* |
| **Memory** | Pronouns · open order · booking references, across calls | *"What was my reference?"* · *"Check reference one zero zero eight."* |
| **Languages** | English · Hindi · Spanish, switchable mid-call | *"चिकन कबाब कितने का है?"* · *"¿Tienen piscina?"* |
| **Interruption** | Talk over any answer, at any moment | Six classes, told apart without a model |

---

## Quick start

```powershell
git clone https://github.com/muhammedamaan790/AETHER-VOICE-AGENT.git
cd AETHER-VOICE-AGENT
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env        # add your Rime, Gemini and LiveKit keys
```

| | |
|---|---|
| **A whole call, no phone, no microphone** | `python scripts/demo_full_call.py` |
| **Take real phone calls** | `python scripts/reset_hotel_db.py` then `python scripts/run_call.py` |
| **Talk to it on your laptop** | `python -m aether.web`, then **Start Listening**. Wear headphones |
| **Run the tests** | `python -m pytest -q` |
| **Re-derive every public number** | `python scripts/metrics.py` |

macOS, Linux, getting each key, connecting a phone number to LiveKit and recording the demo are all
in [SETUP.md](SETUP.md).

---

## Live console

The console shows the call as it happens: the transcript, which answers were cut off and never
spoken, the active Rime voice and language, the stale-leak count, and an *Interrupt* button that uses
the same fence as a spoken interruption.

**Judge mode** is a toggle in the header, off by default. It adds the live pipeline stage, a short
generation timeline showing `ACTIVE` and `FENCED`, whether the turn was answered deterministically
(with the tool that answered it) or by the model, and the latency breakdown. Every figure comes from
the same snapshot the rest of the page reads — a measurement the engine did not report renders as an
em dash, never as a zero, and [`tests/test_judge_mode.py`](tests/test_judge_mode.py) asserts the
panels carry no numbers of their own. Each panel has a small *Why?* naming the module and test behind
the property.

<!-- SCREENSHOTS: none are committed yet, and none are faked here. Two worth capturing by hand:
     1. the console mid-call with Judge mode ON, showing a FENCED generation in the timeline and a
        struck-through discarded answer in the transcript — save as docs/console-judge-mode.png
     2. optionally a short GIF of an interruption: answer playing, caller speaks, generation flips
        to FENCED — save as docs/interruption.gif
     Add them under this section once they exist. -->

---

## Measured, not estimated

| Measurement | Result | Environment |
|---|--:|---|
| Whole turn, speech ended to answer decided | **1262 ms** median | **Real phone** |
| Speech recognition | **927 ms** median | **Real phone** |
| Turns answered with no language model | **11 of 16** | **Real phone** |
| Database answer, transcript to first sound, 75 turns | **337 ms** median | Local pipeline |
| Answer that needed the language model, 6 turns | 1345 ms median | Local pipeline |
| Deterministic route and render | **0.90 ms** median | Local measured |
| Voice ducks when the caller starts speaking | 22 ms median | *Synthetic* |
| Voice fully stops | 321 ms median (300 ms of it deliberate) | *Synthetic* |
| Stale results that reached a caller, 112 recorded runs | **0** | Every trace in [`traces/`](traces/) |

[`evidence/demo-run.jsonl`](evidence/demo-run.jsonl) is a real inbound phone call; the worker log for
the same call is committed beside it and every turn's latency matches between the two files. The
environment column is not decoration — a synthetic duck latency and a real-phone turn latency are not
comparable, and mixing them would overstate the system. Anything not yet measured is marked as such
in [RIME_EVIDENCE.md](RIME_EVIDENCE.md), never estimated.

---

## One hotel, three languages

The call opens by asking which language the caller prefers — before any greeting, so a Hindi speaker
doesn't sit through English to be offered Hindi. They can also switch mid-call.

| Language | Rime model | Voice | Recogniser | First audio |
|---|---|---|---|--:|
| English | `mistv3` | `astra` | Whisper `base.en` | 320–339 ms |
| Spanish | `mistv3` | `isa` | Whisper `base` | 353–360 ms |
| Hindi | `coda` | `nadi` | Whisper `base` | 372–449 ms |

There is **one** facts database, not one per language. A Hindi caller asking *"चिकन कबाब कितने का है?"*
reaches the same row as an English caller and hears the same price, rendered with Hindi numbers and
grammar — never phrased by a model. Hindi needs a different Rime model because `mistv3` does not speak
it; each voice is its own connection, opened only when that language is first used.

---

## Where the LLM is allowed

| Caller asks | What happens | Cost |
|---|---|---|
| *"How much is the chicken kebab?"* | A route exists. The database answers through a template | `llm_ms = 0` |
| *"Is there a rooftop terrace?"* | No row holds it, but it is a hotel question. The model answers as the duty manager — **and the answer is written down** | one model call, then ~6 ms forever after |
| *"Who was Albert Einstein?"* | Declined in one sentence, with an offer to help with the hotel | one model call |
| *"Who is staying in room two zero one?"* | Declined. The tool that answers this reads **no guest record at all** | no lookup |

<details>
<summary><b>Learned answers — consistency, not truth</b></summary>

<br>

When the model answers a hotel question it couldn't look up, that answer goes into a `learned_answers`
table and the next caller asking the same thing gets it back verbatim, with no model call. Without
this, two callers asking about the pool get two different plausible answers and the hotel contradicts
itself.

But nothing verified there is a pool. So a learned answer:

- is stored `confirmed = 0` and **never merges into the hotel's facts** — it lives in its own database
  file (`data/aether_learned.db`, gitignored), so it can't be quoted as a price, a policy or an
  allergen, and can't be joined to one by accident;
- **never outranks the database** — a question with a route is answered from the route, always;
- is only written down **after the caller actually heard it**. A fenced turn is not remembered, by the
  same rule that governs conversation history;
- is matched **exactly**, not fuzzily. A near-miss costs a second; a bad fuzzy hit answers a question
  nobody asked.

```bash
python scripts/review_learned.py                        # what's been guessed, most-asked first
python scripts/review_learned.py --confirm "<question>" # agree
python scripts/review_learned.py --forget  "<question>" # disagree; the model tries again
```

Read the list as a to-do: a question asked six times that the database can't answer is a missing row.
Adding the real fact is better than confirming the guess, because a row is spoken in all three
languages and a learned answer is stored per language.

This is [`aether/hotel/learned.py`](aether/hotel/learned.py), rules R8b.7 and R8b.8 in
[RULES.md](RULES.md).

</details>

<details>
<summary><b>The line is the hotel and the stay, not the database</b></summary>

<br>

Directions from the airport, the neighbourhood, and ordinary courtesy are a duty manager's job and no
row holds any of them. Relativity isn't. Getting this wrong in either direction is a real failure —
too narrow and you rebuild the "that isn't in my records" agent, too wide and the hotel's phone line
is a search engine. Both ends are pinned by tests in
[`tests/test_llm_providers.py`](tests/test_llm_providers.py).

A guest's details are never given out: *"I cannot give out a guest's details. I can tell you whether a
room is free and when it frees up, if that helps."* The refusal reads no guest record at all, which is
enforced by inspecting the function's own source in the test suite rather than by trusting the author.

</details>

---

## What we do not claim

- **STT word accuracy on narrowband phone audio is not separately measured.** Phone audio quality
  decides recognition, and clipped audio loses words AETHER cannot recover.
- **Hindi and Spanish have never been exercised over a real phone line** — only through the full local
  pipeline.
- **Two simultaneous full calls have not been tested end to end.** Simultaneous *bookings* are safe and
  tested; concurrent calls are not.
- **No automatic language detection**, by choice — detection on short narrowband utterances can flip
  mid-call, and a language that changes by itself on camera is a visible failure.
- **No partial stale-result salvage.** AETHER holds no partial-result store, and the test that would
  cover it is skipped rather than faked.
- **The Hindi and Spanish wording has not been reviewed by native speakers**, and the shipped Hindi and
  Spanish voices have not been judged by listening.

The full list, including the two deliberately skipped tests and the live-microphone figures that are
still blank, is in [JUDGING.md](JUDGING.md#what-we-did-not-build-and-what-is-not-measured) and
[RIME_EVIDENCE.md](RIME_EVIDENCE.md).

---

## Under the hood

<details>
<summary><b>Generation fencing — the five checkpoints</b></summary>

<br>

Every request gets a generation ID. When the caller interrupts, the current generation is *fenced* —
permanently, and it can never be un-fenced. Five independent checkpoints check the generation before
anything reaches the caller's ear, and each announces itself with its own `ResultDiscarded` reason, so
the trace shows exactly which door stopped the work:

| # | Checkpoint | Reason in the trace | Stops |
|---|---|---|---|
| 1 | Tool runner | `stale_generation_tool` | a stale booking or order writing a row — checked *before* the tool body runs |
| 2 | Deterministic answer | `stale_generation_menu` | a hotel answer resolved from a row, then abandoned |
| 3 | Language model output | `stale_generation_llm` / `_stream` | a completed *or* half-streamed model reply |
| 4 | Rime client | *(courtesy `clear`)* | Rime doing synthesis nobody will hear |
| 5 | Audio gate | `stale_generation` | the last door: audio reaching the speaker |

Checkpoint 4 is a courtesy to Rime rather than the guarantee —
[`aether/audio/rime_ws.py`](aether/audio/rime_ws.py) says so in its own docstring. The audio gate is
the authority and refuses any chunk from a non-active generation.

**Everything the caller did not hear is forgotten.** Conversation history, what *"it"* refers to, a
*"did you mean…?"* offer and a booking are all committed only once the answer has actually been
spoken.

**The proof.** [`tests/test_full_duplex_acceptance.py`](tests/test_full_duplex_acceptance.py) is the
brief's own acceptance test, run against the real pipeline: a fixed delay is injected into a lookup,
the caller interrupts and changes part of the request, queued audio stops, the stale result is never
spoken, and the final answer is to the question they ended up asking.

</details>

<details>
<summary><b>Six kinds of interruption, told apart without a model</b></summary>

<br>

| Kind | Example | What AETHER does |
|---|---|---|
| Backchannel | *"mm-hm"*, *"okay"* | Keeps talking. Nothing is fenced |
| Refinement | *"actually…"*, *"I meant…"* | Fences the old answer, answers the correction |
| Replacement | anything else said over an answer | Fences the old answer, answers the new question |
| Status query | *"are you still there?"* | Leaves the task running |
| Cancel | *"stop"*, *"never mind"* | Fences, and starts nothing new |
| New task | a question when nothing is in flight | An ordinary turn |

Anything the classifier doesn't recognise **exactly** is treated as a replacement, which fences. So a
misclassification can cost a little responsiveness and can never cost correctness.

The moment the caller starts speaking, AETHER's voice drops in volume within about **22 ms**, and
stops completely once the speech is confirmed as meaningful, about **321 ms** after onset. Both are
medians measured in synthetic mode; the live-microphone figures aren't recorded yet. The 300 ms
confirmation is deliberate, so a *"mm-hm"* doesn't kill the answer it was encouraging.

</details>

<details>
<summary><b>Safe writes and bookings</b></summary>

<br>

AETHER takes real bookings — rooms, restaurant tables, and cancellations. That means it writes to the
database, so it gives up as little as possible:

- **The hotel's facts can't be changed by any code path.** Prices, allergens, policies, room rates and
  room numbers are refused **by SQLite itself**, through an authorizer on the only connection that may
  change the hotel. Six places can be written: `reservations`, `table_bookings`, `guests`,
  `restaurant_orders`, `restaurant_order_items`, and the single column `rooms.status`.
  [`tests/test_bookings.py`](tests/test_bookings.py) attempts nine forbidden writes and every one is
  refused. *It was four until AETHER learned to take an order — widening a guarantee this project
  states out loud is worth stating: two tables were named rather than the authorizer relaxed.*
- **A booking the caller abandons writes nothing.** The fence is checked before the booking runs.
  Moving that check after the booking fails seven tests.
- **Two callers can't both get the last room.** Each booking takes the database's write lock first.
  Tested with a thread per caller; without the fix, 13 bookings were made for 10 free rooms.
- **Practice never changes the demo.** The running system writes to a working copy
  (`data/aether_hotel.live.db`), never the committed database. `python scripts/reset_hotel_db.py`
  starts fresh.

The database itself — 50 rooms, 12 dishes, 27 policies, and the guests and bookings — is described in
[data/README.md](data/README.md).

</details>

<details>
<summary><b>Orders, and remembering what the caller just did</b></summary>

<br>

```text
CALLER   I'll have the chicken kebab.
AETHER   I have added Chicken Kebab. That is Chicken Kebab, four hundred and twenty rupees so far.
CALLER   And two masala chai.
AETHER   I have added two Masala Chai. That is Chicken Kebab and two Masala Chai, seven hundred
         rupees so far.
CALLER   Repeat my order.
AETHER   Your order is Chicken Kebab and two Masala Chai. That comes to seven hundred rupees.
CALLER   That's all, place the order.
AETHER   That is with the kitchen: Chicken Kebab and two Masala Chai, seven hundred rupees. Your
         order number is five zero zero three.
```

Every line of that is `llm_ms = 0`.

- **The order lives in the database, not in the model's context.** A list carried in the conversation
  is re-read every turn and can come back one dish longer; a row cannot. That is what makes *"repeat
  my order"* the same answer every time it is asked.
- **A dish's price is copied onto the order line when it is ordered**, so the total is what the caller
  was told even if the kitchen reprices overnight — and the menu row is still unwritable.
- **A sold-out dish is refused, by name.** An order must never disagree with what *"is the fish curry
  available?"* says.
- **A bare dish name means different things at different moments.** *"Two masala chai"* is a price
  question to someone browsing and another item to someone mid-order. Nothing in the sentence can tell
  them apart, so the session does.
- **What the caller never heard is never remembered.** Interrupt mid-dish and it does not join the
  order; interrupt mid-booking and *"what was my reference?"* correctly says you have not booked
  anything.

The same applies to bookings, which is where *"when will room three zero one be available?"* comes
from — it reads the reservation's check-out date, so it answers with a **date** rather than with *"it
is already reserved"*.

</details>

<details>
<summary><b>Speech output — Rime</b></summary>

<br>

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

All three model, voice and language combinations were checked against Rime's live voice catalogue on
2026-09-10, and synthesised over the shipped `/ws3` path the same day. The check matters: Rime deleted
the voice model this project first used for Hindi overnight.

</details>

<details>
<summary><b>Data flow and providers</b></summary>

<br>

| Layer | Technology |
|---|---|
| Telephony | LiveKit SIP |
| Speech recognition | Groq Whisper `whisper-large-v3-turbo` |
| Interruption classification | Deterministic Python — no model |
| Routing | Deterministic Python — no model |
| Hotel data | SQLite |
| LLM fallback | Google Gemini `gemini-flash-lite-latest` |
| Speech output | Rime `/ws3` |
| Runtime | Python 3.12 |

| Service | Credential |
|---|---|
| **Rime** — all speech output | `RIME_API_KEY` |
| **LiveKit Cloud** — SIP and the room carrying the call's audio | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| **Groq Whisper** — speech recognition, the production setting (`STT_PROVIDER=groq`) | `STT_API_KEY` |
| **Google Gemini** — questions the database can't answer | `GEMINI_API_KEY` |
| Groq / Anthropic / OpenAI — optional *alternative language models* (`LLM_PROVIDER`) | that provider's key |

**Groq appears twice and the two roles are unrelated.** `STT_PROVIDER=groq` is what production runs,
and it means Groq Whisper turns audio into text. `LLM_PROVIDER=groq` is a different switch that would
send *reasoning* to a Groq-hosted model; production uses Gemini for that. Groq never answers a hotel
question in either configuration — the router intercepts those before any model is reached.

**LiveKit, precisely.** AETHER *does* use the LiveKit Agents framework, but only for worker
registration, job dispatch and the warm job executor — `AgentServer` and `@server.rtc_session` in
[`aether/telephony/agent.py`](aether/telephony/agent.py). It deliberately does **not** use
`AgentSession`, which supplies its own recogniser, model, voice and turn-taking and would put the
interruption behaviour being judged inside somebody else's loop. The audio path is driven through the
low-level `rtc` primitives directly, so everything between the phone line and the speaker is the same
code the laptop-microphone path runs.

**Local, with no service and no key:** faster-whisper as the STT fallback, webrtcvad, sounddevice /
PortAudio, and SQLite. Recogniser selection is explicit — an unknown `STT_PROVIDER` raises rather than
silently substituting.

**No telemetry.** Traces are written to your own disk and nowhere else. The only data sent to a third
party is the text Rime speaks, the phone audio LiveKit carries, and — only for questions the database
can't answer — the caller's words sent to the language model.

</details>

<details>
<summary><b>Failure behaviour</b></summary>

<br>

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

</details>

<details>
<summary><b>Repository map</b></summary>

<br>

```
aether/
  audio/         the audio gate, speech detection, the Rime /ws3 client
  classify/      the six interruption classes
  interruption/  barge-in: when a caller's speech fences an answer
  supervisor/    generation IDs and fencing
  hotel/         the database, the router, 33 tools, orders, bookings, clarification, memory ("it")
  lang/          English, Hindi and Spanish: names, greetings, voices
  telephony/     the LiveKit phone worker
  web/           the live console
  spike.py       the turn loop both the phone and the laptop path run
scripts/         run_call.py, demo_full_call.py, metrics.py, reset_hotel_db.py, measurements
tests/           the test suite
data/            the hotel database
docs/            the architecture diagram
evidence/        a real phone call's trace and worker log
```

</details>

---

## Documents

| File | What it's for |
|---|---|
| [AETHER_FAST_REFERENCE.md](AETHER_FAST_REFERENCE.md) | Every verified fact on one sheet: architecture, metrics, limitations, claim-to-evidence map. Start here to answer a question quickly |
| [JUDGING.md](JUDGING.md) | Each judging criterion mapped to the evidence behind it |
| [RIME_EVIDENCE.md](RIME_EVIDENCE.md) | The hard voice claim, acceptance test, procedure, results and limitations |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | The recorded demo, word for word, in all three languages |
| [DEMO.md](DEMO.md) | The demo runbook: flow, commands, and what to do when something fails |
| [SETUP.md](SETUP.md) | Every step to install and run AETHER on a new computer |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, the generation model, the event vocabulary |
| [DESIGN.md](DESIGN.md) | Why the system is shaped the way it is |
| [RULES.md](RULES.md) | The invariants, and the rules that enforce them |
| [PRD.md](PRD.md) | Product requirements and acceptance criteria |
| [PHASES.md](PHASES.md) | The build plan and its checkpoints |
| [MEMORY.md](MEMORY.md) | The engineering journal: decisions, what's measured, what isn't |

<div align="center">
<sub><b>1994 tests pass, 2 are skipped.</b> Both skips are features that genuinely don't exist, and
each test says which: the unsafe-mode control condition, and salvage.</sub>
</div>
