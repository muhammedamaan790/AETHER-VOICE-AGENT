# AETHER, explained — everything you need to answer a judge

This is the document to read the night before. It explains what AETHER is, how every part works, why
each decision was made, and what it does *not* do. The last section is a Q&A of the hard questions,
with the answer and where to point.

**Rule for the room: never claim more than the evidence.** Every number here is measured and says
where it came from. If a judge asks something not covered, *"I don't know — it's not measured"* is a
better answer than a guess, and this project is built to make that answer rare.

---

## Contents

1. [The one-sentence version](#1-the-one-sentence-version)
2. [The problem, and why it has to be voice](#2-the-problem-and-why-it-has-to-be-voice)
3. [The hard problem: interruption](#3-the-hard-problem-interruption)
4. [How a call flows, end to end](#4-how-a-call-flows-end-to-end)
5. [The database, and why facts never reach the model](#5-the-database-and-why-facts-never-reach-the-model)
6. [Three languages from one database](#6-three-languages-from-one-database)
7. [Multi-turn memory: orders and bookings](#7-multi-turn-memory-orders-and-bookings)
8. [What it will and won't answer](#8-what-it-will-and-wont-answer)
9. [Rime](#9-rime)
10. [Telephony](#10-telephony)
11. [What's measured](#11-whats-measured)
12. [Testing, and how we know](#12-testing-and-how-we-know)
13. [What we did NOT build](#13-what-we-did-not-build)
14. [Hard questions, with answers](#14-hard-questions-with-answers)

---

## 1. The one-sentence version

**AETHER answers a hotel's telephone as its duty manager, in English, Hindi or Spanish — and it is
built around one invariant: a stale result must never become spoken output.**

The hotel is real in the sense that matters: 50 rooms, 5 room types, 12 dishes, 27 policies, 6
services, in a SQLite database that is the single source of truth. AETHER answers from it, books
against it, takes food orders into it, and never lets a language model near a price.

**Numbers to have ready**

| | |
|---|---|
| Tools | **33** — 25 read, 8 write |
| Tests | **1903 passing, 2 skipped**, one command |
| Routing accuracy | **31/31 in each of three languages** |
| Deterministic answer | **~1 ms** median (route + lookup + render) |
| First audio, real phone call | **280 ms** median |
| Whole turn, real phone call | **1262 ms** median |
| Stale results ever spoken | **0**, across 95 recorded runs |

---

## 2. The problem, and why it has to be voice

A hotel's phone rings all day with the same twenty questions: what's on the menu, is there parking,
how much is a suite, can I book a table. Answering them is a person's whole job, and most of it is
lookup.

**A phone call has no screen.** The caller cannot tap a menu, read a price list, scroll back to what
was said, or install anything. There is no fallback surface to degrade to — remove speech and there
is no product, not a worse one. That is what makes this a voice problem rather than a chatbot with a
microphone bolted on.

It is also what makes the engineering hard. On a phone, people **interrupt**, change their mind
mid-sentence, and talk over the answer — and there is no screen still showing the previous answer to
recover from when they do.

*Where to point:* `PRD.md` §1–§2, `README.md`.

---

## 3. The hard problem: interruption

> The hard part is not speech-to-text and not text-to-speech. It is what happens when a person
> talks **while the agent is thinking, looking something up, or speaking**.

By the time the caller has changed their mind, the old answer may already exist — half-synthesised,
or queued for the speaker. **Cancelling it is a race that can be lost.** So AETHER does not rely on
cancelling. It refuses to *speak* anything belonging to a request the caller has abandoned.

### Generation fencing

Every request gets a **generation ID**. When the caller interrupts, that generation is **fenced** —
permanently, and it can never be un-fenced. Fencing is not cancellation: the work may well finish,
and it still cannot be heard.

**Four independent layers** check the generation before anything reaches the caller's ear:

1. the **tool runner**, before the tool body runs;
2. the **language model** output, before it is sent to speech;
3. the **Rime client**, per chunk;
4. the **audio callback** that writes to the speaker.

Any one of them alone would mostly work. Four means a bug in one is not a leak.

### Six kinds of interruption, told apart without a model

| Kind | Example | What AETHER does |
|---|---|---|
| Backchannel | *"mm-hm"*, *"okay"* | Keeps talking. Nothing is fenced |
| Refinement | *"actually…"*, *"I meant…"* | Fences the old answer, answers the correction |
| Replacement | anything else said over an answer | Fences, answers the new question |
| Status query | *"are you still there?"* | Leaves the task running |
| Cancel | *"stop"*, *"never mind"* | Fences, starts nothing new |
| New task | a question when nothing is in flight | An ordinary turn |

Deterministic — no model, no confidence score. **Anything not recognised exactly is treated as a
replacement, which fences.** A misclassification can therefore cost a little responsiveness and can
never cost correctness.

### Everything the caller did not hear is forgotten

The same rule covers memory as well as sound. Conversation history, what *"it"* refers to, a
*"did you mean…?"* offer, a booking, a dish on an order — all committed **only once the answer has
actually been spoken**. Talk over an answer and it leaves no trace anywhere.

This is why turn 7 of the demo works: *"Book it for two nights"* books the suite from turn 6, not the
vegetarian list from turn 5 — turn 5 was talked over, never heard, and so never became the subject.

### Stopping the voice

The moment the caller starts speaking, AETHER's voice **ducks within ~22 ms**. It stops completely
once the speech is confirmed meaningful, **~321 ms** after onset, and tells Rime to clear its queue
mid-sentence — which is why it stops mid-word rather than finishing the sentence. Both medians are
**synthetic-mode** measurements; the live-microphone equivalents are not recorded.

The 300 ms confirmation is deliberate: it stops a *"mm-hm"* killing the answer it was encouraging.

*Where to point:* `aether/supervisor/generations.py`, `aether/interruption/`,
`tests/test_full_duplex_acceptance.py`, `tests/test_acceptance.py` A–H.

---

## 4. How a call flows, end to end

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
                                  └────────────────────────────────── caller hears the answer
```

**Turn by turn:**

1. **Speech detection** decides the caller has started and stopped talking.
2. **Whisper** transcribes. Provider is configurable (`STT_PROVIDER`): local `base.en`, or Groq-hosted
   `whisper-large-v3-turbo`.
3. **The classifier** decides which of the six interruption kinds this is, if anything is in flight.
4. **The router** tries to answer deterministically. If it is confident, it names a tool and
   arguments — **this is where most turns end**.
5. **The tool runner** checks the fence, runs the tool against SQLite, stamps identity on the result.
6. **The renderer** turns the result into a sentence in the caller's language.
7. **Rime** streams it back over a WebSocket, sentence by sentence.

**Gemini is only reached when the router returns `None`** — i.e. the database cannot answer. On the
demo call, 14 of 14 turns never touch it.

### The call opens by asking which language

The very first thing a caller hears is a **question**, not a greeting:

> *"Welcome to AETHER, your hotel manager. Which language would you prefer: English, Hindi, or
> Spanish?"*

The reason is ordering. The hotel greeting has to be spoken in *some* language, so greeting first
would already have chosen for the caller — a Hindi speaker would sit through an English greeting
before being offered Hindi. Once they answer, the **voice, recogniser, renderer and the model's own
instructions all change together**, and the real hotel greeting follows in their language.

---

## 5. The database, and why facts never reach the model

`data/aether_hotel.db` is the single source of truth: **50 rooms, 5 room types, 12 dishes, 27
policies, 6 services**, plus guests, reservations, table bookings and restaurant orders.

**A question the database can answer never reaches the model at all.** Three reasons, in order of
importance:

1. **Latency.** A menu fact through Gemini measured ~1.8 s of model time on top of speech. A tool
   lookup is ~1 ms. On a phone call that is the difference between a conversation and a wait.
2. **Correctness.** A model asked to phrase `{"price": 420}` can still say the wrong number. It
   cannot, if it is never asked.
3. **Determinism.** The demo answers identically every rehearsal.

This is a **mechanism, not an instruction**. It is not a prompt asking the model to be careful; the
model is not in the path.

### What can be written, and what cannot

Taking bookings means giving up "read-only". The honest thing is to give up as little as possible, so
the one writable connection installs a **SQLite authorizer** that denies every write outside six
places:

```
reservations              a room booking
table_bookings            a restaurant booking
guests                    the caller, so a booking has someone to belong to
restaurant_orders         an order being taken, then sent to the kitchen
restaurant_order_items    the dishes on it, at the price they were ordered at
rooms.status              the one column a booking changes
```

Every price, allergen, policy, rate and menu item is refused **by the driver**, mid-statement,
whatever the code asks for. A tool with a bug cannot reprice the menu; nor can a model, which never
reaches that class at all. `tests/test_bookings.py` attempts nine forbidden writes and every one is
refused.

> It was four places until AETHER learned to take orders. We widened it deliberately and said so —
> two tables were **named**, not the authorizer relaxed.

### The router

Keyword-and-slot matching, not a model, and **deliberately conservative**: it answers only when
confident and returns `None` otherwise, so anything ambiguous reaches the model rather than being
guessed at. A wrong tool is worse than a slower answer.

It is also hardened against what the recogniser actually does to speech. *"Suite"* is pronounced
"sweet", so `base.en` returns `suit` or `sweet`; *"dessert"* came back as `Desert` on a real call and
the model — with no menu in front of it — invented three desserts and three prices that do not exist.
Both are now handled, and the menu is injected into the model's prompt so a router miss costs a
slower answer instead of a fabrication.

---

## 6. Three languages from one database

**One facts database, three renderers.** Not three databases, not a translation layer over English
answers, and not a model asked to translate.

```
                     ┌─ tools.py    → English sentence
SQLite row ─ tool ───┼─ tools_hi.py → Hindi sentence
                     └─ tools_es.py → Spanish sentence
```

The tool, the router, the runner and the database are identical for all three. **Only the final
string differs.** That is why a Hindi price cannot disagree with an English one — it is the same row.
And `llm_ms` stays 0 in every language, which matters most in the language fewest people in the room
can check.

### Understanding, not just answering

AETHER *answered* in three languages from the first day it spoke them, but for a while it only
*understood* one: `route()` matches English keywords, so a Hindi caller fell through to the model. The
answer still came back in Hindi — which is exactly why nobody noticed — but it came from Gemini
rather than the database.

`aether/hotel/_foreign.py` fixes that by rewriting **vocabulary** into the English the router already
keys on. Not a second router: every routing decision is tested once, in English, and is worth keeping
exactly once.

**Measured:** 31 sentences per language across four capabilities — **eng 31/31, hin 31/31, spa
31/31**. Run it yourself: `python scripts/measure_understanding.py`.

### Hindi is a different Rime model and a different voice

Rime's public catalogue says `mistv3` speaks eng/fra/ger/spa and that `astra` speaks no Hindi on any
model. So Hindi is `coda`/`nadi` — a different model, a different voice, and (because speaker, model
and language are baked into the `/ws3` connect URL) **a second socket**, opened lazily so an English
call never pays for it.

**The honest cost: Hindi is roughly 3× slower to first audio than English** (~1.5 s vs ~0.4 s). It is
recorded rather than hidden, and it is why the language switch is opt-in.

---

## 7. Multi-turn memory: orders and bookings

One question at a time is the easy case. A real call builds something up.

```
CALLER   I'll have the chicken kebab.
AETHER   I have added Chicken Kebab. That is Chicken Kebab, four hundred and twenty rupees so far.
CALLER   And two masala chai.
AETHER   I have added two Masala Chai. That is Chicken Kebab and two Masala Chai, seven hundred
         rupees so far.
CALLER   Repeat my order.
AETHER   Your order is Chicken Kebab and two Masala Chai. That comes to seven hundred rupees.
```

Three things are worth saying out loud about that:

**"And two masala chai" carries no verb.** It is a price question to somebody browsing the menu and a
second item to somebody mid-order, and *nothing in the sentence tells you which*. The open order
does. The router takes an `ordering` flag rather than reading session state itself, so routing stays
a pure function of what was said plus what the session knows.

**The order lives in the database, not in the model's context.** A list carried in the conversation
is re-read by the model every turn and can come back one dish longer. A row cannot. Ask "repeat my
order" ten times and get the same seven hundred rupees ten times, each in ~1 ms.

**The price is copied onto the order line when the dish is ordered.** If the kitchen reprices
overnight, an order taken today still totals what the caller was told — and the menu row is still
unwritable.

### Correcting an order

Most of a real order is corrections, and this is where the worst bug in the project's history lived:
with no removal rule at all, *"remove paneer butter masala"* was read as an **order** for it, and a
caller trying to correct their order watched it go from one to two to four of the dish they were
removing. A removal that adds is worse than no removal, because the caller is actively correcting it
and every attempt makes it worse.

| Caller says | What happens |
|---|---|
| *"Remove the biryani"* | the whole line comes off |
| *"Remove **one** biryani"* | one comes off, the rest stay |
| *"Replace the paneer butter masala with butter chicken"* | both halves, one turn |
| *"I also ordered two biryani, where is it?"* | reads the order back — never adds |

A stated number is honoured and a missing number means the whole line. Those were the same value
until a caller asked to remove one of their two biryani and lost both.

**And what cannot be served is said, with what can.** Three reasons deserve three answers: sold out
(*"the Fish Curry is off today"*), misheard (*"we do not have chiken kebap, but we do have the
Chicken Kebab"* — through the same `clarify.nearest` the router uses), and not on the menu at all
(*"we do not have naan — we do have starters, mains, vegetarian mains, desserts and drinks"*). The
detection is deliberately timid: it ignores anything a known dish covers, drops words that follow a
number without naming food, skips anything the hotel *does* have, and says nothing when unsure.

### And what this call has already booked

A telephone caller gives no name and no number, so a booking made on the call has **nothing to be
looked up by**. It is remembered on the session:

- *"What was my booking reference?"* → the booking from four turns ago
- *"When is my table booked for?"* → party size and sitting
- *"Cancel my reservation"* → cancels it, without a reference

A reference is normally **required** to cancel, because cancelling the wrong booking is not
recoverable by saying sorry. *"My"* is the one safe exception: it is this call's booking, and there is
exactly one of it. With no booking on the call, it asks for a reference exactly as before.

### And it obeys the golden invariant

Interrupt mid-dish and it does not join the order. Interrupt mid-booking and *"what was my
reference?"* correctly says you have not booked anything — which is the right answer, because telling
the caller they have a table that was never booked is worse than forgetting.

---

## 8. What it will and won't answer

Three questions, three behaviours, and the difference between them is the whole design.

| Caller asks | What happens | Cost |
|---|---|---|
| "How much is the chicken kebab?" | A route exists. The database answers through a template. | `llm_ms = 0` |
| "Is there a rooftop terrace?" | No row holds it, but it's a hotel question. The model answers as the duty manager — **and the answer is written down**. | one model call, then ~6 ms forever |
| "Who was Albert Einstein?" | Declined in one sentence, with an offer to help with the hotel. | one model call |

**The line is the hotel and the stay, not the database.** Directions from the airport, the
neighbourhood and ordinary courtesy are a duty manager's job and no row holds any of them. Relativity
is not. Getting this wrong in either direction is a real failure — too narrow and you rebuild the
*"that isn't in my records"* agent; too wide and the hotel's phone line is a search engine. **Both
ends are pinned by tests.**

**Remembering buys consistency, not truth.** When the model answers a hotel question it could not
look up, the answer goes into a `learned_answers` table and the next caller asking the same thing
gets it back verbatim with no model call. Without this, two callers asking about the terrace get two
different plausible answers and the hotel contradicts itself.

But nothing verified there *is* a terrace. So a learned answer:

- is stored `confirmed = 0` and lives in **its own database file** — it cannot be joined to a price;
- **never outranks the database** — a question with a route is answered from the route, always;
- is only written down **after the caller actually heard it**;
- is matched **exactly**, not fuzzily.

`python scripts/review_learned.py` shows a manager what has been guessed, most-asked first, to
confirm or forget. Read it as a to-do list: a question asked six times that the database cannot
answer is a missing row.

---

## 9. Rime

**Rime is the only voice.** There is no fallback TTS: unconfigured means silence, not substitution.

| | | |
|---|---|---|
| Model | `mistv3` | chosen by measurement against `mistv2` |
| Voice | `astra` | verified against the live catalogue |
| Language | `eng`, plus `hin` (`coda`/`nadi`) and `spa` (`mistv3`/`isa`) | a second voice, not a parameter |
| Transport | **WebSocket `/ws3`**, persistent | streaming, and stoppable mid-utterance |
| Audio | `pcm` at **48 kHz** | the gate's own rate — no MP3 decode, no resample |
| First audio | **280 ms** median on a real call | `evidence/demo-run.jsonl` |

Two details worth volunteering:

**`{"operation":"clear"}`** — when the caller interrupts, AETHER tells Rime to clear its queue
mid-utterance. That is *why* it stops instantly rather than finishing the sentence.

**Nothing about Rime was assumed.** Endpoint, model, voice and language were each verified against
Rime's live public catalogue. The first Hindi choice, `arcana`/`anaya`, had to be abandoned: **Rime
deleted the entire `arcana` model between 2026-09-08 and 2026-09-09** (863 catalogue entries down to
594). It still answered after being delisted — which is precisely the trap this project refuses, an
undocumented endpoint that merely happens to respond.

---

## 10. Telephony

Real PSTN calls through **LiveKit SIP**. Two things were calibrated from real call audio rather than
guessed:

- **The speech floor moved from 35 to 2500** on 28 pooled utterances across 3 calls. Telephone audio
  is quieter and noisier than a laptop microphone, and the original floor triggered on line noise.
- **One track produces exactly one pump**, and a second subscription is refused — visible in the
  committed worker log.

`evidence/demo-run.jsonl` and `evidence/demo-call-worker.log` are the same 16-turn call recorded two
ways, committed and secret-audited.

---

## 11. What's measured

Every figure below is from a named run. Nothing here is estimated.

| What | Figure | Where |
|---|---|---|
| Turn latency, real phone call | **1262 ms** median | `evidence/demo-run.jsonl` |
| Rime first audio, real call | **280 ms** median | same |
| Speech-to-text, telephone audio | **927 ms** median | same |
| Deterministic path (route + tool + render) | **~1 ms** median | `scripts/measure_understanding.py` |
| Model turn, when used | **0.9–1.8 s** | same call |
| Duck on speech onset | **22 ms** median | synthetic mode |
| Full stop after confirmed speech | **321 ms** median | synthetic mode |
| Routing accuracy, per language | **31/31 × 3** | `scripts/measure_understanding.py` |
| Stale results spoken | **0** across 95 recorded runs | `traces/` |

**Be precise about the last one in the room:** zero leaks across every trace ever recorded, including
real phone calls. That is the golden invariant holding on real audio.

---

## 12. Testing, and how we know

**1903 passing, 2 skipped**, across 49 test files, in one command:

```bash
python -m pytest -q
```

Both skips are features that genuinely do not exist, documented in the test body.

Three things about the tests are worth saying to a judge, because they are unusual:

**Expectations are read from the database, not written down.** A test cannot pass while the data says
otherwise. `tests/test_hotel_db.py` derives every expected answer from SQLite.

**The demo script is a test.** `DEMO_SCRIPT.md` quotes what AETHER says, and
`tests/test_demo_script.py` parses the document and replays the whole call — one conversation, one
store, subject carried forward, against a fresh hotel. **If the suite passes, the sheet is what will
happen on camera.** This exists because `DEMO.md` once carried a price that had been wrong for
months and would have been read aloud.

**Guards are mutation-tested.** A test that cannot fail is worse than no test. Every important
guarantee here has been verified by deliberately breaking the code and confirming the right test
failed — moving the fence check after the tool body, removing `BEGIN IMMEDIATE`, declaring a write
non-mutating, removing the authorizer. The race test fails with *"13 bookings for 10 free rooms"*.

---

## 13. What we did NOT build

Say these before a judge finds them. Each is a decision, not an oversight.

- **No automatic language detection.** Detection on short narrowband utterances can flip mid-call, and
  a language that changes by itself on camera is a visible failure. The caller chooses.
- **No salvage of partial work.** A fenced generation is discarded whole. Level 1 memory: a turn is
  remembered whole or not at all.
- **No fallback TTS.** Rime unconfigured means silence, not a substitute voice.
- **No account tier or rate-limit verification** — not exposed by Rime's public catalogue.
- **No native-speaker sign-off** on the Hindi and Spanish templates. They are written to be reviewed;
  where they are wrong, they are wrong in one line of one file rather than in a model's output.
- **The newest features have not been through a real phone.** Ordering, booking memory and the
  routing measurements are verified through the real pipeline with recorded/synthetic audio. The
  committed phone evidence predates them.

---

## 14. Hard questions, with answers

**"Isn't this just a chatbot with a voice?"**
No, and the difference is the invariant. A chatbot has no concept of an answer that must not be
spoken. Our whole architecture exists because on a phone the caller can abandon a request *while the
answer is being produced*, and we refuse to speak it rather than racing to cancel it. Point at the
console marking an answer *discarded — never spoken*.

**"Why not let the LLM do the routing? It'd be more flexible."**
Three reasons, and latency is the least interesting. A model in the path costs ~1.8 s per turn; it can
phrase a stored price as the wrong number; and it is non-deterministic, so the demo would not answer
the same way twice. The router is conservative — it answers only when confident and hands everything
ambiguous to the model, which is exactly the right division.

**"What happens if the recogniser mishears?"**
Three defences, all from real failures. The router carries repairs for what `base.en` actually
returns (*suit*/*sweet* for "suite", *Desert* for "dessert"). Anything it isn't sure about falls
through to the model rather than being guessed. And the real menu is injected into the model's prompt,
so a router miss costs a slower answer instead of an invented dish — which happened once, and is why
the prompt changed.

**"How do I know the Hindi is right and not machine-translated nonsense?"**
Two separate claims. The **facts** are provably right: same database row, same tool, different
renderer, and `tests/test_language.py` asserts the Hindi answer carries the same numbers as the
English. The **fluency** is not certified — I'd want a Hindi speaker to review the templates, and
that is listed as an open item rather than claimed.

**"Your agent booked a room. What if it books the wrong one, or two people book the same room?"**
Both are tested. Two callers cannot get the last room: each booking takes SQLite's write lock before
the room is chosen, and the claim is conditional on the room still being free. Mutation-tested —
without the fix, 13 bookings for 10 free rooms. And a booking the caller abandons mid-way writes
nothing, because the fence check runs before the tool body rather than after.

**"What stops it inventing a price or an allergen?"**
For anything with a database route: the model is never asked. For allergens specifically there is a
second rule — never guess, even when the model could. A wrong opening time is corrected next call; a
wrong allergen answer is not.

**"What stops someone ringing up and getting a guest's details?"**
Nothing in the database prevents it — which is exactly why the product has to. `reservation_for_room`
returns dates and status and never the name, and any question about *who* is declined outright:
*"I cannot give out a guest's details. I can tell you whether a room is free and when it frees up."*
The refusal reads no guest record at all, because the answer does not depend on one. A hotel line
answers to whoever dials it, and a name read back to an unauthenticated caller is a disclosure the
schema makes easy and the product should not make casual.

**"Your agent made something up about a rooftop terrace and saved it. Isn't that dangerous?"**
It would be if we treated it as a fact. We deliberately do not. It is stored unconfirmed, in a
separate database file, it can never outrank a real row, and a manager reviews it with
`scripts/review_learned.py`. What it buys is **consistency** — the hotel not contradicting itself
between two callers — and we are careful to claim only that.

**"Why can't it answer a general question? Every other assistant can."**
Because a duty manager is not a search engine. A caller who dials a hotel and gets a physics lesson
has reached the wrong number. The line is the hotel and the stay — which still includes airport
directions and local restaurant recommendations that no row holds. That was a deliberate reversal
after live probing, and both ends are pinned by tests so it cannot drift either way.

**"Show me it isn't scripted."**
Ask it something not on the sheet. The fallback bank in `DEMO_SCRIPT.md` has a dozen more real
questions, or invent one — a room number that doesn't exist, a dish that doesn't exist, an order of
three of something. The suite is 1761 tests, not 14.

**"What's the weakest part?"**
The recogniser on Hindi. Local `base.en` cannot transcribe Hindi at all, and the multilingual local
model returns Urdu script and romanised transliteration — measured at 21.7% accuracy against 89.2%
for the hosted Groq model. That is why `STT_PROVIDER` exists. It's a real limitation and it's
measured rather than hidden.

**"How long did this take, and what would you do next?"**
Next would be: a native-speaker pass on the Hindi and Spanish templates, phone-verifying the ordering
path end to end, and filling the remaining evidence placeholders in `RIME_EVIDENCE.md`. All three are
listed as open rather than quietly skipped.
