"""Run the existing AETHER engine and serve a live view of it.

    python -m aether.web

The engine is the SAME `Day1Spike` the CLI runs -- constructed once, run on a background thread.
Nothing is duplicated and nothing is re-implemented; the browser subscribes to the trace that the
run already produces. `python -m aether.spike` is untouched and keeps working exactly as before.

The browser has exactly TWO controls, and they mean different things:

    START / STOP LISTENING   whether the microphone is open. Fences nothing, ends nothing.
    INTERRUPT                fence whatever AETHER is doing. Does not close the microphone.

It cannot otherwise steer the engine, and it never touches audio -- the microphone and speaker
belong to this process.
"""

from __future__ import annotations

import argparse
import threading
import webbrowser

from ..config import RuntimeConfig
from ..spike import HANDS_FREE, INPUT_MODES, PUSH_TO_TALK, Day1Spike
from ..trace import Trace
from .server import WebBridge


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--http-port", type=int, default=8760)
    ap.add_argument("--ws-port", type=int, default=8761)
    ap.add_argument("--stt-model", default="base.en")
    ap.add_argument("--input-device", type=int, default=None)
    ap.add_argument("--output-device", type=int, default=None)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--input-mode", choices=INPUT_MODES, default=HANDS_FREE,
                    help="hands_free (default): just speak; the button stops the agent.")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    cfg = RuntimeConfig.from_env()
    trace = Trace.new_run(cfg.trace_dir, echo=True)
    # NOT "browser". The web console is a viewer and a control surface over the SAME local
    # microphone session the CLI runs -- `Day1Spike` opens the sound device itself, and no audio
    # ever travels from the browser. Recording "browser" here would name the screen the operator was
    # looking at, not the path the caller's voice took, which is the only thing this field is for.
    trace.input_path = "local_microphone"

    spike = Day1Spike(
        trace,
        stt_model=args.stt_model,
        input_device=args.input_device,
        output_device=args.output_device,
        input_mode=args.input_mode,
    )

    # `phase` is read through a callable so the bridge never holds anything it could write to.
    bridge = WebBridge(
        phase_source=lambda: spike.barge.phase,
        listening_source=lambda: spike.mic.listening,
        # The one permitted write. Routed through `spike.interrupt`, which reaches the same
        # `fence_now` the voice path reaches -- a different trigger, never a different fence.
        on_interrupt=lambda: spike.interrupt(source="button"),
        # Standby is a separate control: it mutes the mic and fences nothing.
        on_standby=lambda: spike.toggle_standby(),
        # THE LISTENING TOGGLE. Takes the desired state, so the button and the engine cannot end
        # up disagreeing about which way the toggle is pointing.
        on_listening=lambda on: spike.set_listening(on),
        http_port=args.http_port,
        ws_port=args.ws_port,
    )
    unsubscribe = trace.subscribe(bridge.on_event)
    bridge.start()

    url = f"http://127.0.0.1:{args.http_port}/index.html?ws={args.ws_port}"
    print(f"\nAETHER web view: {url}")
    print("Two controls: the listening toggle, and INTERRUPT. "
          "The mic and speaker belong to this process.\n")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        spike.run()          # the existing loop, unchanged
    finally:
        unsubscribe()
        bridge.stop()


if __name__ == "__main__":
    main()
