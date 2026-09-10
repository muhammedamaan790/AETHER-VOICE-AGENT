# Committed evidence

## `demo-run.jsonl`

One real run of the demo script — 16 turns, spoken into the pipeline, recorded by the trace writer
as it happened. It is the artifact behind the latency and safety claims made in
[`RIME_EVIDENCE.md`](../RIME_EVIDENCE.md) and [`JUDGING.md`](../JUDGING.md).

**Input path: a real inbound telephone call**, and that is established by evidence rather than
asserted. The trace itself does not record whether audio came from a phone or a microphone — the
event vocabulary is identical on both. What settles it is `demo-call-worker.log`, the LiveKit/SIP
worker's own log for the same run: **all sixteen turns carry identical `stt`/`llm`/`tts`/`turn`
latencies in both files**, and the log is unambiguously telephony (`agent name : aether-hotel`,
`[4/7] inbound pump starting`, `pumps=1`, `inbound audio captured`).

**Traces recorded after 2026-09-10 answer this by themselves.** Each one carries `input_path` --
`telephony` or `local_microphone` -- stamped once by whichever entry point owns the session. This
file predates the field and was deliberately **not** back-filled: the value would be true, but the
file would then be claiming the run observed something it never observed, and evidence that is
edited after the fact is not evidence. So this call keeps its worker log, and future calls will not
need one.

Traces are normally gitignored (`traces/*.jsonl`). This one is committed on purpose: a claim about
latency is worth what the file behind it is worth. It was audited for secrets before being added —
no credential-shaped strings, no phone number, no token and no email. **One value was redacted:**
the LiveKit project URL appeared once, on the `registered worker` line, and is now `<redacted>` --
the same marker the trace writer uses. It is not a credential, but it identifies an account and
there is no reason to publish it. Everything else in the file is verbatim. The trace writer's
own redaction is visible as 48 `<redacted>` markers.

### What it records

| | |
|---|---|
| Turns | 16 |
| TTS | Rime `mistv3`, voice `astra`, transport `ws3` |
| LLM | `gemini:gemini-flash-lite-latest` |
| STT | `base.en` |

| Measurement | Median | Min | Max |
|---|---|---|---|
| Turn latency (speech ended → answer decided) | **1262 ms** | 1119 | 2549 |
| Response latency (speech ended → audio playing) | **2455 ms** | 1656 | 3784 |
| STT | 927 ms | 841 | 1147 |
| **Rime first audio** | **280 ms** | 270 | 346 |

**11 of the 16 turns ran at `llm_ms = 0`** — answered from `data/aether_hotel.db` with no model call
at all. **2 generations were fenced** (`TaskReplaced`) and **0 results leaked** (`ResultLeaked`
never appears), which is the golden invariant holding on a real run rather than in a test.

### The five turns the recogniser mangled

This is the part worth reading. `base.en` misheard five inputs, and not one produced a fabricated
fact:

| Heard | Spoken back | llm_ms |
|---|---|---|
| "How much is a daily speed?" | "I am not quite sure what you mean by a daily speed, but I can help you with our room rates or menu if you like." | 1386 |
| "How much is deluxe heat?" | "I am not sure what you mean by deluxe heat, but our Deluxe King room is eight thousand five hundred rupees a night." | 1004 |
| "How much is daylight skis?" | "I am sorry, I did not catch that. Could you please repeat it?" | 1090 |
| "is room one, three," | "Room numbers run from one oh one to five ten." | 775 |
| "What comes with an executive suit?" | the exact Executive Suite amenities from the database | 1360 |

Three admitted confusion; two recovered the correct fact from the hotel injected into the prompt.
None invented a price, a room or a dish. That is the safety net described in
[`aether/hotel/__init__.py`](../aether/hotel/__init__.py) (`menu_for_prompt`) working on real audio.

### What this run also caused

Two of those mishearings were defects, not bad luck, and both are now fixed in
`aether/hotel/router.py` with tests in `tests/test_hotel_db.py`:

- *"executive suit"* should never have needed the model. `suite` is pronounced "sweet", so the
  recogniser returns `suit` or `sweet`; the router now repairs that before matching, and the turn
  costs 0 ms instead of 1360 ms.
- Separately, *"Do you have room for this?"* (a mishearing of *"do you have room service"*) was
  being answered with the list of room types — a confident answer to a question nobody asked. It
  now falls through.

Replaying all 218 distinct utterances in `traces/` through the router before and after: **2 more are
answered deterministically, 1 confidently-wrong answer is gone, and 0 utterances changed to a
different tool.**

### Reading it

```bash
python -c "import json;[print(e['type'], e.get('text','')) for e in map(json.loads, open('evidence/demo-run.jsonl', encoding='utf-8'))]"
```

Fields on `ResponseSpoken` carry the per-stage timings (`stt_ms`, `llm_ms`, `tts_ms`,
`turn_latency_ms`, `response_latency_ms`) and the provider identity used for that turn.
