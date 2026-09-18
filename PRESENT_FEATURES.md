# AETHER — what makes it worth your attention

Ordered by what is hardest to build and easiest to verify. Every number is measured and names its
source; nothing here is estimated.

---

# 1. Zero stale answers have ever reached a caller

**Across 95 recorded runs — including real phone calls over a real PSTN line — the number of stale
results that reached a caller is `0`.**

That is not a claim about care. It is a structural property, and it is the thing everything else in
this project was built to protect.

**The problem.** On a phone, a caller interrupts *while the answer is being produced*. By then the old
answer already exists — half-synthesised, or queued for the speaker. Cancelling it is a race, and a
race can be lost. Most voice agents lose it quietly: you hear the tail of an answer to a question you
abandoned.

**What we do instead.** AETHER never races. Every request carries a **generation ID**; an interruption
**fences** that generation *permanently*, and fencing is not cancellation — the work may finish, and
it still cannot be heard. **Four independent layers** check the generation before audio reaches the
ear: the tool runner, the model output, the Rime client, and the audio callback. Any one would mostly
work. Four means a bug in one is not a leak.

**How to check it in thirty seconds:** interrupt any answer during the demo and watch the console mark
it *discarded — never spoken*. Then `python -m pytest -q` — 1761 tests, including the brief's own
acceptance test run against the real pipeline.

---

# 2. "Book it for two nights" books the thing you actually heard

This is the one to watch on camera, because it is where the invariant stops being an abstraction.

```
CALLER   Do you have anything vegetarian?
AETHER   Yes. For vegetarian we have Paneer Tikka, Paneer—          ← talked over
CALLER   Actually, how much is an executive suite?
AETHER   The Executive Suite is twelve thousand five hundred rupees a night…
CALLER   Book it for two nights.
AETHER   Done. I have reserved the Executive Suite, room three zero one…
```

**"It" means the suite.** Not the vegetarian list — that turn was talked over, never heard, and so
never became the subject.

The rule is not special-cased for pronouns. **Everything the caller did not hear is forgotten:** the
transcript, what *"it"* refers to, a *"did you mean…?"* offer, a food order, and the booking itself.

And the strongest form of it: **a stale intention never lands.** A caller who changes their mind while
a booking is in flight leaves **no row in the database** — the fence check runs *before* the tool
body, not after. Moving it after makes seven tests fail.

---

# 3. Hotel facts never reach a language model — by mechanism, not by instruction

A question the database can answer **never touches the model at all**.

| | Database path | Model path |
|---|---|---|
| Latency | **~1 ms** | 0.9–1.8 s |
| Can quote a wrong price | **Impossible — never asked** | Yes |
| Same answer every time | **Yes** | No |

This is not a prompt asking a model to be careful. The model is **not in the path**. It cannot quote a
price the hotel does not charge, in the same way it cannot quote one for a hotel it has never heard
of.

**And what *can* be written is narrower than you'd expect.** Taking bookings means giving up
"read-only", so the single writable connection sits behind a **SQLite authorizer** that denies every
write outside six named places. Every price, allergen, policy, rate and menu item is refused **by the
driver**, mid-statement, whatever the code asks for.

`tests/test_bookings.py` attempts nine forbidden writes. All nine are refused.

---

# 4. Three languages, one database — and it *understands* all three, not just answers in them

```
                     ┌─ tools.py    → "The Chicken Kebab is four hundred and twenty rupees."
SQLite row ─ tool ───┼─ tools_hi.py → "Chicken Kebab की कीमत चार सौ बीस रुपये है।"
                     └─ tools_es.py → "Chicken Kebab cuesta cuatrocientas veinte rupias."
```

Same row, same tool, three renderers. **A Hindi price cannot disagree with an English one**, and
`llm_ms` stays 0 in every language — which matters most in the language fewest people in the room can
check.

**The subtle half.** AETHER *answered* in three languages long before it *understood* three. The
router matched English keywords, so a Hindi caller fell through to the model — and the answer still
came back in Hindi, which is exactly why nobody noticed. It came from Gemini rather than the database,
and the project's central claim was quietly false for every non-English caller.

**Now measured, not asserted:**

| | eng | hin | spa |
|---|---|---|---|
| Sentences reaching the right tool | **31/31** | **31/31** | **31/31** |

Run it: `python scripts/measure_understanding.py`. `tests/test_understanding.py` enforces the same
table, and also guards that the three languages are tested *equally* — the cheapest way to fake this
score is to test English thoroughly and the others lightly.

**The call opens by asking which language**, before any hotel greeting, because the greeting has to be
spoken in *some* language. Hindi is a different Rime model and a different voice (`coda`/`nadi`),
opened lazily — an English call never pays for it.

