# Setting up AETHER on your own computer

Every step, in order, for someone who has never run this project. It takes about **30 minutes** the
first time, most of it waiting for downloads. The commands are for **Windows PowerShell** first, with
macOS and Linux shown where they differ.

If you only want to see it work, steps 1–6 are enough — no phone and no microphone needed.

---

## 0. What you need

- **A computer** running Windows 10/11, macOS or Linux, with an internet connection.
- **Python 3.11, 3.12 or 3.13.** AETHER is developed on 3.13.2. Check with `python --version`.
  If you don't have it, install it from <https://www.python.org/downloads/> and, on Windows, tick
  **"Add python.exe to PATH"** in the installer.
- **Git.** Check with `git --version`, or install it from <https://git-scm.com/downloads>.
- **Three free accounts**, for the API keys in step 4: **Rime** (the voice), **Google AI Studio**
  (Gemini, the language model) and **LiveKit Cloud** (the phone line).
- **For phone calls:** a phone number connected to LiveKit (step 8), and a phone to ring it from.
- **Strongly recommended:** a headset or earphones with a microphone.

---

## 1. Get the code

Open **PowerShell** (Windows) or **Terminal** (macOS/Linux) and run:

```powershell
git clone https://github.com/muhammedamaan790/AETHER-VOICE-AGENT.git
cd AETHER-VOICE-AGENT
```

The first line downloads the project; the second moves into its folder. **Every command from here on
is run inside this folder.**

---

## 2. Create a Python environment

A virtual environment keeps AETHER's packages separate from everything else on your computer.

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell says running scripts is disabled, run this once, answer **Y**, then run the
`Activate.ps1` line again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt now starts with `(.venv)`. **Every time you open a new window to work on AETHER, run the
activate line again** — otherwise Python won't find the packages you install next.

---

## 3. Install the packages

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

This takes a few minutes. It installs the speech recogniser (faster-whisper), the LiveKit telephony
libraries, the Rime and Gemini clients, and the test tools. A few warnings at the end are normal; an
error ending in `ERROR:` is not — see *Troubleshooting* at the bottom.

---

## 4. Get your API keys

You need **five values** from three websites. Keep them somewhere safe for step 5. Never paste them
into a chat, an email or a commit.

**Rime — the voice** (`RIME_API_KEY`)

1. Go to <https://app.rime.ai> and sign up.
2. Open the **API keys** page and create a key.
3. Copy it.

**Gemini — the language model** (`GEMINI_API_KEY`)

1. Go to <https://aistudio.google.com/app/apikey> and sign in with a Google account.
2. Click **Create API key**.
3. Copy it.

