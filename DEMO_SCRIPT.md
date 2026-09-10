# The demo call, word for word

A rehearsal sheet for the recorded demo: one guest planning dinner and a stay. Seven English turns
and a switch into Hindi, built to fit the brief's four-to-five minutes with room to narrate.

**Every answer here is quoted from the running system**, produced by putting the line through the
real `route()` → `ToolRunner` → `render()` against `data/aether_hotel.db`. Nothing on this page is
written by hand, and [`tests/test_demo_script.py`](tests/test_demo_script.py) re-runs all of it, so
this sheet cannot drift from the database the way [`DEMO.md`](DEMO.md) once did — it carried a price
of 380 and a spice answer for months after the database said 420 and the tool had been deleted.

**Every turn is deterministic, in both languages. The model is never called.** That is the claim worth making
out loud: the evidence strip shows no `llm_ms` on any turn in this conversation.

Setup, the console, and what to do when something fails are in [`DEMO.md`](DEMO.md). This page is
only the conversation.

---

## The six things the recorded demo must contain

The brief lists them, and a demo that is enjoyable but misses one loses the mark. Each is tied to a
moment below, with a time budget that fits the **4–5 minute** limit. Record against this checklist,
not against the transcript.

| # | The brief asks for | Where it happens | Budget |
|---|---|---|---|
| 1 | **Target user and problem** | Opening line to camera: *"This is a hotel's phone line. The caller has no screen — no menu to tap, no price list to scroll back to. Speech is the only surface."* | 0:00–0:25 |
| 2 | **Normal end-to-end flow** | Turns 1–4: menu, price, allergens, a hotel policy | 0:25–1:45 |
| 3 | **The selected hard voice problem** | Named out loud before turn 5: *"the hard part is what happens when the caller does not let the turn finish — a stale answer must never be spoken"* | 1:45–2:00 |
| 4 | **One deliberate stress / failure case** | **Talk over turn 5.** The barge-in beat below. Speech stops mid-word; the transcript marks the answer *"discarded — never spoken"*; turn 6 answers the question the caller *ended up* asking | 2:00–2:40 |
| 5 | **The result or measurement** | Evidence strip on screen: `stale leaks: 0`, and no `llm_ms` on any turn. Say the number: **337 ms median** to first audio on a database answer, **1345 ms** when the model is used | 2:40–3:15 |
| 6 | **Which speech provider is active** | Point at the console's voice badge — it reads the live value, not a caption: `astra · mistv3`. Say *"Rime, model mistv3, voice astra, language eng, over the /ws3 WebSocket."* Then say **"Hindi"** and it becomes `nadi · coda` while they watch | 3:15–4:15 |

**#4 and #6 are the two that get skipped**, because one needs nerve and the other looks like a
detail. #4 *is* the submission — everything else is context for it. #6 is a stated requirement with
an exact answer, and the console shows it live so it cannot be a claim over a still frame.

If the recording runs long, cut the Hindi section first, then turn 4. Never cut 5→6, which is the
barge-in beat, or the evidence strip.

---

## The call

Seven English turns, then Hindi. Shorter than the twelve-turn sheet it replaces, on purpose: the old
call filled three of the five available minutes with conversation and left almost nothing for the
barge-in, the evidence, or the language switch -- the three things the brief actually scores.

<!-- SCRIPT:BEGIN -->

| # | You say | AETHER says |
|---|---|---|
| 1 | Can you tell me what's on the menu? | We have starters, mains, vegetarian mains, desserts and drinks, with vegetarian, vegan and non-vegetarian options. |
| 2 | What does the chicken kebab cost? | The Chicken Kebab is four hundred and twenty rupees. |
| 3 | I'm allergic to nuts, what can I eat? | If you are avoiding nuts, I would suggest Chicken Kebab, Butter Chicken and Vegetable Biryani. One other dish on the menu contains nuts, so do check with me before you order. |
| 4 | Do you have a swimming pool? | Yes, we offer a swimming pool free of charge from six in the morning until eight in the evening. |
| 5 | Do you have anything vegetarian? | Yes. For vegetarian we have Paneer Tikka, Paneer Butter Masala, Vegetable Biryani, Chocolate Brownie, Fresh Fruit Bowl and Fresh Lime Soda, plus two more. |
| 6 | Actually, how much is an executive suite? | The Executive Suite is twelve thousand five hundred rupees a night, and sleeps up to three. |
| 7 | And what time can I check in? | Check-in is from two in the afternoon, and check-out is by twelve noon. |

