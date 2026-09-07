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

Scope: this registers, connects, bridges audio and tears down cleanly. **No call has been made with
it.** Thresholds are still laptop-calibrated and are expected to need re-deriving on real telephony
audio (see the plan's §6).
"""

from __future__ import annotations

import asyncio
import logging
import threading

from livekit import rtc
from livekit.agents import AgentServer, JobContext, cli

from ..audio.player import AudioGate
from ..bridge import InboundBridge, OutboundBridge
from ..spike import HANDS_FREE, Day1Spike
from ..trace import Trace
from . import AGENT_NAME, TRANSPORT_CHANNELS, TRANSPORT_SAMPLE_RATE, LiveKitConfig
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

    # --- audio in ---------------------------------------------------------------------

    async def pump_inbound(self, track: rtc.Track) -> None:
        """Caller audio → AETHER. Ends when the track does, which is how a hangup arrives."""
        stream = rtc.AudioStream(track)
        try:
            async for event in stream:
                if self._closing.is_set():
                    break
                # `push` resamples, rebuffers into exact VAD frames, and honours STANDBY. A frame
                # of the wrong size would otherwise be dropped silently by `process_frame`.
                self.inbound.push(to_chunk(event.frame))
        except Exception:
            logger.exception("inbound pump stopped")
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

    def start_inbound(self, track: rtc.Track) -> None:
        self._tasks.append(asyncio.create_task(self.pump_inbound(track)))

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


# Spoken, so: no numerals, no symbols, short enough that a caller can interrupt it comfortably.
# Identifies AETHER as the hotel's manager rather than as an assistant or a system.
GREETING = "You've reached AETHER, the hotel manager. How may I help you?"


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
        else:
            logger.info("[4/7] inbound pump starting (%s)", source)
            bridge.start_inbound(track)

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
    cli.run_app(server)


if __name__ == "__main__":
    main()