**LiveKit — the phone line** (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`)

1. Go to <https://cloud.livekit.io> and sign up.
2. Create a **project**.
3. Open the project's **Settings → Keys** and create a key.
4. Copy three things: the **URL** (it starts `wss://` and ends `.livekit.cloud`), the **API key**,
   and the **API secret**. The secret is shown only once.

---

## 5. Put the keys in a `.env` file

AETHER reads its keys from a file called `.env`. Copy the template, then open it:

**Windows:**

```powershell
Copy-Item .env.example .env
notepad .env
```

**macOS / Linux:**

```bash
cp .env.example .env
nano .env
```

Find each of these lines and paste your value straight after the `=`, with no spaces and no quotes:

```ini
RIME_API_KEY=your-rime-key
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-gemini-key
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-livekit-key
LIVEKIT_API_SECRET=your-livekit-secret
```

Save and close. **Leave every other line as it is** — the defaults are the configuration the demo
uses. Never put a comment on the same line as a value: everything after `=` is read as the value.

`.env` is listed in `.gitignore`, so it can never be committed by accident.

---

## 6. Check that it works — no phone, no microphone

```powershell
python -m pytest -q
```

This runs the test suite. Expect **1528 passed, 2 skipped** after about three minutes. The two skips are
features that deliberately don't exist, and each test says which.

```powershell
python scripts/demo_full_call.py
```

This runs a whole scripted call through AETHER — many turns, an interruption, and switches to Hindi,
to Spanish and back to English — with no phone and no microphone, and ends with **PASS**. Add `--mode live` to use the real
Gemini and Rime services, which checks your keys too.

**If both pass, AETHER is installed correctly.**

---

## 7. Download the speech models (once)

```powershell
python -m aether.prewarm
```

The first time, this downloads the speech-recognition models — a few hundred megabytes — and prints
how long each step took. After that it takes about a second. Do this before your first call, so the
first caller isn't kept waiting while a model downloads.

---

## 8. Connect a phone number (once)

AETHER answers calls through LiveKit. LiveKit needs three things to send a phone call to it:

1. **A phone number.** In LiveKit Cloud, open **Telephony** and get a number, or connect one you
   already have through a SIP provider such as Twilio or Telnyx.
2. **An inbound trunk** for that number, so LiveKit accepts calls to it.
3. **A dispatch rule** that sends each call to AETHER. It must:
   - create an **individual room per call**, with the room prefix **`aether-call-`**;
   - dispatch the agent named exactly **`aether-hotel`**. That is the name AETHER registers under.
     A rule with any other name rings into an empty room, and the caller hears nothing.

You can create all three in the LiveKit Cloud dashboard. With the LiveKit CLI (`lk`), the dispatch
rule looks like this. Check the LiveKit SIP documentation (<https://docs.livekit.io/sip/>) for the
current format, which may change:

```json
{
  "name": "aether-hotel",
  "rule": { "dispatchRuleIndividual": { "roomPrefix": "aether-call-" } },
  "roomConfig": { "agents": [ { "agentName": "aether-hotel" } ] }
}
```

You only do this once per number.

---

## 9. Start the agent — every time you want to take calls

```powershell
python scripts/reset_hotel_db.py
python scripts/run_call.py
```

Line by line:

1. **`reset_hotel_db.py`** starts from a fresh hotel. Bookings made while you practise are kept in a
   working copy (`data/aether_hotel.live.db`) and never in the committed database. This line
   throws that working copy away, so the demo's booking reads out the room and reference in the
   script.
2. **`run_call.py`** starts AETHER and connects it to LiveKit. It prints the address of the
   console, then warms up.

**Wait until you see `registered worker`.** AETHER is now ready for a call. Leave this window open.

3. **Open the console** in a browser at the address it printed — normally
   <http://127.0.0.1:8760/index.html?ws=8761>. It shows *"Standing by — the line is open"*.
4. **Ring your LiveKit number.** AETHER waits until your phone is listening, then asks which language
   you'd like.
5. **To finish:** hang up the phone first, **then** press **Ctrl+C** in the agent window. Hanging up
   first lets AETHER print its end-of-call diagnostics.

Each run writes a log to `logs\worker-<date and time>.log`, and every call's events go to
`traces\`. Nothing is overwritten.

To also record what AETHER hears from the caller, for diagnosing a bad line, set this before
`run_call.py`. The audio is saved next to the log:

```powershell
$env:AETHER_CALL_CAPTURE="1"      # Windows
```
```bash
export AETHER_CALL_CAPTURE=1      # macOS / Linux
```

This records a real person's voice, so only use it on your own test calls.

---

## 10. Other ways to run it

**Talk to it with your laptop microphone — no phone needed:**

```powershell
python -m aether.web
```

The console opens in your browser. Click **Start Listening** and talk. **Wear headphones:** without
them, AETHER hears its own voice through the microphone and interrupts itself.

**Watch a whole call run by itself — no microphone:**

```powershell
python scripts/demo_full_call.py                 # free, offline
python scripts/demo_full_call.py --mode live     # real Gemini and Rime
```

**Don't run `python -m aether.web` and `run_call.py` at the same time.** Each is a complete AETHER,
and they compete for the same ports.

---

## 11. Recording the demo with Windows Phone Link

With Phone Link, your phone's call audio goes through the laptop's microphone and speakers. That
works well if you do these four things:

1. **Use a headset on the laptop.** This matters most. With open speakers, AETHER's voice comes out
   of the laptop, goes back into the laptop microphone and down the phone line. AETHER then hears
   itself and treats it as an interruption.
2. **Turn off Windows voice processing on the microphone.** Go to **Settings → System → Sound**,
   choose your microphone, and turn off **Audio enhancements** — and **Voice clarity** if your laptop
   has it. These can chop speech into fragments: on one test call only single words like "menu"
   reached AETHER, with pure silence between them.
3. **Record both sides.** The Xbox Game Bar (**Win + Alt + R**) records the active window with
   system audio. To capture the console, AETHER's voice *and* your microphone together, OBS Studio
   with *Display Capture*, *Desktop Audio* and *Mic/Aux* is the reliable choice.
4. **Make a 20-second test call first.** Say **"English"**, then **"menu"**. In the agent window you
   should see lines starting `LANGUAGE:` and `MENU[`.

If AETHER ever says *"Hello, are you there?"*, your voice isn't reaching it — check the headset,
Voice clarity and mute, then redial. If you hear nothing at all after it answers, hang up and redial:
the audio connection didn't come up.

The full recording script is in [DEMO_SCRIPT.md](DEMO_SCRIPT.md).

---

## Troubleshooting

| What you see | What it means and what to do |
|---|---|
| `python` is not recognised | Python isn't installed or isn't on PATH. Reinstall it and tick "Add python.exe to PATH". |
| `No module named ...` | The environment isn't active. Run the `Activate.ps1` (or `source`) line from step 2 again. |
| `LiveKit is not configured; missing ...` | A LiveKit value is missing from `.env`. The message names which one. |
| Never prints `registered worker` | The LiveKit URL, key or secret is wrong, or the network blocks it. Check `.env`. |
| The call rings but nothing answers | The dispatch rule doesn't send calls to `aether-hotel` (step 8). |
| It answers but you hear nothing | The audio connection didn't come up. Hang up and redial. With Phone Link, check the laptop's audio output. |
| AETHER says *"Hello, are you there?"* | Your voice isn't reaching it. Check headset, mute and Voice clarity (step 11). |
| AETHER keeps saying *"could you say it again?"* | Only fragments of your speech are arriving. Speak close to the microphone and turn off noise suppression. |
| It interrupts itself | Its own voice is reaching the microphone. Use headphones. |
| It speaks but never replies with facts | Rime or Gemini keys are wrong. Run `python scripts/demo_full_call.py --mode live`. |
| `Address already in use` / port 8760 | Another AETHER is already running. Close it first. |
| The first call is slow | Run `python -m aether.prewarm` once before calling. |

At the end of every call the agent window prints a **diagnosis** naming the first stage that produced
nothing — inbound audio, listening, speech detection, recognition, reply, voice, outbound audio. It's
the quickest way to see where a failed call went wrong.
