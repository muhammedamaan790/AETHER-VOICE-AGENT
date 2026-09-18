# AETHER — the presentation script

What to say, in order, with the demo woven through it. Written to be **spoken**, not read off a
slide. Roughly **8 minutes** with the live call; cut marks show how to get to 5 or to 3.

**This is the talk track for presenting to judges.** The separate [DEMO_SCRIPT.md](DEMO_SCRIPT.md) is
the recording script for the video — every line AETHER says there is test-verified. Use this document
for the room, that one for the camera.

> **The one rule:** never claim more than the evidence. If asked something not measured, say
> *"I don't know — that isn't measured."* It costs nothing and buys everything.

---

## Before you stand up

```powershell
python scripts/reset_hotel_db.py     # rehearsal bookings and guesses gone
python scripts/run_call.py           # wait for: registered worker
```

**If AETHER sounds quiet on the line**, set `AETHER_OUTPUT_GAIN=1.6` in `.env` before starting the
worker. It is a multiplier on AETHER's voice only — it does not touch what the caller says, so it
cannot make AETHER hear itself. Above about 1.8 it gets loud rather than clear.

Open the console it prints, full screen, browser zoom 100%. Make a 20-second test call — say
**"English"**, then **"menu"** — and hang up. If AETHER says *"Hello, are you there?"*, your voice
isn't reaching it; fix the headset before you present.

Have these three things on screen or one keystroke away:

1. the **console** (the caller panel with the Voice line and "Stale leaks: 0"),
2. a terminal ready to run `python -m pytest -q`,
3. a terminal ready to run `python scripts/measure_understanding.py`.

---

## 0 · The hook — 30 seconds

> "A hotel's phone rings all day with the same twenty questions. What's on the menu, is there
> parking, how much is a suite, can I book a table for four. Answering them is somebody's whole job,
> and almost all of it is lookup.
>
> So build a voice agent. Everyone here has. **The part nobody gets right is what happens when the
> caller doesn't let the answer finish.**
>
> On a phone there is no screen. You can't tap, you can't scroll back, you can't see the answer
> you're talking over. People interrupt constantly — and by the time they've changed their mind, the
> old answer already exists. It's half-synthesised, or it's queued for the speaker. Cancelling it is
> a race, and a race can be lost.
>
> **AETHER doesn't race. It refuses to speak anything the caller has moved on from.** That is the one
> rule the whole system is built around, and everything I'm about to show you follows from it."

*[Pause. Then:]* "Let me call the hotel."

---

## 1 · The call — 3 minutes

*[Dial. Console goes from "Standing by" to "Call active".]*

**AETHER:** *Welcome to AETHER, your hotel manager. Which language would you prefer: English, Hindi,
or Spanish?*

> "Notice it asked before it greeted me. The greeting has to be in *some* language — greeting first
> would have chosen for the caller."

**YOU:** "English." → **AETHER:** *You've reached AETHER, the hotel's manager. How may I help you?*

### The ordinary case — point at the evidence strip

**YOU:** "What does the chicken kebab cost?"
**AETHER:** *The Chicken Kebab is four hundred and twenty rupees.*

> "`llm_ms` is zero. That price came out of the hotel's SQLite database through a template. **No
> language model was involved** — not instructed to be careful, *not in the path.* It can't quote a
> price the hotel doesn't charge because it's never asked."

**YOU:** "I'm allergic to nuts, what can I eat?"
**AETHER:** *If you are avoiding nuts, I would suggest Chicken Kebab, Butter Chicken and Vegetable
Biryani. One other dish on the menu contains nuts, so do check with me before you order.*

> "The one question where a confident guess could put someone in hospital. It never guesses — that's
> the single hard rule the model can't override."

### The interruption — this is the beat that matters

> "Now the hard part. Watch — I'm going to talk over it."

**YOU:** "Do you have anything vegetarian?"
**AETHER:** *Yes. For vegetarian we have Paneer Tikka, Paneer—*

*[Talk over it at normal volume. Don't wait. Don't press anything.]*

**YOU:** "Actually, how much is an executive suite?"

*[It stops mid-word. Point at the console: the vegetarian answer marked **discarded — never spoken**.]*

**AETHER:** *The Executive Suite is twelve thousand five hundred rupees a night, and sleeps up to
three.*

> "Three things happened there.
>
> It **ducked within about twenty milliseconds** and stopped completely in about three hundred — and
> it told Rime to clear its queue mid-sentence, which is why it stopped mid-word instead of finishing.
>
> The vegetarian answer was **fenced**. Permanently. It may well have finished computing — we don't
> care. Four independent layers check the generation before anything reaches your ear.
>
> And watch what happens next."

