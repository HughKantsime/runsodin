"""Short-lived session helpers for Bambu printers.

Most of ODIN's Bambu callsites use a connect → do-a-thing → disconnect
pattern (see `MIGRATION.md` — routes_status live-status, routes_ams
poll, routes_crud test connection, routes_setup test connection,
detection_thread pause, etc.). This module provides thin helpers that
wrap V2 adapters in that pattern so migration stays mechanical.

Two helpers:

- `read_status_once(config, timeout)` — starts a V2 telemetry adapter,
  waits for the first BambuReportEvent, returns the derived
  `BambuV2StatusView` + the raw `BambuPrintSection`.
- `run_command(config, method_name, *args, **kwargs)` — starts a
  `BambuCommandAdapter`, invokes `method_name` on it, stops.

Both return False / None on connect failure (matching legacy
semantics); callers treat that as "printer unreachable".
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from backend.modules.printers.telemetry.bambu.adapter import (
    BambuAdapterConfig,
    BambuTelemetryAdapter,
)
from backend.modules.printers.telemetry.bambu.commands import BambuCommandAdapter
from backend.modules.printers.telemetry.bambu.raw import BambuPrintSection
from backend.modules.printers.telemetry.bambu.status_view import BambuV2StatusView
from backend.modules.printers.telemetry.events import (
    BambuReportEvent,
    ConnectionEvent,
)

logger = logging.getLogger(__name__)


@dataclass
class StatusReadResult:
    """Returned by `read_status_once`."""

    success: bool
    view: Optional[BambuV2StatusView] = None
    section: Optional[BambuPrintSection] = None
    error: Optional[str] = None


def read_status_once(
    config: BambuAdapterConfig,
    timeout: float = 10.0,
) -> StatusReadResult:
    """Open a V2 telemetry adapter, wait for first status, close.

    `timeout` seconds to receive both CONNACK + first BambuReportEvent.
    If CONNACK arrives but no status pushes in, returns partial
    (success=True, section=None).
    """
    first_section: dict[str, Optional[BambuPrintSection]] = {"value": None}
    connected = threading.Event()
    had_report = threading.Event()

    def emitter(item) -> None:
        if isinstance(item, ConnectionEvent) and item.kind == "connected":
            connected.set()
        elif isinstance(item, BambuReportEvent):
            first_section["value"] = item.section
            had_report.set()

    adapter = BambuTelemetryAdapter(config, emitter=emitter)
    try:
        adapter.start()
    except Exception as exc:
        return StatusReadResult(success=False, error=str(exc))

    try:
        if not connected.wait(timeout=timeout):
            return StatusReadResult(success=False, error="timeout waiting for CONNACK")
        # Give some slack for first report; partial success if none arrives.
        remaining = max(0.1, timeout - 1.0)
        had_report.wait(timeout=remaining)
        status = adapter.status()
        return StatusReadResult(
            success=True,
            view=BambuV2StatusView(status),
            section=first_section["value"],
        )
    finally:
        adapter.stop()


def run_command(
    config: BambuAdapterConfig,
    method_name: str,
    *args: Any,
    connect_timeout: float = 5.0,
    **kwargs: Any,
) -> bool:
    """Open a command adapter, call `method_name(*args, **kwargs)`, close.

    Returns False on connect failure, method-not-found, or publish
    failure. Matches legacy's boolean return contract.
    """
    adapter = BambuCommandAdapter(config)
    try:
        adapter.start()
    except Exception:
        logger.exception("command adapter start failed: printer=%s", config.printer_id)
        return False

    try:
        # Wait briefly for CONNACK
        deadline = time.time() + connect_timeout
        while time.time() < deadline:
            if adapter.is_connected():
                break
            time.sleep(0.05)
        if not adapter.is_connected():
            logger.warning("command adapter not connected within timeout: printer=%s", config.printer_id)
            return False

        method = getattr(adapter, method_name, None)
        if method is None or not callable(method):
            logger.warning("unknown command method: %s", method_name)
            return False
        try:
            return bool(method(*args, **kwargs))
        except Exception:
            logger.exception("command %s raised", method_name)
            return False
    finally:
        adapter.stop()