<!-- SCRIPT:END -->

Before turn 1, AETHER opens by asking which language:

> *"Welcome to AETHER, your hotel manager. Which language would you prefer: English, Hindi, or
> Spanish?"*

Say **"English"** and it answers with the hotel greeting -- *"You've reached AETHER, the hotel's
manager. How may I help you?"* -- and the call continues in English.

Turn 4 is there to be a *new* kind of question rather than another menu lookup: it is a hotel policy,
answered from the same database, and it is the one turn a judge is most likely to try to break by
asking something similar of their own.

## Then switch language, on camera

After turn 7, say **"Hindi"**. This is the sixth required beat and the cheapest way to prove it:
the console's voice badge changes from `astra · mistv3` to `nadi · coda` while the judges watch, so
"which speech provider is active" is answered by the running system rather than by a caption.

<!-- HINDI:BEGIN -->

| lang | You say | AETHER says |
|---|---|---|
| hin | मेन्यू में क्या है | हमारे पास स्टार्टर, मेन कोर्स, शाकाहारी मेन कोर्स, मिठाई और पेय हैं, और शाकाहारी, वीगन और मांसाहारी विकल्प भी हैं। |
| hin | चिकन कबाब कितने का है | Chicken Kebab की कीमत चार सौ बीस रुपये है। |
| hin | क्या आपके पास स्विमिंग पूल है | जी हाँ, स्विमिंग पूल उपलब्ध है। यह मुफ़्त है। समय सुबह छह बजे से रात आठ बजे तक है। |

<!-- HINDI:END -->

**These are the same three database rows as turns 1, 2 and 4**, which is the point worth saying out
loud: not a translated script, the same lookup rendered by a different renderer. `llm_ms` stays at
zero in Hindi. Dish names stay in Latin script because they are the hotel's own proper nouns -- the
kitchen calls it Chicken Kebab in every language.

Say **"English"** to switch back. Expect roughly 370-450 ms to first audio in Hindi against
320-340 ms in English: that is the provider, it is measured, and it is in RIME_EVIDENCE.

If Hindi is not going to be spoken by anyone on camera, **cut this section entirely** rather than
mispronouncing it. Beat #6 can be satisfied by pointing at the badge and saying the configuration
aloud in English. A demo that fumbles a language nobody in the room speaks is worse than one that
does not attempt it.

## The one beat that needs rehearsing

**Talk over turn 5.** It is the longest harmless answer in the script -- 24 words, about ten seconds.
Start saying turn 6 roughly three words in, as it reaches *"…Paneer Tikka, Paneer…"*.

What should happen: speech stops mid-word, the transcript marks that answer **"Answer discarded --
never spoken"**, and turn 6 is answered normally with no button press and no repeat.

Note what turn 6 is: *"Actually, how much is an executive suite?"* The caller did not repeat
themselves and did not start again -- they changed the subject mid-answer, and got an answer to the
question they ended up asking. That is the brief's acceptance test happening live rather than in a
test file.

That is the whole product claim in one beat, and it is the only one whose timing matters. Turn 3 is
longer, but it is the **allergy** answer -- talking over a safety reply reads badly on camera.

## What to point at, and when

| After turn | On screen | Say |
|---|---|---|
| 1 | Evidence strip, no `llm_ms` | "That came from the hotel's database, not from a language model." |
| 3 | — | "This is the one question where a confident guess could hurt somebody, so it doesn't guess." |
| 4 | — | "Twenty-seven of these. Parking, the gym, deposits, what ID to bring." |
| 6 | Transcript: the discarded answer | "I talked over it. That answer was never spoken, and it's marked as such — and it answered the question I *ended up* asking." |
| 7 | `stale leaks: 0` | "Nothing stale ever reached the caller, across the whole call." |
| Hindi | Voice badge `nadi · coda` | "Same database row. Different renderer, different Rime voice, still no model." |

---

## If a judge asks something live

**Pronouns now work.** Ask *"how much is the chicken kebab?"* and then *"is it available
tonight?"* and the second question is answered about the kebab. That used to answer *"we have forty
one rooms free"* — confidently, about the wrong table — and this sheet used to carry a rule telling
you never to use a pronoun. A rule telling a human to avoid a defect is not a fix, so the defect was
fixed instead.

Worth knowing about how far it goes, because a judge may push:

1. **One subject, not a conversation.** It remembers the last thing you were told about, not a
   history. "The first one" and "the other one" are not understood.
2. **Naming something always wins.** Say a dish, a room or a policy and that is the subject,
   whatever came before.
