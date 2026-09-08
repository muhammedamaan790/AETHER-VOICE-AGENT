# AETHER

**AETHER answers a hotel's telephone as its duty manager.** A customer calls, asks about the menu in
ordinary speech, interrupts mid-answer, changes their mind, or tells it to stop — and the
conversation stays coherent, with a live view of what the system is doing and why.

The product is a hotel voice manager. The engineering problem underneath it is conversation
continuity during realtime voice interaction: users can interrupt, refine, replace, question, cancel
or start a new task while the agent is working, **without allowing stale state to become spoken
output**.

The hard part is not speech-to-text and it is not text-to-speech. It is what happens when a human
talks *while the agent is thinking, running a tool, or speaking*. The system must decide what the
interruption **means**, update conversational state correctly, and guarantee that work which is now
obsolete never reaches the speaker.

## The golden invariant

> **A stale result must never become spoken output.**

Every unit of work carries a generation ID. Before anything is spoken, the system verifies the
result belongs to the currently valid generation. If it does not, the result is discarded and the
discard is recorded in the event trace.

This invariant matters more than classifier accuracy. When classification is uncertain, AETHER fails
safe: it fences rather than speaks.

## Architecture in one diagram

```
 PSTN ──▶ LiveKit SIP ──▶ Room ──┬─▶ inbound track ─▶ resample 48k→16k ─▶ MicVAD.process_frame
                                 │                                              │
                                 │            Whisper ─▶ classify ─▶ MenuRouter ─┼─▶ hotel tool
                                 │                                              └─▶ Gemini
                                 │                                              │
                                 │                                    Rime /ws3 (unchanged)
                                 │                                              ▼
                                 └─◀ published track ◀─ AudioGate._callback ◀────┘
                                                              │
                    trace events ──▶ WebBridge ──▶ browser (the AETHER console)
```

**LiveKit is transport only.** It carries phone audio in and out. It is emphatically *not* the
brain: the worker uses `livekit.rtc` directly and deliberately **not** `AgentSession`, which brings
its own STT, LLM, TTS and turn detection and would bypass the GenerationRegistry, the AudioGate and
the entire fence — the parts being judged.

Everything between the two bridges is the same code the local microphone path runs. A fence that
works on a laptop works on a call because it is literally the same `AudioGate._callback` either side
of the swap.

## Interruption taxonomy (frozen — six classes)

All six are implemented in `aether/classify/`, deterministically: closed sets, whole-utterance
matching, no model, no confidence threshold. Anything the classifier does not recognise **exactly**
falls through to `REPLACEMENT`, which fences — so a misclassification can cost recall and can never
cost correctness.

| Class | Meaning | What actually happens |
|---|---|---|
| `BACKCHANNEL` | "mhm", "yeah", "okay" while AETHER is speaking | **No generation is allocated.** The answer in flight is never fenced and keeps playing. `BackchannelDetected` |
| `REFINEMENT` | An explicit correction: "actually…", "I meant…" | New generation; old one fenced. `TaskReplaced(reason=refinement)` |
| `REPLACEMENT` | Anything else said over a running task | New generation; old one fenced. `TaskReplaced(reason=replacement)` |
| `STATUS_QUERY` | "Are you still there?" during a task | **No generation is allocated.** No fence, no `TaskReplaced`; the task survives |
| `CANCEL` | "Stop", "forget it", "never mind" | Fenced with reason `cancelled_by_caller`, and **no successor task**. `CancellationResolved` |
| `NEW_TASK` | A request with nothing in flight | An ordinary turn. Nothing was displaced, so no `TaskReplaced` |

Two deliberate omissions, stated rather than papered over:

- **Salvage (`ResultSalvaged`) is not implemented.** AETHER holds no partial-result store, so a
  refinement reuses nothing. Emitting a salvage event with `records_reused = 0` on every run would
  be evidence-shaped noise (RULES.md R8), so refinement is labelled honestly and fences.
- **A status query is not answered aloud.** Speaking over an answer already in flight would need a
  second audio path able to bypass the gate's single active generation. The class protects the
  task; it does not talk over it.

Suspend/resume is **not** implemented and is not claimed.

## The two controls

They are different mechanisms, and confusing them is a bug this codebase has already had once.

| | START / STOP LISTENING | INTERRUPT |
|---|---|---|
| Question it answers | "can AETHER hear me?" | "stop what you are doing" |
| Mechanism | `MicVAD.set_listening` → `InboundBridge` drops frames | `BargeInCoordinator.fence_now` |
| Fences a generation | **never** | always (when there is one) |
| Closes the microphone | yes, that is the point | **never** |
| Ends the call | **no** — the room, track and outbound pump keep running | no |

