# The demo — full script

The whole recording, in order: every line you say, every line AETHER says back, what to point at,
and what each line means for judges who don't speak Hindi or Spanish. It is built around the **six
things the brief says a demo must show**, in the order the brief lists them, and it runs about
**four minutes fifty** — inside the five-minute limit.

Every line marked **AETHER:** is quoted from the running system, not written by hand.
[`tests/test_demo_script.py`](tests/test_demo_script.py) checks every one of them and replays the call
as a real conversation. If the test suite passes, this is what will happen.

| # | What the brief asks the demo to show | Where in the script |
|---|---|---|
| 1 | The target user and problem | 0:00 |
| 2 | The normal end-to-end flow | 0:20 |
| 3 | The selected hard voice problem | 1:25 |
| 4 | One deliberate stress or failure case | 1:40 |
| 5 | The result or measurement | 2:55 |
| 6 | Which speech provider is active | 3:15 — and it changes voice for Hindi, then Spanish |

---

## Before you record

Follow [SETUP.md](SETUP.md) once to install everything. Then, each time:

1. **Reset the hotel.** The call takes a real booking, and the room and reference it reads out are the
   ones a fresh hotel gives.

   ```powershell
   python scripts/reset_hotel_db.py
   ```

2. **Start the agent** and leave its window open. Wait for **`registered worker`**.

   ```powershell
   python scripts/run_call.py
   ```

3. **Open the console** at the address it printed — normally
   `http://127.0.0.1:8760/index.html?ws=8761` — full screen, browser zoom 100% (Ctrl+0).

