"""Bambu-specific enums extracted for use by both raw models and state machine.

Values are derived from `odin-e2e/captures/run-2026-04-16` — any value
observed in a real Bambu MQTT report is a member; anything else raises.
This is the fail-loud spine of the state mapping: the legacy adapter
falls through to `UNKNOWN` on unknown values, silently degrading the
printer tile to "Idle".
"""
from __future__ import annotations

from enum import Enum


class BambuGcodeState(str, Enum):
    """The `print.gcode_state` enum.

    Observed values across all 4 Bambu printers in run-2026-04-16:

    - `IDLE` — bambu-a1, bambu-p1s (but NOT bambu-h2d or bambu-x1c during
      this capture window, which started in FAILED or FINISH).
    - `PREPARE` — all 4 printers (warm-up / bed leveling / filament load).
    - `RUNNING` — all 4 printers (printing).
    - `PAUSE` — bambu-h2d (paused mid-print for filament tangle, pre-FAILED).
    - `FAILED` — bambu-h2d (print failure at 15:02), bambu-x1c (started in
      FAILED state at capture open).
    - `FINISH` — all 4 printers (print complete, pre-IDLE).

    Firmware versions observed: a1=01.07.00.00, p1s=01.09.00.00,
    x1c=01.11.02.00, h2d=01.03.00.00.

    **Any value not in this enum raises — do not add a fallback.** If a
    new state appears in future firmware, the adapter surfaces DEGRADED
    and the observer logs the unknown string. The fix is to add the enum
    member here, not to swallow the unknown silently.
    """

    IDLE = "IDLE"
    PREPARE = "PREPARE"
    RUNNING = "RUNNING"
    PAUSE = "PAUSE"
    FAILED = "FAILED"
    FINISH = "FINISH"