**One toggle**, alternating between `START LISTENING` and `STOP LISTENING`; never both on screen at
once. It is not push-to-talk, and INTERRUPT is never the way to begin speaking — the caller can talk
whenever listening is on.

Natural barge-in and the INTERRUPT button converge on the same `fence_now`. One fence, two triggers,
distinguished only by `reason` in the trace (`voiced_duration_confirmed` vs `button_interrupt`).

## The hotel menu

`aether/hotel/` — 29 dishes across starters, mains, desserts and drinks, each with a price,
vegetarian/vegan/non-vegetarian, spice level, allergens and availability (two items are sold out on
purpose). Frozen dataclasses, module-level constants, no randomness, no clocks: two runs a week
apart produce byte-identical answers.

Nine read-only tools — `menu_overview`, `list_category`, `price_of`, `find_by_diet`,
`check_availability`, `check_allergens`, `find_by_spice`, `spice_of`, `safe_for` — run through the
**existing** `ToolRunner`, so fencing, injectable delay and result identity are not reimplemented.
A caller can never change the menu.

`menu_overview` answers the broadest and usually first question — "what's on the menu?", "what
dishes do you have?" — with the *shape* of the menu rather than its contents, computed from the
fixture: "We have starters, mains, desserts and drinks, with vegetarian, non-vegetarian and vegan
options." Reading twenty-nine dish names down a telephone is not an answer, and **there is one
hotel and one menu, so AETHER never asks which restaurant the caller means.**

**Menu facts do not go through the LLM.** `aether/hotel/router.py` maps a sentence to a tool by
keyword and slot, and a template renders the answer for speech (numbers as words, no symbols). The
router answers only when confident and returns `None` otherwise, so anything ambiguous still reaches
Gemini. Two reasons: an LLM stage measured ~1.8 s that a lookup does not, and a model asked to read
back `{"price": 380}` can still say a number the hotel does not charge — or an allergen that could
put somebody in hospital.

## Evidence model — three tiers, never mixed

1. **Local / laptop** — measured on this machine with a real microphone or a WAV, real STT, real
   Gemini, real Rime. Every latency number in this repository is from this tier unless it says
   otherwise.
2. **Synthetic bridge** — the LiveKit-shaped adapters driven from a WAV or generated audio, with no
   phone in the loop. Proves the plumbing and the fence; proves nothing about telephony.
3. **Real phone call — NOT YET VERIFIED.** No successful call has been completed. Nothing in this
   repository is evidence about telephone audio, and no test asserts it.

**No measurement here is real until it carries a `<from_run>` value.** See
[RIME_EVIDENCE.md](RIME_EVIDENCE.md).

## Speech output

Rime is the primary and sole TTS in the judged path — there is no fallback TTS by design (RULES.md
R9.4). Configuration, verified against Rime's live catalog:

| | |
|---|---|
| Model | `mistv3` |
| Speaker | `astra` |
| Language | `eng` |
| Transport | WebSocket `/ws3`, one persistent socket reused across turns |
| Format | raw PCM at the AudioGate's own sample rate |