**YOU:** "Book it for two nights."
**AETHER:** *Done. I have reserved the Executive Suite, room three zero one, for two nights at twelve
thousand five hundred rupees a night. Your reference is one zero zero eight.*

> "**I said 'it'.** It booked the suite — the thing I actually heard — not the vegetarian list I
> talked over. Because a turn that was never spoken never becomes memory. The same rule covers the
> transcript, what 'it' refers to, and the booking itself. **A booking the caller abandons mid-way
> writes nothing to the database.**"

### Memory across turns — the part you can't fake

> "One question at a time is the easy case. A real call builds something up."

**YOU:** "I'll have the chicken kebab." → *I have added Chicken Kebab. That is Chicken Kebab, four
hundred and twenty rupees so far.*

**YOU:** "And two masala chai." → *I have added two Masala Chai. That is Chicken Kebab and two Masala
Chai, seven hundred rupees so far.*

> "**Listen to what I just said. 'And two masala chai.' No verb. I never said 'order'.**
>
> That exact sentence is a *price question* if you're reading the menu, and a *second item* if you're
> ordering. Nothing in the sentence tells you which. It knew because the order was open."

**YOU:** "Repeat my order." → *Your order is Chicken Kebab and two Masala Chai. That comes to seven
hundred rupees.*

> "And that's a database row, not the model remembering our conversation. Ask it ten times, get
> seven hundred rupees ten times, each in about a millisecond. A list carried in a model's context
> can come back one dish longer. A row can't."

**YOU:** "Can you remove one vegetable biryani?" → *I have taken off Vegetable Biryani. That leaves
Vegetable Biryani and Paneer Butter Masala, eight hundred and seventy rupees.*

**YOU:** "Replace the paneer butter masala with butter chicken." → *I have taken off Paneer Butter
Masala and added Butter Chicken. That leaves Vegetable Biryani and Butter Chicken, nine hundred and
ten rupees.*

> "**Remove *one*, not remove them all** — and a swap does both halves in one turn. Most of a real
> order is corrections, and this is where the worst bug we ever had lived: with no removal rule at
> all, 'remove the paneer' was read as an *order* for it, and a caller correcting their order
> watched it go one, two, four. A removal that adds is worse than no removal, because they're
> actively trying to fix it and every attempt makes it worse."

**YOU:** "What was my booking reference?" → *You have the Executive Suite, room three zero one, for
two nights, reference one zero zero eight.*

> "I booked that four turns and an entire food order ago. And I never gave a name or a phone number —
> on a phone line there's **nothing to look a booking up by** — so it remembers what this call did."

### And it stays on the hotel

**YOU:** "Who was Albert Einstein?"
**AETHER:** *(declines, offers to help with the hotel)*

> "It's a duty manager, not a search engine. A caller who dials a hotel and gets a physics lesson has
> reached the wrong number. But —"

**YOU:** "How do I get to you from the airport?" → *(answers, naturally)*

> "— that one it answers, and there's no database row for it. **The line is the hotel and the stay,
> not the database.** Getting that wrong in either direction is a real failure, and both ends are
> pinned by tests."

### The language switch

**YOU:** "Can we switch language?" → "Hindi."

*[Point at the Voice line changing from `astra · mistv3` to `nadi · coda`.]*

**AETHER:** *नमस्ते, आप AETHER से बात कर रहे हैं, होटल की मैनेजर।…*

**YOU:** "चिकन कबाब कितने का है" → *Chicken Kebab की कीमत चार सौ बीस रुपये है।*

> "**Four hundred and twenty rupees. The same number as in English — because it's the same database
> row.** Different renderer, different Rime model, different voice, same fact. And still no language
> model, which matters most in the language fewest people in this room can check."

*[Hang up.]*

---

## 2 · How it works — 2 minutes

*[One diagram, or just talk it.]*

```
Phone → LiveKit SIP → speech detection → Whisper → classifier
                                                      ↓
                                          router → SQLite tools      (~1 ms)
                                             └───→ Gemini, only if needed
                                                      ↓
                                              Rime over WebSocket → caller
```

