"""Long-running publisher for the demo.subsystem.app App Review env.

Runs in the `odin-demo-publisher` container of `docker-compose.demo.yml`.
Connects to an external mosquitto broker (the `mosquitto` compose service)
and re-publishes the scrubbed AMS-swap fixture in an infinite loop so the
demo printer always looks "live" to App Review reviewers.

Reuses `publish_fixture` from `live_replay.py` — no new telemetry code.
The wiring is: connect → publish full fixture → sleep briefly → repeat.

Env:
    ODIN_DEMO_BROKER_HOST    default: mosquitto
    ODIN_DEMO_BROKER_PORT    default: 1883
    ODIN_DEMO_PRINTER_SERIAL default: 00M09D4B1600284 (matches scenario)
    ODIN_DEMO_FIXTURE        default: tests/fixtures/telemetry/bambu-x1c-ams-swap.demo.jsonl
    ODIN_DEMO_SPEED          default: 1.0  (wall-clock pacing)
    ODIN_DEMO_LOOP_GAP_SEC   default: 2.0  (pause between cycles)

Health side-channel: writes its last-publish timestamp to
`/tmp/odin-demo-publisher.heartbeat` on every cycle. The health probe
in this directory reads that file to assert "publisher fresh within 30s".
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.modules.printers.telemetry.live_replay import publish_fixture  # noqa: E402

logger = logging.getLogger("odin.demo.publisher")
HEARTBEAT_PATH = Path(os.environ.get("ODIN_DEMO_HEARTBEAT_PATH", "/tmp/odin-demo-publisher.heartbeat"))


def _heartbeat() -> None:
    HEARTBEAT_PATH.write_text(str(time.time()))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    host = os.environ.get("ODIN_DEMO_BROKER_HOST", "mosquitto")
    port = int(os.environ.get("ODIN_DEMO_BROKER_PORT", "1883"))
    serial = os.environ.get("ODIN_DEMO_PRINTER_SERIAL", "00M09D4B1600284")
    fixture_rel = os.environ.get(
        "ODIN_DEMO_FIXTURE",
        "tests/fixtures/telemetry/bambu-x1c-ams-swap.demo.jsonl",
    )
    speed = float(os.environ.get("ODIN_DEMO_SPEED", "1.0"))
    gap = float(os.environ.get("ODIN_DEMO_LOOP_GAP_SEC", "2.0"))

    fixture_path = (REPO_ROOT / fixture_rel).resolve()
    if not fixture_path.exists():
        logger.error("fixture missing: %s", fixture_path)
        return 2

    stop = {"flag": False}

    def _on_signal(signum, _frame):
        logger.info("signal %s received; stopping after current cycle", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    cycle = 0
    while not stop["flag"]:
        cycle += 1
        logger.info("cycle %d → broker mqtt://%s:%d serial=%s", cycle, host, port, serial)
        try:
            res = publish_fixture(
                fixture_path=fixture_path,
                broker_host=host,
                broker_port=port,
                serial=serial,
                speed=speed,
            )
            logger.info(
                "cycle %d done: published=%d skipped=%d duration=%.1fs",
                cycle, res.messages_published, res.lines_skipped, res.duration_sec,
            )
            _heartbeat()
        except (ConnectionRefusedError, OSError) as exc:
            logger.warning("broker unreachable, retrying: %s", exc)
            time.sleep(5.0)
            continue

        if stop["flag"]:
            break
        time.sleep(gap)

    logger.info("publisher exited cleanly after %d cycle(s)", cycle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
