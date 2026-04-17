"""CLI for the demo engine.

Runs a scenario against a mosquitto broker so an operator can record
marketing footage, verify the UI against real telemetry, or validate
an integration by hand.

Usage:
    python -m backend.modules.printers.telemetry.demo_cli <scenario> [options]

Examples:
    # run dramatic-failure at 10× speed with narration captions
    python -m backend.modules.printers.telemetry.demo_cli dramatic-failure --speed 10

    # start happy-farm and pause immediately (resume via SIGCONT or interactive mode)
    python -m backend.modules.printers.telemetry.demo_cli happy-farm --pause

    # interactive controls via stdin
    python -m backend.modules.printers.telemetry.demo_cli dramatic-failure --interactive
    > pause
    > resume
    > speed 5
    > seek 2026-04-16T15:02:00Z
    > quit

Points ODIN at the demo broker by setting `ODIN_BAMBU_BROKER_URL` to
the printed URL before starting the backend. The broker dies when
this CLI exits.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from backend.modules.printers.telemetry.demo import (
    DemoEngine,
    DemoMarker,
    SCENARIOS_DIR,
    FIXTURES_DIR,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="demo_cli",
        description="Run a telemetry demo scenario against a live MQTT broker.",
    )
    parser.add_argument(
        "scenario",
        help="Scenario name (matches a directory in demo_scenarios/).",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Playback multiplier (1 = wall-clock, 10 = 10×, 100+ for tests).",
    )
    parser.add_argument(
        "--pause",
        action="store_true",
        help="Start paused. Use --interactive to resume.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Read commands from stdin: pause, resume, speed N, seek ISO, loop ISO1 ISO2, clear-loop, quit.",
    )
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=SCENARIOS_DIR,
        help="Override scenarios directory.",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=FIXTURES_DIR,
        help="Override fixtures directory.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    def on_marker(m: DemoMarker) -> None:
        print(f"[{m.at_iso}] {m.label}", flush=True)

    engine = DemoEngine.from_scenario(
        args.scenario,
        scenarios_dir=args.scenarios_dir,
        fixtures_dir=args.fixtures_dir,
        speed=args.speed,
        on_marker=on_marker,
    )

    if args.pause:
        engine.pause()

    engine.start()
    print(f"demo engine started: scenario={engine.scenario.name}", flush=True)
    print(f"broker URL (point ODIN at this): {engine.broker_url}", flush=True)
    print(f"printers: {[p.id for p in engine.scenario.printers]}", flush=True)

    # Graceful shutdown on SIGINT
    shutdown = threading.Event()

    def _on_signal(signum, frame):
        print(f"\nreceived signal {signum}, stopping...", flush=True)
        shutdown.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        if args.interactive:
            _run_interactive(engine, shutdown)
        else:
            # block until fixture done or signal
            while not shutdown.is_set():
                if engine.wait_until_done(timeout=0.5):
                    break
    finally:
        engine.stop()
        print("demo engine stopped.", flush=True)

    return 0


def _run_interactive(engine: DemoEngine, shutdown: threading.Event) -> None:
    print("interactive mode. Commands: pause | resume | speed N | seek ISO | loop ISO1 ISO2 | clear-loop | quit", flush=True)
    while not shutdown.is_set():
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd in ("q", "quit", "exit"):
                break
            elif cmd == "pause":
                engine.pause()
                print("paused", flush=True)
            elif cmd == "resume":
                engine.resume()
                print("resumed", flush=True)
            elif cmd == "speed" and len(parts) == 2:
                engine.set_speed(float(parts[1]))
                print(f"speed={parts[1]}", flush=True)
            elif cmd == "seek" and len(parts) == 2:
                engine.seek_to(parts[1])
                print(f"seeking to {parts[1]}", flush=True)
            elif cmd == "loop" and len(parts) == 3:
                engine.set_loop(parts[1], parts[2])
                print(f"looping {parts[1]} → {parts[2]}", flush=True)
            elif cmd == "clear-loop":
                engine.clear_loop()
                print("loop cleared", flush=True)
            else:
                print(f"unknown command: {line}", flush=True)
        except Exception as exc:  # pragma: no cover
            print(f"error: {exc}", flush=True)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