3. **An interrupted answer is never referred back to.** If you talk over an answer, "it" does not
   mean that — you never heard it, so it is not part of the conversation. That is the same rule
   that keeps a fenced turn out of the transcript, and it is worth pointing at if anyone asks
   whether the memory could go stale.
4. **A question with no pronoun and no subject still goes to the model.** Guessing what an unmarked
   fragment refers to is exactly the confident-error class this router avoids.

These are verified answers. Anything here is safe to ask on camera.

<!-- BANK:BEGIN -->

| Question | AETHER says |
|---|---|
| What mains do you have? | For mains we have Butter Chicken. It is five hundred and twenty rupees. The Fish Curry is off today. |
| What desserts do you have? | For desserts we have Chocolate Brownie and Fresh Fruit Bowl. They start at two hundred and twenty rupees. |
| What drinks do you have? | For drinks we have Fresh Lime Soda, Masala Chai and Mineral Water. They start at eighty rupees. |
| Do you have vegan options? | Yes. For vegan we have Vegetable Biryani, Fresh Fruit Bowl, Fresh Lime Soda, Masala Chai and Mineral Water. |
| How much is the paneer butter masala? | The Paneer Butter Masala is four hundred and eighty rupees. |
| Tell me about the paneer tikka. | Char-grilled paneer with peppers and spices. |
| Do you have any rooms available? | We have forty one rooms free, starting at six thousand five hundred rupees a night. |
| How much is a family suite? | The Family Suite is fifteen thousand rupees a night, and sleeps up to four. |
| What comes with a standard king? | The Standard King has king bed, wi-fi, air conditioning, tv and work desk. |
| Is room one zero two free? | Room one zero two is occupied at the moment. |
| Is room one zero four free? | Room one zero four is with housekeeping just now. |
| Is room two zero two free? | Room two zero two is already reserved. |
| Is room two zero four free? | Room two zero four is out for maintenance. |
| Is room two zero two reserved? | Room two zero two is held on a confirmed booking. It is a Deluxe King, booked from the fifteenth of September to the eighteenth of September. |
| What services do you have? | We offer front desk, housekeeping, luggage assistance, maintenance, room service and wake-up call. |
| What time is housekeeping available? | Housekeeping is available from eight in the morning until ten at night. You can reach it on extension one zero two. |
| What time is check out? | Check-in is from two in the afternoon, and check-out is by twelve noon. |

<!-- BANK:END -->

Every one of these is deterministic too. A reservation lookup names the room, the type and the
dates, and **never the guest's name** — that is deliberate, and worth saying if anyone asks.

## Traps

**Only fifty rooms exist: x01 to x10 on five floors.** 101–110, 201–210, and so on to 510. There is
no room 412, no 350, no 220. **This is now a good beat rather than a trap** — ask *"is room nine
nine nine free?"* on purpose and AETHER says *"We do not have a room nine nine nine. Our rooms are
numbered one zero one to five one zero."* It names the room as absent and offers the range that
exists, in all three languages, and the range is read from the database rather than written down.

It used to answer with the generic *"I could not find that, could you say it again?"*, which
describes a mishearing — so a caller who spoke perfectly clearly would repeat the same impossible
number, louder. Fixed 2026-09-10.

**"Does the butter chicken contain nuts?"** answers *"The Butter Chicken contains dairy."* It tells
you what the dish does contain rather than answering yes or no. True and safe, but it can sound like
a dodge — prefer turn 5's phrasing, *"I'm allergic to nuts, what can I eat?"*, which is the question
the tool is built for.

**Reset the browser zoom to 100%** (Ctrl+0) before recording. The console is laid out for a full
window.

**Say room numbers digit by digit** — "three zero five", not "three hundred and five". That is how
AETHER says them back, and how the recogniser hears them best.

**A spoken room number is the least reliable thing you can say on this call**, which is why the
script does not contain one. An earlier sheet asked *"is room one zero one free?"* and it did not
recognise well on a real line: "one zero one" is the same short vowel three times, and the recogniser
has no menu of room numbers to bias towards the way it effectively does for dish and room-type
names. *"Is room three zero five free?"* is more distinct, and if you want a per-room lookup on camera
that is the one to use — but it is optional, and the answer is a flat *"Room three zero five is
occupied at the moment."* It is in the bank above either way.

**"Deluxe king" was cut for the same reason**: "deluxe" is the rarest word in the whole vocabulary
and the recogniser has the least to go on. The script asks for the **executive suite** instead.
