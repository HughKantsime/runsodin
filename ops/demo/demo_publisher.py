"""Long-running publisher for demo + capture-pipeline ODIN stacks.

Runs in the `odin-demo-publisher` container of `docker-compose.demo.yml` and
in the `publisher` Deployment of `ops/demo/k8s/`. Connects to an external
mosquitto broker and re-publishes telemetry in an infinite loop so the
demo printers always look "live".

## Modes

**Single-printer (default — App Review env)**

Reuses `publish_fixture` from `live_replay.py`. One process, one printer,
one fixture, looped. Env:

    ODIN_DEMO_BROKER_HOST    default: mosquitto
    ODIN_DEMO_BROKER_PORT    default: 1883
    ODIN_DEMO_PRINTER_SERIAL default: 00M09D4B1600284
    ODIN_DEMO_FIXTURE        default: tests/fixtures/telemetry/bambu-x1c-ams-swap.demo.jsonl
    ODIN_DEMO_SPEED          default: 1.0  (wall-clock pacing)
    ODIN_DEMO_LOOP_GAP_SEC   default: 2.0  (pause between cycles)

**Scenario (multi-printer — capture pipeline / fleet-overview)**

When `ODIN_DEMO_SCENARIO=<name>` is set, this process loads
`demo_scenarios/<name>/scenario.yaml` via `DemoScenario.load`, spawns one
publisher thread per printer (each calling `publish_fixture` against the
shared mosquitto broker), and writes a single combined heartbeat after
each cycle. Each thread loops independently. ODIN-142 (Wake 2) introduced
this mode for the marketing-asset capture pipeline.

    ODIN_DEMO_SCENARIO       set this to opt into multi-printer mode
                             (e.g. happy-farm, ams-swap, dramatic-failure)

In scenario mode `ODIN_DEMO_PRINTER_SERIAL` and `ODIN_DEMO_FIXTURE` are
ignored — those come from the scenario YAML. `ODIN_DEMO_SPEED` and
`ODIN_DEMO_LOOP_GAP_SEC` still apply, uniformly across all threads.

## Health side-channel

Writes its last-publish wall-clock timestamp to `ODIN_DEMO_HEARTBEAT_PATH`
(default `/tmp/odin-demo-publisher.heartbeat`). The health probe reads
that file to assert "publisher fresh within 30s". In scenario mode the
heartbeat fires once after each thread completes a cycle (any-printer
freshness, not all-printer freshness — matches App Review semantics).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.modules.printers.telemetry.live_replay import publish_fixture  # noqa: E402

logger = logging.getLogger("odin.demo.publisher")
HEARTBEAT_PATH = Path(os.environ.get("ODIN_DEMO_HEARTBEAT_PATH", "/tmp/odin-demo-publisher.heartbeat"))


def _heartbeat() -> None:
    HEARTBEAT_PATH.write_text(str(time.time()))


def _publish_loop_single(
    *,
    fixture_path: Path,
    serial: str,
    host: str,
    port: int,
    speed: float,
    gap: float,
    stop: dict,
    label: str = "",
) -> None:
    """One printer's publish loop. Reused by both single-printer and
    multi-printer (scenario) modes."""
    cycle = 0
    while not stop["flag"]:
        cycle += 1
        logger.info("[%s] cycle %d → mqtt://%s:%d serial=%s", label, cycle, host, port, serial)
        try:
            res = publish_fixture(
                fixture_path=fixture_path,
                broker_host=host,
                broker_port=port,
                serial=serial,
                speed=speed,
            )
            logger.info(
                "[%s] cycle %d done: published=%d skipped=%d duration=%.1fs",
                label, cycle, res.messages_published, res.lines_skipped, res.duration_sec,
            )
            _heartbeat()
        except (ConnectionRefusedError, OSError) as exc:
            logger.warning("[%s] broker unreachable, retrying: %s", label, exc)
            time.sleep(5.0)
            continue

        if stop["flag"]:
            break
        time.sleep(gap)

    logger.info("[%s] publisher exited cleanly after %d cycle(s)", label, cycle)


def _run_single(host: str, port: int, speed: float, gap: float, stop: dict) -> int:
    serial = os.environ.get("ODIN_DEMO_PRINTER_SERIAL", "00M09D4B1600284")
    fixture_rel = os.environ.get(
        "ODIN_DEMO_FIXTURE",
        "tests/fixtures/telemetry/bambu-x1c-ams-swap.demo.jsonl",
    )
    fixture_path = (REPO_ROOT / fixture_rel).resolve()
    if not fixture_path.exists():
        logger.error("fixture missing: %s", fixture_path)
        return 2

    _publish_loop_single(
        fixture_path=fixture_path,
        serial=serial,
        host=host,
        port=port,
        speed=speed,
        gap=gap,
        stop=stop,
        label=serial,
    )
    return 0


def _run_scenario(name: str, host: str, port: int, speed: float, gap: float, stop: dict) -> int:
    """Multi-printer mode: spawn one thread per scenario printer."""
    # Import lazily — single-printer mode (the App Review default) doesn't
    # need DemoScenario at all, so a partial install / older image without
    # the demo module still works for the App Review path.
    from backend.modules.printers.telemetry.demo import DemoScenario, FIXTURES_DIR, SCENARIOS_DIR

    try:
        scenario = DemoScenario.load(SCENARIOS_DIR, name)
    except FileNotFoundError as exc:
        logger.error("scenario %r not found: %s", name, exc)
        return 2

    if not scenario.printers:
        logger.error("scenario %r has no printers", name)
        return 2

    logger.info(
        "scenario mode: name=%s printers=%d broker=mqtt://%s:%d",
        name, len(scenario.printers), host, port,
    )

    threads: list[threading.Thread] = []
    for printer in scenario.printers:
        fixture_path = (FIXTURES_DIR / printer.fixture).resolve()
        if not fixture_path.exists():
            logger.error("scenario printer %s fixture missing: %s", printer.id, fixture_path)
            return 2
        t = threading.Thread(
            target=_publish_loop_single,
            kwargs={
                "fixture_path": fixture_path,
                "serial": printer.serial,
                "host": host,
                "port": port,
                "speed": speed,
                "gap": gap,
                "stop": stop,
                "label": printer.id,
            },
            name=f"publisher-{printer.id}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Block until stop flag is set, then join with a short timeout. Threads
    # check stop["flag"] between cycles; they exit cleanly within `gap`.
    while not stop["flag"]:
        time.sleep(0.5)
    for t in threads:
        t.join(timeout=10.0)

    logger.info("scenario publisher exited cleanly")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    host = os.environ.get("ODIN_DEMO_BROKER_HOST", "mosquitto")
    port = int(os.environ.get("ODIN_DEMO_BROKER_PORT", "1883"))
    speed = float(os.environ.get("ODIN_DEMO_SPEED", "1.0"))
    gap = float(os.environ.get("ODIN_DEMO_LOOP_GAP_SEC", "2.0"))
    scenario_name = os.environ.get("ODIN_DEMO_SCENARIO", "").strip()

    stop = {"flag": False}

    def _on_signal(signum, _frame):
        logger.info("signal %s received; stopping after current cycle", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    if scenario_name:
        return _run_scenario(scenario_name, host, port, speed, gap, stop)
    return _run_single(host, port, speed, gap, stop)


if __name__ == "__main__":
    sys.exit(main())
