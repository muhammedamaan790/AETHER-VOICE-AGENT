# AETHER — demo runbook

The exact sequence, the exact commands, and what to say if something fails. Read
[RIME_EVIDENCE.md](RIME_EVIDENCE.md) Part 6 first: **the real phone path has never been validated**,
and this runbook does not pretend otherwise.

---

## Before anything

```bash
python -m pytest -q                 # expect 765 passed, 2 skipped
python -m aether.prewarm            # warms the process; prints what it cost
```

Check `.env` has `RIME_API_KEY`, one LLM key, and the three `LIVEKIT_*` values. Nothing prints them,
and nothing should.

**Wear headphones on the local path.** The microphone stays open while AETHER speaks and there is no
echo cancellation; on open speakers it retriggers its own VAD.

---

## Launch

**The console (local microphone).** This is the fallback demo and the regression harness. It works
with no phone, no LiveKit and no network beyond Rime and the LLM.

```bash
python -m aether.web
```

Opens `http://127.0.0.1:8760/index.html?ws=8761`.

**The LiveKit worker (real calls).** Registers as `aether-hotel`, prewarms, and serves the console
for the whole life of the worker — open the page BEFORE you dial.

**Do not run `python -m aether.web` at the same time.** It is a second complete AETHER, with its own
Whisper model and its own microphone, competing for CPU with the call and racing it for the ports.
The worker's own console is the one to use.

```bash
python -m aether.telephony.agent dev
```

If a previous call went wrong, capture the next one so the failure names itself:

```bash
AETHER_CALL_CAPTURE=call1.wav python -m aether.telephony.agent dev 2>&1 | tee call1.log
```

Open `http://127.0.0.1:8760/index.html?ws=8761` **first** — the orb sits at "waiting for a call".
Then dial. The worker logs numbered stages `[1/7]`…`[7/7]`, so a failure says exactly how far it
got, and prints a diagnosis when the call ends.

---

## The flow

| # | Do this | What to point at |
|---|---|---|
| 0 | Page open, before dialling | Orb calm grey, **"waiting for a call"**, `not recording` |
| 1 | Dial the number | Orb wakes; the recording strip turns green with the trace path and a live event count |
| 2 | AETHER greets: *"You've reached AETHER, the hotel's manager. How may I help you?"* | Orb amber, transcript line appears |
| 3 | *"What starters do you have?"* | Answered **without the LLM**. Evidence strip: no `llm_ms` |
| 4 | *"How much is the chicken kebab?"* | *"The Chicken Kebab is four hundred and twenty rupees."* Exact, read from `data/aether_hotel.db` |
| 5 | *"Is room three oh five free?"* | *"Room three oh five is occupied at the moment."* A different table, same deterministic path — and the number is spoken as a door, not a quantity |
| 6 | *"I have a nut allergy, what can I eat?"* | Suggestions plus a count of what to avoid. **Never** from the model |
| 6b | *"What time is check in?"* | *"Check-in is from two in the afternoon, and check-out is by twelve noon."* |
| 7 | **Talk over AETHER mid-answer** | Speech stops. Transcript marks the abandoned turn *"Answer discarded — never spoken"*. New question answered |
| 8 | Say **"mm-hm"** while it is speaking | Nothing happens — and that is the point. `BackchannelDetected`, no fence, answer keeps playing |
| 9 | Say **"stop"** | Speech stops, nothing new is said. `CancellationResolved`, fence reason `cancelled_by_caller` |
| 10 | Press **INTERRUPT** mid-answer | Same fence, different reason: `button_interrupt` |
| 11 | Press **STOP LISTENING** | Button becomes START LISTENING. **The call stays connected** — check LiveKit still shows the participant. Speak: nothing is heard |
| 12 | Press **START LISTENING** | Button becomes STOP LISTENING. Speak: answered normally, with no INTERRUPT press needed |
| 13 | Point at the evidence strip | Generation, phase, last fence reason, class, latency, **stale leaks: 0** |
| 14 | Point at the event log | Canonical events scrolling as they are written — the log being produced live |
| 15 | Hang up | Orb returns to "waiting for a call". The page stays connected; nothing reloads |
| 16 | **Scroll the conversation back up** | The panel scrolls on its own, with a visible bar. The orb and both controls stay put — the console is a fixed frame and only the conversation moves |
| 17 | Open the trace file named in the recording strip | The whole conversation, on disk |

If the screen looks small, reset the browser zoom to 100% (Ctrl+0) before recording — the console
is laid out for a full window, and a zoomed-out tab shrinks every label with it.

Two lines worth saying out loud, because they are the claim:

> The listening toggle is not push-to-talk. The customer never has to press anything to speak.

> Interrupt and natural barge-in are the **same fence**. Only the reason in the trace differs.

---

## What to say about the evidence

- Latency numbers on screen are **measured by the run in progress**. Nothing is projected.
- `stale leaks: 0` is the golden invariant, live.
- The interruption class shown is deterministic — closed sets, no model, no confidence score.
  Anything it does not recognise exactly falls through to REPLACEMENT, which fences.
- If asked "does the phone path work": **it has not been validated.** The bridges are proven against
  synthetic audio and the fence is proven on the laptop; the call itself is unverified.

---

## If it fails

| Symptom | Do |
|---|---|
| Call connects, AETHER silent | Let the call end. The worker prints `--- call diagnostics ---` naming the first failed stage: `inbound_audio`, `listening_gate`, `vad`, `stt`, `reply`, `tts`, `outbound_audio` |
| Greeting heard, then AETHER cannot hear you | Check the `transport:` line. `pumps=2` for one caller means the track was attached twice and two readers were interleaved. `frames_failed` above zero means frames are arriving unconvertible. Re-run with `AETHER_CALL_CAPTURE=call.wav` and listen to what actually arrived |
| Verdict is `vad` | The report prints `peak_rms` / `floor_rms` next to `speech_floor`. Set `AETHER_SPEECH_FLOOR` from that and restart. Do not guess it |
| Verdict is `stt` | Speech was heard, words were not. Nothing to tune live — fall back to the local console |
| Rime rejects a turn | The turn is discarded and the session survives. There is **no fallback voice** by design; say so |
| The whole call path fails | **Switch to `python -m aether.web`.** The same brain, the same menu, the same fence, the same console — with a microphone instead of a phone. Say plainly that telephony is unvalidated |
| Console will not bind its port | `AETHER_WEB_HTTP_PORT` / `AETHER_WEB_WS_PORT`, or `AETHER_WEB=0` to run the call without a UI |

---

## What not to claim

- That a real call has been measured. It has not.
- That `mistv3` sounds better than `mistv2`. Only that it reached first audio sooner and speaks
  ~15% longer, in both recorded runs.
- That the ~1.2 s menu-path turn projection is a measurement. It is arithmetic on laptop numbers.
- That anything was salvaged from a fenced generation. Nothing ever is.
- That a status query is answered aloud. It protects the task; it does not speak.
