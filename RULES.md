# AETHER — Rules

Non-negotiable rules. Breaking one is a bug, not a trade-off. If a rule appears to block real work,
raise it rather than quietly working around it.

## R1 — The golden invariant

**A stale result must never become spoken output.**

- R1.1 Every unit of work carries the generation ID that requested it.
- R1.2 Every result passes the Output Gate's validity check before speech.
- R1.3 A result whose generation is not the active generation is discarded and logged as
  `ResultDiscarded`.
- R1.4 In safe mode, `ResultLeaked` must never appear in a trace. One occurrence fails the run.
- R1.5 This invariant outranks classifier accuracy. A wrong-but-safe classification is a defect to
  fix later; a leak is a failure now.

## R2 — Fail safe under uncertainty

- R2.1 When the classifier does not recognise an utterance, take the fencing branch.
  *(Amended 2026-09-08: there is no confidence threshold. The classifier is closed-set and
  whole-utterance -- it either matches a table or falls through to `REPLACEMENT`, which fences.
  The fail-safe is the default branch, not a comparison. `AETHER_CLASSIFIER_CONFIDENCE_THRESHOLD`
  is marked dead in `.env.example`.)*
- R2.2 When a result's generation cannot be determined, discard it.
- R2.3 Silence is preferable to speaking something possibly stale.

## R3 — One event vocabulary

- R3.1 The canonical list lives in [ARCHITECTURE.md](ARCHITECTURE.md) §4, and machine-readably in
  [aether/events.py](aether/events.py).
- R3.2 Runtime, trace, tests, and evaluator use those names verbatim. No synonyms, no per-module
  local names, no free-form strings.
- R3.3 Adding or renaming an event requires updating both the document and `events.py` in the same
  change.
- R3.4 The trace is append-only. Events are never rewritten to make a run look better.

## R4 — Generation model

- R4.1 Status is `active` or `fenced`. Nothing else.
- R4.2 Exactly one generation is active at a time.
- R4.3 `fenced` is terminal.
- R4.4 `suspended` does not exist unless suspend/resume is actually implemented and tested.

## R5 — Fencing is not cancellation

- R5.1 Fencing invalidates a generation's output; the task intent may continue in a successor.
- R5.2 Cancellation ends the task intent; there is no successor.
- R5.3 A cancel fences, but a fence is not a cancel.
- R5.4 Cancellation resolves explicitly with `CancellationResolved`. A trace that shows only
  fencing has not proven cancellation.

## R6 — Duck is not stop

- R6.1 Duck happens immediately on `SpeechOnset`, before any understanding exists.
- R6.2 Full stop happens only once a meaningful interruption is confirmed.
- R6.3 They are separate events (`AudioDucked`, `AudioStopped`) and separate metrics.
- R6.4 Documentation, code, and demo narration never use the two words interchangeably.

## R7 — Status queries are read-only

- R7.1 `STATUS_QUERY` never changes the active generation.
- R7.2 It never mutates or restarts the underlying task.
- R7.3 It is answered from live task state, not from a re-run.

## R8 — Salvage honesty

- R8.1 Salvage only where reuse is genuinely valid — the new constraint is a local filter over
  records already retrieved.
- R8.2 `records_reused = 0` is a legitimate, expected outcome. Report it.
- R8.3 Never fabricate or inflate a salvage count to make the system look smarter.
- R8.4 Salvage happens at supervisor transition time. It is never a fenced generation's result
  sneaking through the Output Gate.

## R8b — Hotel facts, and where an answer may come from

*Added 2026-09-10. This rule states the current product decision and deliberately replaces an
earlier, stricter one.*

- R8b.1 **`data/aether_hotel.db` is the single source of hotel facts.** There is exactly one facts
  database. Per-language fact stores are forbidden; a language supplies translations, number, time
  and date rendering, and templates — never its own copy of a price.
- R8b.2 **If the database can answer, the database answers.** A question with a deterministic route
  never reaches the LLM: `aether.hotel.router` intercepts it and a template renders the row. This is
  the mechanism, not an instruction — it is what makes it impossible for the model to quote a price
  the hotel does not charge.
