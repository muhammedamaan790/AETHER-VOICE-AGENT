# The demo call, word for word

A rehearsal sheet for the recorded demo: one guest planning dinner and a stay, twelve turns, about
three minutes.

**Every answer here is quoted from the running system**, produced by putting the line through the
real `route()` → `ToolRunner` → `render()` against `data/aether_hotel.db`. Nothing on this page is
written by hand, and [`tests/test_demo_script.py`](tests/test_demo_script.py) re-runs all of it, so
this sheet cannot drift from the database the way [`DEMO.md`](DEMO.md) once did — it carried a price
of 380 and a spice answer for months after the database said 420 and the tool had been deleted.

**All twelve turns are deterministic. The model is never called.** That is the claim worth making
out loud: the evidence strip shows no `llm_ms` on any turn in this conversation.

Setup, the console, and what to do when something fails are in [`DEMO.md`](DEMO.md). This page is
only the conversation.

---

## The call

<!-- SCRIPT:BEGIN -->

| # | You say | AETHER says |
|---|---|---|
| 1 | Can you tell me what's on the menu? | We have starters, mains, vegetarian mains, desserts and drinks, with vegetarian, vegan and non-vegetarian options. |
| 2 | And what about starters? | For starters we have Chicken Kebab and Paneer Tikka. They start at three hundred and sixty rupees. The Vegetable Samosa is off today. |
| 3 | What does the chicken kebab cost? | The Chicken Kebab is four hundred and twenty rupees. |
| 4 | Is the fish curry available? | I am sorry, the Fish Curry is not available today. |
| 5 | I'm allergic to nuts, what can I eat? | If you are avoiding nuts, I would suggest Chicken Kebab, Butter Chicken and Vegetable Biryani. One other dish on the menu contains nuts, so do check with me before you order. |
| 6 | Do you have anything vegetarian? | Yes. For vegetarian we have Paneer Tikka, Paneer Butter Masala, Vegetable Biryani, Chocolate Brownie, Fresh Fruit Bowl and Fresh Lime Soda, plus two more. |
| 7 | Actually, what rooms do you have? | We have Standard King, Standard Twin, Deluxe King, Executive Suite and Family Suite, starting at six thousand five hundred rupees a night. |
| 8 | How much is a deluxe king? | The Deluxe King is eight thousand five hundred rupees a night, and sleeps up to two. |
| 9 | Is room one oh one free? | Room one oh one is free. It is a Standard King at six thousand five hundred rupees a night. |
| 10 | What comes with an executive suite? | The Executive Suite has king bed, living room, wi-fi, smart tv, minibar, city view and breakfast. |
| 11 | And what time can I check in? | Check-in is from two in the afternoon, and check-out is by twelve noon. |
| 12 | Do you have room service? | Room Service is available from six in the morning until eleven at night. You can reach it on extension one oh one. |

<!-- SCRIPT:END -->

Before turn 1, AETHER opens with the fixed greeting: *"You've reached AETHER, the hotel's manager.
How may I help you?"*

## The one beat that needs rehearsing

**Talk over turn 6.** It is the longest harmless answer in the script — 24 words, about ten seconds.
Start saying turn 7 roughly three words in, as it reaches *"…Paneer Tikka, Paneer…"*.

What should happen: speech stops mid-word, the transcript marks that answer **"Answer discarded —
never spoken"**, and turn 7 is answered normally with no button press and no repeat.

That is the whole product claim in one beat, and it is the only one whose timing matters. Turn 5 is
longer, but it is the **allergy** answer — talking over a safety reply reads badly on camera.

## What to point at, and when

| After turn | On screen | Say |
|---|---|---|
| 1 | Evidence strip, no `llm_ms` | "That came from the hotel's database, not from a language model." |
| 2 | — | "It volunteered that the samosa is off. Nobody asked." |
| 4 | — | "It will tell you what it hasn't got." |
| 5 | — | "This is the one question where a confident guess could hurt somebody, so it doesn't guess." |
| 7 | Transcript: the discarded answer | "I talked over it. That answer was never spoken, and it's marked as such." |
| 9 | — | "A real room, with its real status. Said as a door — 'one oh one' — not as a number." |
| 12 | `stale leaks: 0`, recording path | "Nothing stale ever reached the caller, and the whole call is on disk." |

---

## If a judge asks something live

Two rules, and they matter more than they look:

1. **Name the subject in every question.** The router has no conversational memory.
2. **Never use a pronoun for the previous turn.** *"Is that available tonight?"* after asking about
   the chicken kebab answers *"We have forty one rooms free"* — confidently, and about the wrong
   table.

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
| Is room one oh two free? | Room one oh two is occupied at the moment. |
| Is room one oh four free? | Room one oh four is with housekeeping just now. |
| Is room two oh two free? | Room two oh two is already reserved. |
| Is room two oh four free? | Room two oh four is out for maintenance. |
| Is room two oh two reserved? | Room two oh two is held on a confirmed booking. It is a Deluxe King, booked from the fifteenth of September to the eighteenth of September. |
| What services do you have? | We offer front desk, housekeeping, luggage assistance, maintenance, room service and wake-up call. |
| What time is housekeeping available? | Housekeeping is available from eight in the morning until ten at night. You can reach it on extension one oh two. |
| What time is check out? | Check-in is from two in the afternoon, and check-out is by twelve noon. |

<!-- BANK:END -->

Every one of these is deterministic too. A reservation lookup names the room, the type and the
dates, and **never the guest's name** — that is deliberate, and worth saying if anyone asks.

## Traps

**Only fifty rooms exist: x01 to x10 on five floors.** 101–110, 201–210, and so on to 510. There is
no room 412, no 350, no 220. A judge picking a room at random will usually pick one that does not
exist, and the answer is *"I am sorry, I could not find that. Could you say it again?"* — honest,
but it sounds like a mishearing rather than "no such room". If it happens, say so: it refuses to
invent a room. Or ask *"is room nine nine nine free?"* deliberately and make it a feature.

**"Does the butter chicken contain nuts?"** answers *"The Butter Chicken contains dairy."* It tells
you what the dish does contain rather than answering yes or no. True and safe, but it can sound like
a dodge — prefer turn 5's phrasing, *"I'm allergic to nuts, what can I eat?"*, which is the question
the tool is built for.

**Reset the browser zoom to 100%** (Ctrl+0) before recording. The console is laid out for a full
window.

**Say room numbers digit by digit** — "one oh one", not "a hundred and one". That is how AETHER says
them back, and how the recogniser hears them best.
