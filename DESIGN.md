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
unusable in a noisy warehouse and destroys turns on "mhm". Continuing at full volume makes the user
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

## D9 — Why a warehouse

Voice must be *necessary*, not decorative. Hands on a pallet and eyes on a scanner make a screen
unusable, so the demo does not have to argue for voice.

The dataset is deliberately tiny and deterministic: enough records for "8 priority orders, actually
only aisle 9" to be meaningful, few enough that every test is reproducible and nobody is tempted to
build a warehouse management system. The engine is domain-agnostic; the warehouse is a fixture.

## D10 — Why general Q&A stays in

It proves the engine is not warehouse-specific, and it supplies the cleanest zero-salvage refinement
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