- R8b.3 **If the database cannot answer, the LLM may.** It is expected to reply naturally and
  plausibly as the duty manager, in the caller's language. It must NOT say "that is not in the
  database", refuse, or redirect to the menu. *This reverses the previous rule that the model may
  never state an unlisted hotel fact: that rule was correct about accuracy and wrong about the
  product — a duty manager who answers "not in my records" is useless on a telephone.* Bounded by
  R8b.7: this licenses answering about *the hotel* without a row, not answering about anything.
- R8b.4 **The facts it IS given are authoritative.** The model may not contradict, restate or alter
  any price, time, name or allergen that appears in the hotel data injected into its prompt.
- R8b.5 **One safety exception: allergens and dietary status are never guessed.** Not whether a dish
  contains nuts, dairy, gluten, shellfish, fish or eggs, and not whether it is vegetarian or vegan.
  A wrong opening time is corrected on the next call; a wrong allergen answer is not. Where the
  database holds the answer it is given deterministically; where it does not, the caller is told it
  will be checked with the kitchen.
- R8b.6 Every deterministic capability exists in **all supported languages**. An English-only hotel
  capability is a defect. *Measured, not asserted:* `scripts/measure_understanding.py` routes the
  same 31 sentences per language and reports what reached which tool; `tests/test_understanding.py`
  is the same table as a test, and it also guards that the three languages are tested equally.
- R8b.6a **A question is never answered by a mutating tool.** A lookup that silently becomes a write
  is the worst failure this router can produce, and it has happened twice — both times outside
  English. "क्या कमरे पर कोई बुकिंग है" (*is there a booking on that room*) and
  "बुकिंग रद्द कीजिए" (*cancel that booking*) both reached `reserve_room`, because English is
  protected by exact-form verbs and neither Hindi nor Spanish separates the verb from the noun.
  Pinned per language in `tests/test_understanding.py`.
- R8b.7 **The line is the hotel and the stay, not the database.** *Added 2026-09-18, and it reverses
  a previous instruction that the model could answer general knowledge freely.* Anything a duty
  manager would reasonably handle is in scope even with no row behind it: rooms, food, services,
  policies, the building, getting here, the local area, and ordinary courtesy. Anything else —
  history, science, politics, celebrities, arithmetic, coding, the news, a joke, a recipe — is
  declined in one warm sentence that offers help with the hotel instead. *Live: "Who was Albert
  Einstein?" was answered with a paragraph on relativity. A caller who dials a hotel and gets a
  physics lesson has reached the wrong number.* Note that R8b.7 and R8b.3 pull in opposite
  directions on purpose, and the tests pin both ends: over-correcting here re-creates the
  "not in my records" agent R8b.3 exists to prevent.
- R8b.9 **What this call has done is part of what AETHER knows.** A telephone caller gives no name
  and no number, so a booking made on the call has nothing to be looked up *by* — it is remembered
  on the session, and "what was my reference?", "when is my table booked for?" and "cancel my
  reservation" are answered from it rather than from the model. An order is the same shape and is
  kept in the database, so "repeat my order" is a lookup with `llm_ms = 0` and gives the same answer
  every time it is asked. Both obey the completed/spoken boundary: a booking or a dish the caller
  never heard confirmed is neither written nor remembered.
- R8b.11 **An order is editable, and an edit is never an addition.** Removing, replacing and
  swapping are distinct from adding, and a sentence that asks for one must never do the other. *This
  is the rule with the worst failure mode in the file:* with no removal rule at all, "remove paneer
  butter masala" was read as an ORDER for it, and a caller correcting their order watched it go from
  one to two to four of the dish they were removing. A removal that adds is worse than no removal,
  because the caller is actively correcting it and every attempt makes it worse. A number the caller
  states is honoured ("remove **one**"), and a missing number means the whole line.