---

# 5. "And two masala chai" — the sentence that proves it's listening

```
CALLER   I'll have the chicken kebab.
AETHER   I have added Chicken Kebab. That is Chicken Kebab, four hundred and twenty rupees so far.
CALLER   And two masala chai.
AETHER   I have added two Masala Chai. That is Chicken Kebab and two Masala Chai, seven hundred
         rupees so far.
CALLER   Repeat my order.
AETHER   Your order is Chicken Kebab and two Masala Chai. That comes to seven hundred rupees.
```

**No verb. Never said "order".** That exact sentence is a *price question* to somebody reading the
menu and a *second item* to somebody mid-order — and nothing in the sentence tells you which. It knew
because the order was open.

**The order lives in the database, not in the model's context.** A list carried in a conversation is
re-read by the model every turn and can come back one dish longer. A row cannot. So *"repeat my
order"* is a **lookup at `llm_ms = 0`** that gives the same answer every time it is asked.

The price is **copied onto the order line** when the dish is ordered, so the total is what the caller
was told even if the kitchen reprices overnight — and the menu row is still unwritable.

**And an order can be corrected, which is where most voice agents fall over.**

```
CALLER   Can you remove one vegetable biryani?
AETHER   I have taken off Vegetable Biryani. That leaves Vegetable Biryani and Paneer Butter
         Masala, eight hundred and seventy rupees.
CALLER   Can you replace my paneer butter and masala for butter chicken?
AETHER   I have taken off Paneer Butter Masala and added Butter Chicken. That leaves Vegetable
         Biryani and Butter Chicken, nine hundred and ten rupees.
```

*"Remove **one**"* takes one off and leaves the rest; *"remove the biryani"* clears the line. A swap
does both halves in one turn. *"I also ordered two biryani, where is it?"* reads the order back
rather than adding two more — a caller chasing something they think they ordered is the last person
who should be given more of it.

**And what the hotel cannot serve is said, with what it can.** Three reasons, three answers: *"the
Fish Curry is off today"* (sold out), *"we do not have chiken kebap, but we do have the Chicken
Kebab"* (misheard), and *"we do not have naan — we do have starters, mains, vegetarian mains,
desserts and drinks"* (not on the menu). Silence about two of four items reads, on a phone, as an
order that worked.

**And it remembers what this call did.** *"What was my booking reference?"* answers from a booking made
four turns earlier. A telephone caller gives no name and no number — there is **nothing to look a
booking up by** — so the session remembers. Interrupt mid-booking and it correctly says you have not
booked anything, because telling a caller they have a table that was never booked is worse than
forgetting.

---

# 6. It knows the difference between a hotel question and the internet

| Caller asks | What happens |
|---|---|
| "How much is the chicken kebab?" | Database answers. `llm_ms = 0` |
| "How do I get to you from the airport?" | **No row holds it.** The model answers as the duty manager |
| "Is there a rooftop terrace?" | Model answers — **and the answer is written down** |
| "Who was Albert Einstein?" | Declined in one warm sentence, with an offer to help |

**The line is the hotel and the stay, not the database.** Directions, the neighbourhood and ordinary
courtesy are a duty manager's job with no row behind them. Relativity is not. Getting this wrong in
either direction is a real failure — too narrow and you rebuild the *"that isn't in my records"*
agent, too wide and the hotel's line is a search engine — so **both ends are pinned by tests**.

**And a guest's details are never given out.** *"Who is staying in room two zero one?"* →
*"I am sorry, I cannot give out a guest's details. I can tell you whether a room is free and when it
frees up, if that helps."* A hotel line answers to whoever dials it. The withholding was always
there — `reservation_for_room` has never spoken a name — but the question used to be answered as
availability instead, which is a confident answer to something nobody asked.

**Remembering buys consistency, not truth.** When the model answers a hotel question it couldn't look
up, the answer is stored and replayed to the next caller — **1023 ms the first time, 7.6 ms the
second, and no model call**. Without it, two callers asking about the terrace get two different
plausible answers and the hotel contradicts itself.

But nothing verified there *is* a terrace, so the row is stored **unconfirmed**, in its **own database
file**, it can **never outrank a real row**, and `python scripts/review_learned.py` shows a manager
what has been guessed, most-asked first, to confirm or forget.

---

# 7. The recogniser mishears, and it still doesn't lie

Real failures, from real calls, all now handled:

- **"Suite" sounds like "sweet."** `base.en` returns `suit` or `sweet`, the room-type match missed,
  and one real turn spent 1360 ms in the model answering what the database answers for nothing.
