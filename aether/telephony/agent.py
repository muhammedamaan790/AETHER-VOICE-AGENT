"""LiveKit worker: carries a phone call into AETHER's existing pipeline and back out.

    python -m aether.telephony.agent dev        # register and wait for calls
    python -m aether.telephony.agent start      # production run

**Not** an `AgentSession`. That class supplies its own STT, LLM, TTS and turn detection, and using
it would mean the judged path ran LiveKit's interruption logic rather than AETHER's — the
GenerationRegistry, the AudioGate's per-chunk generation tagging and the four-layer fence would all
sit unused beside it. This uses `livekit.rtc` directly: subscribe to the caller's track, publish
one of our own, and let the existing pipeline do everything in between.

    caller ──▶ rtc.AudioStream ──to_chunk──▶ InboundBridge ──▶ MicVAD ──▶ (STT ▸ LLM/menu ▸ Rime)
                                                                                      │
    caller ◀── rtc.AudioSource ◀──to_frame_args── OutboundBridge ◀── AudioGate ◀───────┘

Everything between the two bridges is unchanged code, so a fence that works on the laptop works on
the call — it is the same `AudioGate._callback` either side of the swap.

Threading: LiveKit is asyncio. AETHER's realtime path is threaded (MEMORY.md locked decision 14 —
asyncio is not the mechanism *inside* the audio callback, and may be used outside it). Inbound
frames are handed to `process_frame` from an async task, which is a plain function call; the turn
loop runs on its own thread exactly as it does locally. Nothing new runs on a realtime callback.

Scope: this registers, connects, bridges audio and tears down cleanly. A real call has now reached
it -- the caller heard the greeting, and AETHER could not understand a word they said. The cause was
the caller's track being attached on both discovery paths, so two `rtc.AudioStream` readers
interleaved into one bridge; `start_inbound` now deduplicates by track sid. **The fix has not itself
been exercised by a call.**

Thresholds are still laptop-calibrated and are expected to need re-deriving on real telephony audio
(RIME_EVIDENCE.md Part 6, which is still all placeholders).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import wave

import numpy as np
from livekit import rtc
from livekit.agents import AgentServer, JobContext, cli

from ..audio.player import AudioGate
from ..bridge import InboundBridge, OutboundBridge
from ..prewarm import prewarm
from ..spike import HANDS_FREE, Day1Spike
from ..trace import Trace
from ..web.server import WebBridge
from . import AGENT_NAME, TRANSPORT_CHANNELS, TRANSPORT_SAMPLE_RATE, LiveKitConfig
from .diagnostics import diagnose, format_report
from .frames import to_chunk, to_frame_args

logger = logging.getLogger("aether.telephony")

server = AgentServer()


class CallBridge:
    """One phone call, wired to one AETHER pipeline.

    Owns only the plumbing: the LiveKit source/stream, the two adapters, and the tasks that move
    audio. The pipeline it drives is the ordinary `Day1Spike` — constructed without audio devices,
    because on a call the transport is the device.
    """

    def __init__(self, spike: Day1Spike, gate: AudioGate, closing: asyncio.Event):
        self.spike = spike
        self.inbound = InboundBridge(spike.mic, source_rate=TRANSPORT_SAMPLE_RATE)
        self.outbound = OutboundBridge(gate, sink_rate=TRANSPORT_SAMPLE_RATE)
        self.source = rtc.AudioSource(TRANSPORT_SAMPLE_RATE, TRANSPORT_CHANNELS)
        self._tasks: list[asyncio.Task] = []
        # Owned by the ENTRYPOINT and passed in, not created here. When the bridge owned it, the
        # only code that set it was `aclose()` -- which the entrypoint calls in its `finally`,
        # i.e. after the wait it was supposed to release. That circular wait is why the first real
        # call hung until LiveKit killed it with "entrypoint did not exit in time". The room's
        # disconnect handlers now set this, so the wait is released by the call actually ending.
        self._closing = closing
        # Frames that arrived and could not be converted. Counted rather than fatal -- see
        # `pump_inbound` -- and surfaced in the call diagnostics, because "audio arrived but none
        # of it was usable" must not look like "no audio arrived".
        self.frames_failed = 0
        # Every track this bridge has already started a pump for, by sid. See `hotel_call.attach`.
        self.attached: set[str] = set()
        # Opt-in capture of exactly what the VAD was handed. Off unless AETHER_CALL_CAPTURE is set.
        self._capture: list[np.ndarray] = []
        self._capture_path = os.environ.get("AETHER_CALL_CAPTURE", "").strip() or None

    # --- audio in ---------------------------------------------------------------------

    async def pump_inbound(self, track: rtc.Track) -> None:
        """Caller audio → AETHER. Ends when the track does, which is how a hangup arrives.

        A frame that cannot be converted is COUNTED AND SKIPPED, not fatal. The `try` used to wrap
        the whole loop, so one malformed frame ended inbound audio for the remainder of the call
        while the room, the outbound pump and the turn loop all kept running: the call stayed up,
        the greeting had already played, and AETHER was deaf from that moment on with a single line
        in the log to say so. A bad frame is 20 ms of audio; it must cost 20 ms, not the call.

        Only a failure of the stream itself ends the pump, because at that point there is no more
        audio to read.
        """
        stream = rtc.AudioStream(track)
        try:
            async for event in stream:
                if self._closing.is_set():
                    break
                try:
                    # `push` resamples, rebuffers into exact VAD frames, and honours STANDBY. A
                    # frame of the wrong size would otherwise be dropped silently by
                    # `process_frame`.
                    self.inbound.push(to_chunk(event.frame))
                except Exception:
                    self.frames_failed += 1
                    # Only the first few, then silence: a systematically bad stream would
                    # otherwise fill the log at fifty lines a second and bury everything else.
                    # The count is what matters, and it is reported in the call diagnostics.
                    if self.frames_failed <= 3:
                        logger.exception("inbound frame %d could not be converted; skipping",
                                         self.frames_failed)
        except Exception:
            logger.exception("inbound stream ended early")
        finally:
            await stream.aclose()

    # --- audio out --------------------------------------------------------------------

    async def pump_outbound(self) -> None:
        """AETHER → caller, paced in real time.

        Pulls whole blocks from `AudioGate._callback`, so generation tagging, ducking and
        flush-on-fence all happen in their usual place. Silence is published rather than skipped:
        a gap in a phone stream is heard as a dropout, not as quiet.
        """
        period = self.outbound.block_ms / 1000.0
        next_at = asyncio.get_running_loop().time()
        while not self._closing.is_set():
            chunk = self.outbound.pull()
            try:
                await self.source.capture_frame(rtc.AudioFrame(*to_frame_args(chunk)))
            except Exception:
                logger.exception("outbound pump stopped")
                return
            next_at += period
            await asyncio.sleep(max(0.0, next_at - asyncio.get_running_loop().time()))

    # --- lifecycle --------------------------------------------------------------------

    def start_outbound(self) -> None:
        self._tasks.append(asyncio.create_task(self.pump_outbound()))

    def start_inbound(self, track: rtc.Track) -> bool:
        """Start one pump for this track. Returns False if it already has one.

        THE DEDUP, and it belongs here rather than at the call site because this object owns the
        pumps. A track is discovered on two paths -- the `track_subscribed` event, and the sweep of
        already-subscribed publications after the pipeline is built -- and in the ordinary timing
        of a call BOTH fire for the same track: the event lands during the ~9 s build and is
        queued, then the sweep finds the same publication once it is subscribed.

        Without this check that started two `rtc.AudioStream` readers on one track, both pushing
        into ONE `InboundBridge` with one `_carry` buffer. Every frame arrived twice, interleaved
        non-deterministically, and the 320-sample rebuffering was scrambled across two producers --
        so the caller heard the greeting and AETHER could not understand a word, while
        `samples_received` looked healthy because it had doubled.

        Both discovery paths are kept: either can legitimately be the one that fires, and losing
        the track entirely is the bug that pair was written to fix.
        """
        sid = getattr(track, "sid", None) or id(track)
        if sid in self.attached:
            return False
        self.attached.add(sid)
        if self._capture_path:
            # Tap the frames the VAD is handed -- after resampling and rebuffering -- so the
            # capture is what AETHER heard, not what the transport sent.
            self.inbound.on_frame = self._capture.append
        self._tasks.append(asyncio.create_task(self.pump_inbound(track)))
        return True

    def write_capture(self) -> str | None:
        """Write the captured inbound audio, if capture was enabled. Returns the path.

        Off unless AETHER_CALL_CAPTURE names a file. This records a real caller's voice, so it is
        a deliberate act and never a default.

        Never raises: a failed capture must not disturb the teardown of a call.
        """
        if not self._capture_path or not self._capture:
            return None
        try:
            pcm = np.concatenate(self._capture)
            # Open the file FIRST, then hand the handle to `wave`. Letting `wave.open` do the
            # opening leaves a half-built `Wave_write` behind when the path is bad, whose
            # destructor then raises during garbage collection -- noise in an unrelated place,
            # long after the failure this method already handled.
            with open(self._capture_path, "wb") as handle:
                with wave.open(handle, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(self.spike.mic.samplerate)
                    wav.writeframes(pcm.tobytes())
            return self._capture_path
        except Exception:
            logger.exception("call capture could not be written")
            return None

    async def aclose(self) -> None:
        """Stop both pumps and fence anything in flight.

        The fence matters on teardown: without it a generation whose audio was mid-flight would be
        left active, and a late Rime chunk could be enqueued against a call that no longer exists.
        """
        self._closing.set()
        self.spike.barge.fence_now(reason="call_disconnected")
        self.inbound.reset()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()


def start_console(spike: Day1Spike, trace: Trace) -> WebBridge | None:
    """Serve the AETHER console for this call, and wire its two controls to this pipeline.

    The SAME `WebBridge` the local-mic path uses, wired to the SAME `Day1Spike` methods. That is
    what makes the toggle honest on a phone call rather than decorative: STOP LISTENING closes
    `MicVAD`, `InboundBridge.push` then drops every inbound block, and the LiveKit room, the
    published track and the outbound pump all keep running -- the caller is still connected and
    AETHER simply cannot hear them.

    Best-effort. A console that fails to bind must never take a call down, so every failure is
    swallowed and the call proceeds without a UI. Disable entirely with AETHER_WEB=0.
    """
    if os.environ.get("AETHER_WEB", "1").strip() == "0":
        return None
    try:
        console = WebBridge(
            phase_source=lambda: spike.barge.phase,
            listening_source=lambda: spike.listening,
            # The one fence the browser may ask for -- the same `fence_now` the caller's voice
            # reaches, distinguished only by `reason` in the trace.
            on_interrupt=lambda: spike.interrupt(source="button"),
            on_listening=spike.set_listening,
            on_standby=spike.toggle_standby,
            http_port=int(os.environ.get("AETHER_WEB_HTTP_PORT", "8760")),
            ws_port=int(os.environ.get("AETHER_WEB_WS_PORT", "8761")),
        )
        unsubscribe = trace.subscribe(console.on_event)
        console.start()
        console._aether_unsubscribe = unsubscribe   # released in the call's teardown
        logger.info("[5/7] console on http://127.0.0.1:%s/index.html?ws=%s",
                    console.http_port, console.ws_port)
        return console
    except Exception:
        logger.exception("console did not start; the call continues without a UI")
        return None


def stop_console(console: WebBridge | None) -> None:
    if console is None:
        return
    try:
        unsubscribe = getattr(console, "_aether_unsubscribe", None)
        if unsubscribe is not None:
            unsubscribe()
        console.stop()
    except Exception:
        logger.exception("console teardown failed")


# Spoken, so: no numerals, no symbols, short enough that a caller can interrupt it comfortably.
# Identifies AETHER as the hotel's manager rather than as an assistant or a system.
GREETING = "You've reached AETHER, the hotel's manager. How may I help you?"


def speak_greeting(spike: Day1Spike, text: str = GREETING) -> bool:
    """Say the opening line through the ordinary Rime path. Returns whether audio was accepted.

    Uses the SAME machinery a normal turn uses -- a real generation from the coordinator, the gate
    told which generation may play, and `is_valid` polled per chunk -- so the greeting is
    interruptible exactly like any other answer. A caller who starts talking over it fences it, and
    the half-spoken greeting is discarded rather than finishing over them.

    Blocking: this synthesises and enqueues audio, so it belongs on a worker thread. Calling it
    from the event loop would stall LiveKit the same way constructing the pipeline did.

    Never raises. A greeting that fails must not take the call down -- the caller can still speak,
    and the trace records why nothing was said.
    """
    gen = spike.barge.begin_turn(turn_id=0)
    try:
        spike.gate.set_active_generation(gen.id, turn_id=0)
        result = spike.rime.speak(
            text, gate=spike.gate, gen=gen.id, turn_id=0,
            is_valid=lambda: spike.barge.is_valid(gen.id),
        )
        return bool(result.accepted)
    except Exception:
        logger.exception("greeting failed; the call continues without it")
        return False
    finally:
        spike.barge.end_turn()


def build_pipeline(trace: Trace) -> Day1Spike:
    """The ordinary AETHER pipeline, with no audio devices of its own.

    `Day1Spike` normally opens PortAudio for the microphone and speaker. On a call the transport
    is the device, so both are suppressed — but every component above them (VAD, STT, LLM, Rime,
    the gate, the registry, the coordinator) is constructed exactly as the local path constructs
    it. That is the point: one brain, two transports.
    """
    return Day1Spike(trace, input_mode=HANDS_FREE)


@server.rtc_session(agent_name=AGENT_NAME)
async def hotel_call(ctx: JobContext) -> None:
    """Runs once per inbound call."""
    config = LiveKitConfig.from_env()
    logger.info("[1/7] call starting: project=%s room=%s", config.project_host, ctx.room.name)

    closing = asyncio.Event()
    bridge: CallBridge | None = None
    pending_tracks: list[rtc.Track] = []

    def attach(track: rtc.Track, source: str) -> None:
        """Start the inbound pump, or hold the track until the pipeline exists.

        The pipeline takes ~9 s to build, and LiveKit subscribes the caller's track within
        milliseconds of connect. Without this queue that event lands in the gap and is lost
        forever -- which is why the first real call produced no inbound audio at all.
        """
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if bridge is None:
            logger.info("[3/7] caller audio (%s) queued until the pipeline is ready", source)
            pending_tracks.append(track)
        elif bridge.start_inbound(track):
            logger.info("[4/7] inbound pump starting (%s)", source)
        else:
            # Expected, not an error: both discovery paths found the same track. Logged so the
            # log still shows what happened rather than silently dropping a line.
            logger.info("[4/7] track already has a pump (%s); not starting a second", source)

    # --- handlers FIRST, before connect ------------------------------------------------
    # Registering these after `connect()` is what lost the track-subscribed event on call one.

    @ctx.room.on("track_subscribed")
    def _on_track(track: rtc.Track, *_args) -> None:
        attach(track, "subscribed")

    @ctx.room.on("disconnected")
    def _on_disconnected(*_args) -> None:
        logger.info("[7/7] room disconnected")
        closing.set()

    @ctx.room.on("participant_disconnected")
    def _on_participant_left(participant, *_args) -> None:
        # The caller hanging up ends the call; there is nobody left to talk to.
        logger.info("[7/7] caller left: %s", getattr(participant, "identity", "?"))
        closing.set()

    await ctx.connect()
    logger.info("[2/7] connected to room=%s", ctx.room.name)

    trace = Trace.new_run("traces", echo=False)
    # OFF THE EVENT LOOP. Loading Whisper, negotiating PortAudio and opening both device streams
    # measured 9051 ms; running that inline stalled LiveKit's heartbeats and event dispatch for
    # the whole of call setup.
    spike = await asyncio.to_thread(build_pipeline, trace)
    bridge = CallBridge(spike, spike.gate, closing)
    logger.info("[2/7] pipeline ready: llm=%s rime=%s", spike.llm.name, spike.rime.configured)
    console = start_console(spike, trace)

    # Anything that arrived during those 9 s, plus anything already subscribed before we attached
    # the handler at all. Both paths, because either can be the one that fires.
    for track in pending_tracks:
        attach(track, "queued")
    pending_tracks.clear()
    for participant in ctx.room.remote_participants.values():
        logger.info("[3/7] participant present: %s", participant.identity)
        for publication in participant.track_publications.values():
            if publication.track is not None:
                attach(publication.track, "already-subscribed")

    track = rtc.LocalAudioTrack.create_audio_track("aether", bridge.source)
    await ctx.room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    bridge.start_outbound()
    logger.info("[5/7] outbound track published, pump running")

    # The turn loop is the same threaded loop the local path runs; it is not asyncio-driven.
    turns = threading.Thread(target=spike.run_turns, name="aether-turns", daemon=True)
    turns.start()

    # Greet on a worker thread: synthesis blocks, and the loop must stay free to carry the audio
    # the greeting is producing.
    logger.info("[6/7] greeting starting")
    spoken = await asyncio.to_thread(speak_greeting, spike)
    logger.info("[6/7] greeting %s", "spoken" if spoken else "produced no audio")

    try:
        await closing.wait()
    finally:
        logger.info("call ending, tearing down")
        await bridge.aclose()
        spike.stop_turns()
        # BEFORE `shutdown()` closes the trace. The report is read out of the trace, so ordering
        # it after teardown would produce an empty diagnosis of the call that just happened.
        try:
            report = diagnose(trace, inbound=bridge.inbound, outbound=bridge.outbound)
            report["inbound_frames_failed"] = bridge.frames_failed
            report["inbound_pumps"] = len(bridge.attached)
            logger.info("\n%s", format_report(report))
        except Exception:
            logger.exception("diagnostics failed")
        captured = bridge.write_capture()
        if captured:
            logger.info("inbound audio captured: %s", captured)
        stop_console(console)
        spike.shutdown()
        logger.info("[7/7] call ended: trace=%s", trace.path)


def main() -> None:
    """Preflight the credentials, then hand off to LiveKit's CLI."""
    from dotenv import load_dotenv

    load_dotenv()

    config = LiveKitConfig.from_env()
    if not config.configured:
        # Names only. A missing-credential message must never become a disclosure.
        raise SystemExit(
            "LiveKit is not configured; missing " + ", ".join(config.missing()) + ". "
            "Set them for the project this worker should join (see .env.example)."
        )
    print(f"agent name : {AGENT_NAME}")
    print(f"project    : {config.project_host}")

    # BEFORE the worker registers, so the process is warm before any call can arrive. Jobs run
    # with `JobExecutorType.THREAD` by default, i.e. in this same process, so the imports and the
    # page-cached Whisper weights are inherited by every call. Measured on this machine: 3810 ms
    # cold, 672 ms warm -- about 3.1 s of setup a caller no longer waits through.
    warm = prewarm()
    print("prewarm    : " + "  ".join(f"{k}={v}ms" for k, v in warm.items()))

    cli.run_app(server)


if __name__ == "__main__":
    main()