- R8b.13 **A guest's details are never given out, and the refusal is SPOKEN.** A hotel line answers
  to whoever dials it, so a name, telephone number or address read back to an unauthenticated caller
  is a disclosure the database makes easy and the product must not make casual. The withholding was
  never in doubt — `reservation_for_room` has always refused to speak a name. What was missing was
  saying so: "who is staying in room two zero one" was answered "room two zero one is already
  reserved", which is true, about availability, and not the question. `guest_privacy` declines in
  one warm sentence and names what it *can* say. It reads no record at all, because the answer does
  not depend on one.
- R8b.12 **What the hotel cannot serve is said, with what it can.** Three reasons and three answers:
  sold out ("the Fish Curry is off today"), misheard ("we do not have chiken kebap, but we do have
  the Chicken Kebab" — via `clarify.nearest`), and not on the menu at all ("we do not have naan — we
  do have starters, mains, …"). Silence about part of a list reads, on a telephone, as an order that
  worked.
- R8b.10 **Taking an order writes to two more tables, and nothing else changed.** `bookings.py`
  named `restaurant_orders` and `restaurant_order_items` in its authorizer rather than relaxing it;
  a dish's price is *copied* onto the order line at the moment of ordering, so an order totals what
  the caller was told and the menu row is still unwritable.
- R8b.8 **A model answer to a hotel question is remembered, and remains a guess.** Once spoken, it
  is written to `learned_answers` and replayed verbatim to the next caller who asks the same
  question, with no model call. This buys **consistency, not truth**: nothing verified it, so the
  row is stored `confirmed = 0`, never outranks a deterministic route, and is reported as a guess by
  `scripts/review_learned.py` until a human agrees. It is kept in its own database file
  (`data/aether_learned.db`), not in `aether_hotel.db` — R8b.1 is about hotel facts, and a guess is
  not one; keeping them in separate files makes that structural rather than merely stated. Only an answer the caller actually heard is written down — a fenced or ungated turn is
  not, by the same rule that governs conversation history.

## R9 — Rime

- R9.1 Rime is the primary and sole TTS in the judged path.
- R9.2 Every spoken turn in the judged flow uses Rime.
- R9.3 The active speech provider is observable at runtime and recorded in `ResponseSpoken.provider`.
- R9.4 If a fallback provider exists at all, its use is disclosed explicitly. Silent fallback is
  forbidden.
- R9.5 Rime endpoint, model, voice, and language are configuration, never hardcoded, and never
  invented — see [RIME_EVIDENCE.md](RIME_EVIDENCE.md).

## R10 — Measurement honesty

- R10.1 Never write a number that did not come from an execution.
- R10.2 Unmeasured values are written as `<from_run>` (or `<measured>`) placeholders.
- R10.3 Metrics may be defined mathematically before they are measured; results may not.
- R10.4 `stale_leak_rate = 0` is a target until a real run produces it, and only then is it a
  result.
- R10.5 Every reported number names the run that produced it.

## R11 — Unsafe mode is test-only

- R11.1 Reachable only via an explicit test/environment switch.
- R11.2 Never enabled in the demo or any judged path.
- R11.3 It disables the Output Gate validity check and nothing else, so safe/unsafe comparisons
  differ in exactly one variable.
- R11.4 Loudly labelled wherever it appears.

## R12 — Scope discipline

Do not build: multiple domain implementations, a large warehouse application, elaborate UI, RAG
infrastructure, custom model training, multi-agent architecture, a suspend/resume stack, or
anything unrelated to interruption and continuity.

General Q&A stays supported. It must not turn the architecture into a generic assistant platform.

## R13 — Claim discipline

- R13.1 Never claim a capability that is not implemented and tested. Suspend/resume and
  spoken-prefix recovery are specifically covered by this rule.
- R13.2 Cut features per the Day-5 cut rule rather than shipping a claim the code does not support.
- R13.3 The live demo is live. Replayed traces are supporting evidence, never a substitute.

## R14 — Secrets

- R14.1 No credentials in the repository, in `.env.example`, in screenshots, or in the recording —
  not for a single frame.
- R14.2 `.env` is git-ignored. Only `.env.example` is committed, with placeholder values.
