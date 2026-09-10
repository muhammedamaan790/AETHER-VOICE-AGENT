# AETHER — demo runbook

The exact sequence, the exact commands, and what to say if something fails. Read
[RIME_EVIDENCE.md](RIME_EVIDENCE.md) Part 6 first: the phone path **connects, answers, understands
and transcribes**, measured on real calls — but STT word accuracy on narrowband audio is still not
separately measured, and this runbook does not pretend otherwise.

**The conversation itself is in [DEMO_SCRIPT.md](DEMO_SCRIPT.md)** — twelve turns, with AETHER's
exact words quoted from the running system and re-checked by
[`tests/test_demo_script.py`](tests/test_demo_script.py) so the sheet cannot go stale. This runbook
covers setup, the console and the failure paths; that one covers what to say.

---

## Before anything

```bash
python -m pytest -q                 # expect 1260 passed, 2 skipped
python -m aether.prewarm            # warms the process; prints what it cost
```

**One command that shows the whole product working**, if you want a single thing to run rather than
a suite to read:

```bash
python scripts/demo_full_call.py                    # no keys, no network, no cost
python scripts/demo_full_call.py --mode live        # real Gemini, real Rime; costs API calls
```

One call, 27 turns, a real interruption mid-lookup and three language switches, ending in a printed
latency table and a PASS/FAIL verdict. It exits non-zero if a stale result is ever spoken, so it can
be run as a check and not only read. Measured output is in
[RIME_EVIDENCE.md](RIME_EVIDENCE.md) under *One full call, measured end to end*.

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
python scripts/run_call.py
```

That is `python -m aether.telephony.agent dev` with its output written to a **new log every time** --
`logs/worker-<UTC timestamp>.log`, printed as it starts. Nothing to number, nothing to rename, and
no run can overwrite another. It still prints to your screen exactly as before.

The log ends with a footer naming every trace that worker session produced, so the two halves of the
evidence are paired **in the file** rather than argued afterwards by matching latencies -- which is
how `evidence/demo-call-worker.log` had to be tied to its trace. Each of those traces also carries
`input_path=telephony` on its first event, so it states for itself that the audio came over a phone
line rather than a laptop microphone.

Any LiveKit CLI argument is forwarded: `python scripts/run_call.py start` for a production run.
`--log-dir` (or `AETHER_LOG_DIR`) moves the logs; `logs/` is gitignored, so a log you want to keep
as evidence is copied into `evidence/` deliberately, after a secrets audit.

The plain form still works if you want it, and so does hand-teeing:

```bash
python -m aether.telephony.agent dev
```

If a previous call went wrong, capture the audio too so the failure names itself:

```bash
AETHER_CALL_CAPTURE=call1.wav python scripts/run_call.py
```

Open `http://127.0.0.1:8760/index.html?ws=8761` **first** — the orb sits at "waiting for a call".
Then dial. The worker logs numbered stages `[1/7]`…`[7/7]`, so a failure says exactly how far it
got, and prints a diagnosis when the call ends.

---

## The flow

| # | Do this | What to point at |
|---|---|---|
| 0 | Page open, before dialling | Orb calm grey, **"waiting for a call"**, `not recording` |
| 0b | Dial. AETHER asks the language FIRST: *"Welcome to AETHER, your hotel manager. Which language would you prefer: English, Hindi, or Spanish?"* | No hotel greeting yet — the greeting has to be in *some* language, so choosing comes first |
| 0c | Say **"English"** | *"You've reached AETHER, the hotel's manager. How may I help you?"* The rest of the call is English. Say "Hindi" or "Spanish" instead and the voice, recogniser and every answer switch with it |
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
| 16b | Say **"can we switch language"** | It offers the list *in the language being spoken* rather than guessing. Then name one and everything switches |
| 16c | Ask something the hotel has no record of — *"do you have a rooftop pool?"* | Gemini answers naturally as the duty manager. Point out `llm_ms` is non-zero here and zero on every database answer |
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
- If asked "does the phone path work": **calls connect, are answered, are understood and are
  transcribed**, and the telephony audio is measured — 28 utterances across three calls, which is
  what moved the speech floor from 35 to 2500. What is **not** separately measured is STT word
  accuracy on narrowband audio. Say that distinction rather than blurring it.

---

## If it fails

| Symptom | Do |
|---|---|
| Call connects, AETHER silent | Let the call end. The worker prints `--- call diagnostics ---` naming the first failed stage: `inbound_audio`, `listening_gate`, `vad`, `stt`, `reply`, `tts`, `outbound_audio` |
| Greeting heard, then AETHER cannot hear you | Check the `transport:` line. `pumps=2` for one caller means the track was attached twice and two readers were interleaved. `frames_failed` above zero means frames are arriving unconvertible. Re-run with `AETHER_CALL_CAPTURE=call.wav` and listen to what actually arrived |
| Verdict is `vad` | The report prints `peak_rms` / `floor_rms` next to `speech_floor`. Set `AETHER_SPEECH_FLOOR` from that and restart. Do not guess it |
| Verdict is `stt` | Speech was heard, words were not. Nothing to tune live — fall back to the local console |
| Rime rejects a turn | The turn is discarded and the session survives. There is **no fallback voice** by design; say so |
| The whole call path fails | **Switch to `python -m aether.web`.** The same brain, the same hotel database, the same fence, the same console — with a microphone instead of a phone. Say plainly that the call failed and this is the same system on a different input |
| Console will not bind its port | `AETHER_WEB_HTTP_PORT` / `AETHER_WEB_WS_PORT`, or `AETHER_WEB=0` to run the call without a UI |

---

## What not to claim

- That STT word accuracy on narrowband telephony audio has been measured. It has not.
- That any particular trace is telephony evidence. A trace does not record whether its audio came
  from the phone or the microphone, so only runs identified as calls at the time count.
- That `mistv3` sounds better than `mistv2`. Only that it reached first audio sooner and speaks
  ~15% longer, in both recorded runs.
- That the ~1.2 s menu-path turn projection is a measurement. It is arithmetic on laptop numbers.
- That anything was salvaged from a fenced generation. Nothing ever is.
- That a status query is answered aloud. It protects the task; it does not speak.