> "Four things I'd point at.
>
> **One — the router is not a model.** Keyword and slot matching, deliberately conservative: it
> answers only when it's confident and hands everything ambiguous to Gemini. A wrong tool is worse
> than a slower answer. That's what makes a database answer about a millisecond instead of nearly two
> seconds.
>
> **Two — fencing is not cancellation.** A generation is active or fenced, monotonically, and it can
> never be un-fenced. Four layers check it: the tool runner, the model output, the Rime client, and
> the audio callback. Any one would mostly work. Four means a bug in one isn't a leak.
>
> **Three — bookings write, and almost nothing else can.** The one writable connection sits behind a
> SQLite authorizer that refuses every write outside six places. Every price, allergen and policy is
> refused *by the driver*, mid-statement, whatever the code asks for. A tool with a bug cannot reprice
> the menu.
>
> **Four — three languages, one database.** Not three databases and not a translation layer. Same
> row, same tool, three renderers. A Hindi price can't disagree with an English one."

---

## 3 · The evidence — 2 minutes

> "Everything I've claimed is measured, and the repository reproduces it with one command."

*[Run it live if you have the time — it takes about two minutes. Otherwise show the last run.]*

```bash
python -m pytest -q          # 1903 passed, 2 skipped
```

| | |
|---|---|
| **Stale results ever spoken** | **0**, across 95 recorded runs including real phone calls |
| Turn latency, real phone call | 1262 ms median |
| Rime first audio | 280 ms median |
| Database answer | ~1 ms |
| Routing, per language | **31/31 eng · 31/31 hin · 31/31 spa** |

*[Optionally run:]* `python scripts/measure_understanding.py`

> "Two things about the tests I'd actually defend.
>
> **The expectations are read from the database, not written down.** A test can't pass while the data
> says otherwise.
>
> **The demo script is a test.** The document we recorded from is parsed and replayed against a fresh
> hotel, one conversation, subject carried forward. If the suite passes, the sheet is what happens on
> camera. That exists because a price on our own demo sheet was wrong for months and would have been
> read aloud.
>
> And the guards are **mutation-tested** — we broke the code deliberately and confirmed the right test
> failed. Remove the write lock and the race test fails with *thirteen bookings for ten free rooms*."

---

## 4 · What we didn't build — 30 seconds

**Say this before a judge finds it.** It costs thirty seconds and buys the credibility of everything
else.

> "Three things we deliberately didn't do, and one we can't claim.
>
> **No automatic language detection** — detection on short phone audio flips mid-call, and a language
> that changes by itself on camera is a visible failure. The caller chooses.
>
> **No fallback voice** — Rime unconfigured means silence, not a substitute.
>
> **No salvage of partial work** — a fenced answer is discarded whole.
>
> And the one we can't claim: **the Hindi and Spanish templates haven't been reviewed by a native
> speaker.** The *facts* are provably right — same row, and the tests assert the numbers match. The
> fluency is not certified, and I'd rather say so than have you find out."

---

## 5 · Close — 20 seconds

> "AETHER is a hotel phone line where interrupting is safe. The abandoned answer is never spoken,
> never remembered, and never booked — over a real phone call, in three languages, from one database.
>
> Zero stale results have reached a caller across every run we've ever recorded.
>
> And every claim I've made reproduces with one command."

---

## Cutting it down

| You have | Keep |
|---|---|
| **8 min** | everything above |
| **5 min** | hook · the call (drop the language switch to one question) · fencing + authorizer only · the evidence table · what we didn't build |
| **3 min** | hook · interruption + "book it" · "and two masala chai" · "0 stale leaks, 1761 tests" · close |

**Never cut:** the interruption and the *"book it for two nights"* that follows — they are one beat
and the whole argument. And never cut **"and two masala chai"**: it is the single most interesting
sentence in the demo, because nothing in it says what it means.

---

## If it goes wrong on camera

| What happens | What to say, and do |
|---|---|
| AETHER says *"Hello, are you there?"* | *"It can't hear me — that's the line check working."* Hang up, redial. |
| *"Did you mean…?"* | Say **"yes"**. *"It offers the nearest real thing instead of guessing."* Keep rolling — this is a feature. |
| A turn shows `llm_ms` | *"That one the recogniser mangled, so it went to the model — which is exactly the fallback."* Don't point at the strip. |
| The vegetarian answer doesn't stop | Start talking earlier, at *"Paneer Tikka, Paneer"*. |
| *"And two masala chai"* returns a price | The previous turn didn't land. Say *"Order two masala chai"* and carry on. |
| The whole call fails | Switch to `python scripts/demo_full_call.py` — the entire call, no phone, no microphone, prints PASS. |

**The recovery line for anything:** *"That's a real phone line, and that's what real phone lines do —
here's the same call without the telephony."* Then run `demo_full_call.py`.
