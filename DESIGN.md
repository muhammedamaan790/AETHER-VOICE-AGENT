# AETHER — Design Notes

Why the system is shaped the way it is. [ARCHITECTURE.md](ARCHITECTURE.md) says *what*; this says
*why*, so the reasoning survives a week of pressure.

## D1 — Why generations instead of cancellation alone

Cancelling an in-flight task is not enough. Cancellation is cooperative and racy: an HTTP call may
already be in flight, a model may already be streaming, a coroutine may be between `await` points.
By the time the cancel lands, a result may already exist.

Generations move the guarantee from *stopping producers* to *rejecting consumers*. Producers may
finish late; it does not matter, because the Output Gate checks generation membership before speech.
A single check at a single choke point is far easier to prove correct than cancellation propagated
through every async path.

This is why the invariant is enforced at the Output Gate and nowhere else. One place to audit, one
place to test, one place to break in unsafe mode.

## D2 — Why fencing and cancellation are separate concepts

They answer different questions:

- Fencing: "may this generation's output be spoken?" — No.
- Cancellation: "does the user still want this task?" — No.

A refinement fences without cancelling: the user still wants the task, just differently. A cancel
does both. Collapsing them would make a trace unable to distinguish "we moved on" from "the user
stopped us", and the judge question *"why is fencing different from cancellation?"* would have no
answer in the evidence.

## D3 — Why duck immediately but stop late

At `SpeechOnset` the system knows nothing about meaning. Stopping fully on any sound makes the agent
unusable on a noisy line and destroys turns on "mhm". Continuing at full volume makes the user
talk over themselves.

Ducking is the cheap, reversible action available before understanding exists. Full stop is the
expensive, irreversible action taken once meaning is confirmed. Backchannels resolve at the duck
stage and never reach full stop — that is the whole reason the two are separate.

## D4 — Why exactly six classes, frozen

The six classes are the distinct *state transitions* the supervisor can perform: leave state alone
(backchannel), narrow it (refinement), swap it (replacement), read it (status), end it (cancel),
and start an unrelated one (new task). More classes would not produce different supervisor
behavior; fewer would collapse behaviors that must stay distinct.

Freezing the taxonomy also freezes the test matrix, which is why acceptance tests could be written
on Day 1.

## D5 — Why the invariant outranks the classifier

Classifiers are wrong sometimes; that is a fact to design around, not a bug to eliminate. A
misclassification produces a wrong-but-coherent conversation: an unnecessary re-lookup, or a
question answered too literally. The user notices and corrects it — which the system already
handles, since correction is just another interruption.

A leak produces an *incoherent* conversation: confident spoken output about a request the user
already retracted. That destroys trust in a way a wrong branch does not. So under uncertainty, the
system fences.

## D6 — Why `ResultLeaked` exists at all

An invariant with no way to observe its violation is a slogan. `ResultLeaked` makes the failure a
countable event, which makes `stale_leak_rate` computable, which makes "zero leaks" a measurement
rather than a claim.

Unsafe mode then supplies the control condition. Without it, a zero-leak run is indistinguishable
from a run where nothing was ever at risk. With it, the same delayed result leaks in one mode and is
blocked in the other, and the only difference is the check under test.

## D7 — Why salvage is allowed to reuse nothing

Refinement is a semantic category, not a promise of reusability. "Only aisle 9" is a local filter
over records already retrieved — salvage is genuine. "I meant 2026, not 2025" changes the query
identity; nothing retrieved is reusable, and the honest answer is `records_reused = 0` plus a fresh
lookup.

Pretending otherwise would mean speaking filtered stale data, which is exactly the failure the
project exists to prevent. The salvage metric is only meaningful if it is allowed to be zero.

## D8 — Why NEW_TASK fences instead of suspending (Tier 1)

Suspend/resume needs a second live task state, a resume classification, a rule for holding late
results, and a policy for what happens when the user never returns. That is a second concurrency
problem layered on the one being solved, and it can silently reintroduce the exact failure mode of
speaking work from a task the user has moved on from.

Tier 1 fences mechanically: it is simple, it is safe, and it is honest. Suspend/resume stays an
optional Day-5 stretch, single-slot only, and is not claimed unless implemented and tested.

## D9 — Why a hotel, and why the warehouse is still here

Voice must be *necessary*, not decorative. AETHER began as a warehouse picker's assistant -- hands
on a pallet, eyes on a scanner -- and became a hotel's telephone line, because a phone is the purer
case: there is no screen to fall back to at all, not merely one the user's hands are too busy for.
Every product decision since is written against that caller.

The warehouse remains, deliberately small, as a **second domain** under test (`aether/warehouse/`,
`tests/test_warehouse_tools.py`). It is what shows the engine -- generations, fencing, the Output
Gate, `ToolRunner` -- is not hotel-specific. Until 2026-09-10 it was also the only *mutable* store,
and so the only place "a fenced mutation never lands" could be tested. Hotel bookings now exercise
that path in the product itself (`tests/test_bookings.py`), so the warehouse's remaining job is the
first one. Nobody is tempted to grow it into a warehouse management system; RULES.md says so.

## D10 — Why general Q&A stays in

It proves the engine is not domain-specific, and it supplies the cleanest zero-salvage refinement
case (D7). It is a capability and a test surface — not the pitch. The architecture must not drift
toward a generic assistant platform to accommodate it.

Controlled tests back the knowledge path with a deterministic stub, because a live model's answers
are not reproducible and fixtures must be. The live demo uses the real model.

## D11 — Why one canonical event vocabulary

Competing vocabularies are how a demo ends up unable to prove its own claim: the runtime logs one
name, the evaluator greps another, and the discrepancy surfaces the night before judging. One
enumeration, one source of truth in code, one document. The evaluator reads exactly what the
runtime writes.

## D12 — Why the trace is the product of record

Judges cannot inspect memory at 2× speed during a live demo. The trace is what makes the invariant
auditable after the fact, and what turns "it never leaked" into something checkable by someone who
does not trust us.

Hence the trace-sufficiency requirement in [PRD.md](PRD.md) FR-9.3: if a question about a run cannot
be answered from the trace alone, the event model is incomplete.

## D13 — Why bookings sit behind an authorizer, on a working copy

The hotel stayed read-only for as long as it could, and that was a feature: an agent that cannot
write cannot ruin anyone's evening. Taking a booking gives that up, and the question was how little
could be given up. The answer is one read-write connection behind a `sqlite3` authorizer permitting
writes to `reservations`, `table_bookings`, `guests` and the single column `rooms.status`, and
refusing everything else at the driver -- so a price, an allergen or a room number still cannot be
changed by any code path, a buggy one included. It is `mode=ro` narrowed, not abandoned.

And the running system writes to a working copy (`aether_hotel.live.db`), never the committed file,
so rehearsals cannot drift the hotel away from what the documents and the demo sheet say it is.