4. **If you're calling through Windows Phone Link:**
   - **wear a headset on the laptop**, or AETHER hears its own voice and interrupts itself;
   - **turn off Audio enhancements and Voice clarity** for the microphone in Windows Sound settings;
   - **record with OBS** — *Display Capture*, *Desktop Audio* and *Mic/Aux* — so both voices and the
     console are captured.

   Details are in [SETUP.md §11](SETUP.md#11-recording-the-demo-with-windows-phone-link).

5. **Make a 20-second test call.** Say **"English"**, then **"menu"**, and hang up. The agent window
   should show lines starting `LANGUAGE:` and `MENU[`. If AETHER says *"Hello, are you there?"*, your
   voice isn't reaching it — fix the headset or microphone settings and try again.

6. **Rehearse the stress case at 1:40 twice.** It's the only moment where timing matters.

Hang up the phone **before** pressing Ctrl+C in the agent window.

---

## The script

<!-- SCREENPLAY:BEGIN -->

### 1 · The target user and the problem — 0:00

**YOU (to camera):** "This is AETHER, the duty manager on a hotel's phone line. The caller has no
screen — no menu to tap, no price list to scroll back to. Speech is the only way in. This hotel is in
India, so callers speak English, Hindi and Spanish. And people on the phone don't wait their turn.
Let me call it."

### 2 · The normal end-to-end flow — 0:20

*[Dial the hotel. The console changes from "Standing by" to "Call active". Wait a moment — AETHER
waits until your phone is listening before it speaks.]*

**AETHER:** Welcome to AETHER, your hotel manager. Which language would you prefer: English, Hindi, or Spanish?

**YOU:** English.

**AETHER:** You've reached AETHER, the hotel's manager. How may I help you?

**YOU:** Can you tell me what's on the menu?

**AETHER:** We have starters, mains, vegetarian mains, desserts and drinks, with vegetarian, vegan and non-vegetarian options.

*[Point at the evidence strip.]*

**YOU (to camera):** "That came straight from the hotel's own database — no language model."

**YOU:** What does the chicken kebab cost?

**AETHER:** The Chicken Kebab is four hundred and twenty rupees.

**YOU:** I'm allergic to nuts, what can I eat?

**AETHER:** If you are avoiding nuts, I would suggest Chicken Kebab, Butter Chicken and Vegetable Biryani. One other dish on the menu contains nuts, so do check with me before you order.

**YOU (to camera):** "The one question where a confident guess could hurt someone — so it never
guesses."

**YOU:** Do you have a swimming pool?

**AETHER:** Yes, we offer a swimming pool free of charge from six in the morning until eight in the evening.

### 3 · The selected hard voice problem — 1:25

**YOU (to camera):** "The hard part isn't speech-to-text or text-to-speech. It's what happens when the
caller doesn't let the answer finish. A stale answer must never be spoken — and never remembered.
Watch."

### 4 · The deliberate stress case — 1:40

**YOU:** Do you have anything vegetarian?

**AETHER (you talk over this):** Yes. For vegetarian we have Paneer Tikka, Paneer…

*[As it reaches "Paneer Tikka, Paneer", talk over it at normal volume. Don't wait, and don't press
anything.]*

**YOU:** Actually, how much is an executive suite?

*[AETHER stops mid-word. The console marks the vegetarian answer "discarded — never spoken".]*

**AETHER:** The Executive Suite is twelve thousand five hundred rupees a night, and sleeps up to three.

*[Point at the discarded answer.]*

**YOU (to camera):** "I talked over it. That answer was never spoken — the console marks it — and
AETHER answered the question I actually ended up asking."

**YOU:** Book it for two nights.

**AETHER:** Done. I have reserved the Executive Suite, room three zero one, for two nights at twelve thousand five hundred rupees a night. Your reference is one zero zero four.

**YOU (to camera):** "I said 'book it'. It booked the suite — the thing I heard about — not the
vegetarian list I interrupted. Its memory follows what was actually heard, and that's a real booking
in the hotel's database."

**YOU:** Is room nine nine nine free?

**AETHER:** We do not have a room nine nine nine. Our rooms are numbered one zero one to five one zero.

**YOU (to camera):** "And it won't invent a room."

### 5 · The result — 2:55

*[Point at "Stale leaks: 0" in the caller panel.]*

**YOU (to camera):** "Zero stale answers reached the caller. Answers from the database start playing
in about a third of a second, and nothing on this call needed a language model. We've measured the
same on real phone calls — across every call we've recorded, stale leaks are zero."

### 6 · The speech provider: Rime — 3:15

*[Point at the Voice line in the caller panel: "astra · mistv3". It's read live from the engine, not
typed in.]*

**YOU (to camera):** "Every word you've heard is Rime — model mistv3, voice astra — streamed over a
WebSocket. When I interrupted, AETHER told Rime to clear its queue mid-sentence. That's why it stopped
instantly. Now watch the voice change."

**YOU:** Can we switch language?

**AETHER:** I can help you in English, Hindi or Spanish. Which would you prefer?

**YOU:** Hindi.

**AETHER:** जी हाँ, मैं हिन्दी में बात करूँगी। बताइए, मैं आपकी क्या मदद कर सकती हूँ?

*[Means: "Yes, I'll speak in Hindi. Tell me, how can I help you?" The Voice line changes to
"nadi · coda" — a different Rime model and voice, chosen for Hindi.]*

**YOU:** मेन्यू में क्या है

*[Say it as: "Menu mein kya hai?" — "What's on the menu?"]*

**AETHER:** हमारे पास स्टार्टर, मेन कोर्स, शाकाहारी मेन कोर्स, मिठाई और पेय हैं, और शाकाहारी, वीगन और मांसाहारी विकल्प भी हैं।

*[Means: "We have starters, mains, vegetarian mains, desserts and drinks, with vegetarian, vegan and
non-vegetarian options."]*

**YOU:** चिकन कबाब कितने का है

*[Say it as: "Chicken kebab kitne ka hai?" — "How much is the chicken kebab?"]*

**AETHER:** Chicken Kebab की कीमत चार सौ बीस रुपये है।

*[Means: "The Chicken Kebab costs four hundred and twenty rupees." The same price as in English — it's
the same database row.]*

*[Hang up.]*

**YOU (to camera):** "Same database, a different renderer, a different Rime voice — and still no
language model. One more language."

*[Dial again.]*

**AETHER:** Welcome to AETHER, your hotel manager. Which language would you prefer: English, Hindi, or Spanish?

**YOU:** Spanish.

**AETHER:** Ha llamado a AETHER, la gerente del hotel. ¿En qué puedo ayudarle?

*[Means: "You've reached AETHER, the hotel's manager. How can I help you?" The Voice line changes to
"isa · mistv3" — Rime's Spanish voice.]*

**YOU:** ¿Qué hay en el menú?

*[Say it as: "Keh eye en el meh-NOO?" — "What's on the menu?"]*

**AETHER:** Tenemos entrantes, platos principales, platos principales vegetarianos, postres y bebidas, con opciones vegetarianas, veganas y no vegetarianas.

*[Means: the same menu answer, in Spanish.]*

**YOU:** ¿Tienen piscina?

*[Say it as: "Tee-EH-nen pee-SEE-nah?" — "Do you have a pool?"]*

**AETHER:** Sí, ofrecemos una piscina sin coste desde las seis de la mañana hasta las ocho de la noche.

*[Means: "Yes, we have a swimming pool, free, from six in the morning until eight in the evening."]*

*[Hang up.]*

### Close — 4:35

**YOU (to camera):** "That's AETHER: a hotel phone line where interrupting is safe. The abandoned
answer is never spoken, never remembered and never booked — over a real phone call, in three
languages, from one database. And the repository reproduces every claim with one command."

<!-- SCREENPLAY:END -->

---

## If something goes wrong on camera

| What happens | What to do |
|---|---|
| You hear nothing after AETHER answers | The audio connection didn't come up. Hang up and redial. |
| AETHER says *"Hello, are you there?"* | Your voice isn't reaching it. Check the headset, mute and Voice clarity; if it happens again, redial. |
| *"Sorry, I did not quite catch that. Did you mean …?"* | Say **"yes"**. It's offering the nearest real thing instead of guessing — keep rolling. |
| *"Sorry, I did not catch that. Could you say it again?"* | Repeat the line a little more slowly, close to the microphone. |
| It asks for a language twice, then *"Let's continue in English…"* | Carry on in English; switch later with *"Can we switch language?"*. |
| The booking reads a different room or reference | You didn't reset first. The booking is still right — keep going, or reset and re-record. |
| The vegetarian answer doesn't stop when you talk | Start earlier, at *"Paneer Tikka, Paneer"*, at normal volume. |
| A turn shows `llm_ms` in the evidence strip | The recogniser misheard and the language model answered. Carry on; don't point at the strip on that turn. |

**If it runs long, cut in this order:** the second Spanish question, the second Hindi question, room
nine nine nine, the swimming pool. **Never cut 1:40–2:55** — the interruption, the recovery and the
booking are one beat.

**If nobody on camera speaks Hindi or Spanish,** keep the Rime line and one question in each
language: the voice change on the console is the point, not fluency.

---

## The exact replies, as the test suite checks them

You don't need these while recording — the script above already contains every line. These tables are
what [`tests/test_demo_script.py`](tests/test_demo_script.py) replays.

<!-- SCRIPT:BEGIN -->

| # | You say | AETHER says |
|---|---|---|
| 1 | Can you tell me what's on the menu? | We have starters, mains, vegetarian mains, desserts and drinks, with vegetarian, vegan and non-vegetarian options. |
| 2 | What does the chicken kebab cost? | The Chicken Kebab is four hundred and twenty rupees. |
| 3 | I'm allergic to nuts, what can I eat? | If you are avoiding nuts, I would suggest Chicken Kebab, Butter Chicken and Vegetable Biryani. One other dish on the menu contains nuts, so do check with me before you order. |
| 4 | Do you have a swimming pool? | Yes, we offer a swimming pool free of charge from six in the morning until eight in the evening. |
| 5 | Do you have anything vegetarian? | Yes. For vegetarian we have Paneer Tikka, Paneer Butter Masala, Vegetable Biryani, Chocolate Brownie, Fresh Fruit Bowl and Fresh Lime Soda, plus two more. |
| 6 | Actually, how much is an executive suite? | The Executive Suite is twelve thousand five hundred rupees a night, and sleeps up to three. |
| 7 | Book it for two nights. | Done. I have reserved the Executive Suite, room three zero one, for two nights at twelve thousand five hundred rupees a night. Your reference is one zero zero four. |
| 8 | Is room nine nine nine free? | We do not have a room nine nine nine. Our rooms are numbered one zero one to five one zero. |

<!-- SCRIPT:END -->

Turn 5 is talked over, so it's never heard in full. That's why turn 7's "it" means the suite from turn
6: turn 5 was never heard, so it never became the subject.

<!-- LANGUAGES:BEGIN -->

| lang | You say | AETHER says |
|---|---|---|
| hin | मेन्यू में क्या है | हमारे पास स्टार्टर, मेन कोर्स, शाकाहारी मेन कोर्स, मिठाई और पेय हैं, और शाकाहारी, वीगन और मांसाहारी विकल्प भी हैं। |
| hin | चिकन कबाब कितने का है | Chicken Kebab की कीमत चार सौ बीस रुपये है। |
| hin | क्या आपके पास स्विमिंग पूल है | जी हाँ, स्विमिंग पूल उपलब्ध है। यह मुफ़्त है। समय सुबह छह बजे से रात आठ बजे तक है। |
| spa | ¿Qué hay en el menú? | Tenemos entrantes, platos principales, platos principales vegetarianos, postres y bebidas, con opciones vegetarianas, veganas y no vegetarianas. |
| spa | ¿Tienen piscina? | Sí, ofrecemos una piscina sin coste desde las seis de la mañana hasta las ocho de la noche. |

<!-- LANGUAGES:END -->

These are the same database rows as the English turns, rendered in Hindi and Spanish.

---

## If a judge asks something live

**Pronouns work.** *"How much is the chicken kebab?"* then *"Is it available tonight?"* — the second
question is answered about the kebab. What it remembers, and where it stops:

1. **One subject, not a history.** It remembers the last thing you were told about. "The first one"
   and "the other one" aren't understood.
2. **Naming something always wins.** Say a dish, a room or a policy and that becomes the subject.
3. **An interrupted answer is never referred back to.** You never heard it, so "it" can't mean it.
4. **A question with no pronoun and no subject goes to the model.** It doesn't guess.

Anything in this table is safe to ask on camera:

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

A reservation lookup names the room, the type and the dates, and **never the guest's name**. That's
deliberate, and worth saying if anyone asks.

---

## Traps

**Only fifty rooms exist:** 101–110, 201–210 and so on to 510. Any other number gets *"We do not have a
room…"* with the real range — the stress case uses that on purpose.

**Say room numbers digit by digit** — "three zero five", not "three hundred and five". That's how
AETHER says them back, and how the recogniser hears them best.

**The rarest words recognise worst.** The script asks for the *executive suite* rather than the
*deluxe king* for that reason. If AETHER mishears a room type it offers *"Did you mean …?"* — say yes.

**Switch language once per call.** Asking to switch a second time, while speaking Hindi or Spanish,
hasn't been tested on a phone line. That's why the script hangs up and redials for Spanish.