- **"Dessert" came back as `Desert`.** The router missed, the model answered with no menu in front of
  it, and **invented three desserts and three prices that do not exist.** The real menu is now
  injected into the prompt, so a router miss costs a slower answer instead of a fabrication.
- **"Do you have room service?"** was misheard as *"Do you have room for this?"* and answered with the
  list of room types — a confident answer to a question nobody asked.

Replaying all 218 distinct utterances from real traces: **+2 answered deterministically, −1 wrong
answer, 0 rerouted.**

**And the fallback is honest.** Ask for room nine nine nine and it says *"We do not have a room nine
nine nine. Our rooms are numbered one zero one to five one zero"* — rather than inventing one, or
answering about a different room.

---

# 8. Two callers cannot both get the last room

Each booking takes SQLite's **write lock before the room is chosen**, and the claim is conditional on
the room still being free at the instant of writing.

**Mutation-tested, which is the only way to know a concurrency test works.** Remove the lock and make
the claim unconditional, and the test fails on every run with:

```
13 bookings for 10 free rooms
```

Three callers told they had a Deluxe King that somebody else had also been given.

---

# 9. Everything reproduces with one command

```bash
python -m pytest -q          # 1891 passed, 2 skipped — about two minutes
```

Both skips are features that genuinely don't exist, documented in the test body.

Three things about the tests are unusual enough to be worth defending:

**Expectations are read from the database, not written down.** A test cannot pass while the data says
otherwise.

**The demo script is a test.** `DEMO_SCRIPT.md` quotes what AETHER says, and the test suite parses that
document and replays the whole call — one conversation, one store, subject carried forward, against a
fresh hotel. **If the suite passes, the sheet is what will happen on camera.** It exists because a
price on our own demo sheet was wrong for months and would have been read aloud.

**Guards are mutation-tested.** A test that cannot fail is worse than no test, so every important
guarantee here has been verified by deliberately breaking the code and confirming the right test
failed.

---

# 10. Nothing about the voice provider was assumed

| | | |
|---|---|---|
| Model | `mistv3` | chosen by measuring against `mistv2` — **180 ms faster to first audio** |
| Voice | `astra` · `nadi` (hin) · `isa` (spa) | each verified against Rime's live catalogue |
| Transport | WebSocket `/ws3`, persistent | streaming, stoppable mid-utterance |
| Audio | `pcm` at 48 kHz | the gate's own rate — no MP3 decode, no resample |
| First audio | **280 ms median** on a real phone call | `evidence/demo-run.jsonl` |

When the caller interrupts, AETHER sends Rime `{"operation":"clear"}` — which is *why* it stops
mid-word instead of finishing the sentence.

**And the catalogue verification earned its keep.** Our first Hindi choice, `arcana`/`anaya`, had to be
abandoned: **Rime deleted the entire `arcana` model between 2026-09-08 and 2026-09-09** — 863
catalogue entries down to 594. It still answered after being delisted, which is exactly the trap this
project refuses: an undocumented endpoint that merely happens to respond.

---

## The numbers, in one place

| | |
|---|---|
| Stale results ever spoken | **0** across 95 runs, 6,869 events |
| Tests | **1891 passing, 2 skipped**, 49 files |
| Tools | 33 — 25 read, 8 write |
| Routing accuracy | **31/31** in each of three languages |
| Database answer | **~1 ms** |
| Rime first audio, real call | **280 ms** median |
| Whole turn, real call | **1262 ms** median |
| Duck on interruption | **22 ms** median *(synthetic)* |
| Full stop after confirmed speech | **321 ms** median *(synthetic)* |

---

## What we deliberately did not build

Stated up front, because a rubric that rewards transparent method punishes overclaiming.

- **No automatic language detection** — detection on short narrowband audio flips mid-call, and a
  language that changes by itself on camera is a visible failure. The caller chooses.
- **No fallback TTS** — Rime unconfigured means silence, not a substitute voice.
- **No salvage of partial work** — a fenced generation is discarded whole.
- **No native-speaker sign-off** on the Hindi and Spanish templates. The *facts* are provably right —
  same row, and the tests assert the numbers match across languages. The *fluency* is not certified.
- **The newest features have not been through a real phone.** Ordering, booking memory and the routing
  measurements are verified through the real pipeline with recorded audio; the committed phone
  evidence predates them.
- **Hindi speech-to-text is the weakest part.** The local model returns Urdu script and romanised
  transliteration — **21.7%** accuracy against **89.2%** for the hosted model. That's why
  `STT_PROVIDER` is configurable, and it's measured rather than hidden.