`mistv3` + `astra` + `eng` is confirmed present in Rime's catalog
(`https://users.rime.ai/data/voices/voice_details.json`: 863 entries, 83 under `mistv3`, with
`astra` listed under `mistv3`, `mistv2` and `arcana`). Which of `mistv2`/`mistv3` *sounds* better
has not been judged and is not claimed; `RIME_MODEL` switches between them in one line.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in Rime, an LLM key, and LiveKit
```

**The console (local microphone).** The full product without a phone: orb, transcript, both
controls, evidence strip.

```bash
python -m aether.web
# then http://127.0.0.1:8760/index.html?ws=8761   (opened automatically)
```

**The LiveKit worker (real calls).** Registers as `aether-hotel` and waits. Prewarms the process
before registering, and serves the same console for the duration of each call.

```bash
python -m aether.telephony.agent dev        # register and wait for calls
python -m aether.telephony.agent start      # production run
```

**Everything else.**

```bash
python -m aether.spike                      # terminal only: ENTER interrupts, m toggles the mic
python -m aether.prewarm                    # measure model/import warm-up on this machine
python scripts/diagnose_mic_turn.py --phrases   # say a phrase, see which stage changed it
python scripts/calibrate_mic.py             # derive AETHER_SPEECH_FLOOR for your microphone
python scripts/bench_turn.py                # headless end-to-end turn, no mic
pytest -q
```

**Wear headphones on the local path.** The microphone stays open while the agent speaks and there is
no acoustic echo cancellation; on open speakers the agent retriggers its own VAD. On a phone call
the handset and the carrier do their own AEC, so this is expected not to apply — *expected*, not
verified.

Without Rime credentials the agent runs but cannot speak, and says so.

## Failure behaviour

| Failure | What happens |
|---|---|
| Rime unreachable / rejects | The turn is discarded with `ResultDiscarded(stage=tts)`. The session survives. Nothing is substituted — silence, not a different voice |
| LLM error, timeout or empty reply | `ResultDiscarded(stage=llm)`. Nothing is spoken and nothing enters history |
| Empty transcript (noise) | Ignored. **Does not fence** the answer in flight — the transcript is taken before a generation is allocated |
| Menu lookup fenced mid-flight | Nothing spoken; the stale answer is not handed to the LLM either |
| Caller hangs up | Everything in flight is fenced (`call_disconnected`), both pumps stop, the trace is closed |
| Browser disconnects | The engine is unaffected. On reconnect the browser is handed the whole conversation again from the backend |
| Console cannot bind its port | Logged; the call proceeds without a UI |

## Diagnostics

`aether/telephony/diagnostics.py` walks the pipeline in order and names the **first** stage that
produced nothing, from the bridge counters and the trace: `inbound_audio`, `listening_gate`, `vad`,
`stt`, `reply`, `tts`, `outbound_audio`. It is printed at the end of every call. "AETHER never
replied" is six different faults wearing the same coat, and this is what tells them apart.

Inbound level metering (`peak_rms`, `mean_rms`, `floor_rms`) is reported next to the VAD's current
`speech_floor`, so the threshold can be re-derived from real call audio when a call finally happens.
Nothing tunes itself.

## Known limitations

- **The real phone path is unvalidated.** See the evidence model above.
- Every VAD threshold is calibrated on a laptop microphone. Telephony audio is narrowband and
  codec-compressed; `AETHER_SPEECH_FLOOR` is expected to need re-deriving and has not been.
- `base.en` on narrowband phone audio may be materially worse than on clean 16 kHz. Unmeasured.
- One caller at a time is the tested case. Each call constructs its own pipeline and its own menu
  store, so calls do not share state, but concurrency has not been exercised.
- `AETHER_UNSAFE_MODE` is parsed and honoured nowhere, so the "unsafe control condition" acceptance
  scenario is skipped rather than passing.
- No AEC on the local path.

## Status

**734 tests pass, 2 are skipped.** Both skips are features that genuinely do not exist, and each one
says which: the unsafe-mode control condition, and salvage.

| Built and tested | Not built |
|---|---|
| Hotel menu fixture, 8 read-only tools, deterministic routing | Salvage / partial-result reuse |
| Six-class deterministic classifier, wired into the turn path | Suspend/resume |
| Generation registry, barge-in coordinator, four-layer fence | Evaluator (`aether/evaluator/`) |
| AudioGate with per-chunk generation tagging | Unsafe-mode control path |
| Rime `/ws3` streaming, persistent socket, mid-utterance stop | |
| LiveKit↔AETHER audio bridges (synthetic-verified) | |
| Telephony worker: lifecycle, greeting, teardown, diagnostics | |
| One listening toggle + separate INTERRUPT, both through the real bridge | |
| Siri-style console: orb, transcript, evidence strip, reconnect | |
| Process prewarm (3810 ms cold → 672 ms warm) | |
| Append-only JSONL trace with secret redaction | |

See [MEMORY.md](MEMORY.md) for the authoritative record of locked decisions, and what is measured
versus deferred.

## Documents

| File | Purpose |
|---|---|
| [DEMO.md](DEMO.md) | The demo runbook: exact flow, exact commands, what to do when it fails |
| [PRD.md](PRD.md) | Product requirements and acceptance criteria |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, generation model, canonical event vocabulary |
| [RULES.md](RULES.md) | Non-negotiable invariants and the rules that enforce them |
| [PHASES.md](PHASES.md) | Canonical plan with checkpoints and cut rules |
| [DESIGN.md](DESIGN.md) | Design decisions and the reasoning behind them |
| [MEMORY.md](MEMORY.md) | Locked decisions, status, measured vs unmeasured, human tasks |
| [RIME_EVIDENCE.md](RIME_EVIDENCE.md) | Rime checklist and pre-registered acceptance tests |
