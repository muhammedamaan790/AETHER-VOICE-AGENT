"""Run the existing AETHER engine and serve a live view of it.

    python -m aether.web

The engine is the SAME `Day1Spike` the CLI runs -- constructed once, run on a background thread.
Nothing is duplicated and nothing is re-implemented; the browser subscribes to the trace that the
run already produces. `python -m aether.spike` is untouched and keeps working exactly as before.

Observation only in this pass: the browser cannot start, stop or steer the engine. The microphone
and speaker belong to this process, so speak to this machine and watch the page follow.
"""

from __future__ import annotations

import argparse
import threading
import webbrowser

from ..config import RuntimeConfig
from ..spike import Day1Spike
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
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    cfg = RuntimeConfig.from_env()
    trace = Trace.new_run(cfg.trace_dir, echo=True)

    spike = Day1Spike(
        trace,
        stt_model=args.stt_model,
        input_device=args.input_device,
        output_device=args.output_device,
    )

    # `phase` is read through a callable so the bridge never holds anything it could write to.
    bridge = WebBridge(
        phase_source=lambda: spike.barge.phase,
        http_port=args.http_port,
        ws_port=args.ws_port,
    )
    unsubscribe = trace.subscribe(bridge.on_event)
    bridge.start()

    url = f"http://127.0.0.1:{args.http_port}/index.html?ws={args.ws_port}"
    print(f"\nAETHER web view: {url}")
    print("Observation only -- the mic and speaker belong to this process.\n")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        spike.run()          # the existing loop, unchanged
    finally:
        unsubscribe()
        bridge.stop()


if __name__ == "__main__":
    main()
