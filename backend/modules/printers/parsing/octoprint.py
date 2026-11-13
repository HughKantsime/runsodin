"""Pure OctoPrint status parsing with no transport or command imports.

Built from OctoPrint's public REST API documentation only (GET /api/printer, GET /api/job).
Every function fails closed: a body that is not what the documentation describes yields
UNKNOWN or None, never IDLE or PRINTING.
"""

from __future__ import annotations

import math
from typing import Any

FLAG_NAMES = ("operational", "paused", "printing", "cancelling", "pausing", "error", "ready", "closedOrError")
READINGS = frozenset({"IDLE", "PRINTING", "PAUSED", "ERROR"})
UNKNOWN = "UNKNOWN"
_MAX_NAME = 255


def map_flags(flags: object) -> str:
    """'IDLE' | 'PRINTING' | 'PAUSED' | 'ERROR' | 'UNKNOWN' from GET /api/printer state.flags.

    printing, pausing and cancelling are PRINTING (a cancel stays PRINTING until the printer
    reports idle); paused is PAUSED; error or closedOrError is ERROR; operational and ready with
    nothing else set is IDLE. A flag that is not a real boolean, or a combination the
    documentation does not describe (for example printing together with error), is UNKNOWN.
    """
    if not isinstance(flags, dict) or not flags:
        return UNKNOWN
    values = {}
    for name in FLAG_NAMES:
        value = flags.get(name, False)
        if type(value) is not bool:
            return UNKNOWN
        values[name] = value
    busy = values["printing"] or values["pausing"] or values["cancelling"]
    halted = values["error"] or values["closedOrError"]
    paused = values["paused"]
    if halted:
        return UNKNOWN if (busy or paused) else "ERROR"
    if paused:
        return UNKNOWN if busy else "PAUSED"
    if busy:
        return "PRINTING"
    if values["operational"] and values["ready"]:
        return "IDLE"
    return UNKNOWN


def printer_state(payload: object) -> str:
    """State from a whole GET /api/printer body."""
    if not isinstance(payload, dict):
        return UNKNOWN
    state = payload.get("state")
    return map_flags(state.get("flags") if isinstance(state, dict) else None)


def _number(value: object):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _seconds(value: object) -> int:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else 0


def _percent(value: object):
    number = _number(value)
    return number if number is not None and 0.0 <= number <= 100.0 else None


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def parse_printer(payload: object) -> dict[str, Any]:
    """State plus temperatures (tool0 and bed, actual and target) from GET /api/printer."""
    body = _mapping(payload)
    temperature = _mapping(body.get("temperature"))
    tool0 = _mapping(temperature.get("tool0"))
    bed = _mapping(temperature.get("bed"))
    return {
        "state": printer_state(payload),
        "nozzle_temp": _number(tool0.get("actual")), "nozzle_target": _number(tool0.get("target")),
        "bed_temp": _number(bed.get("actual")), "bed_target": _number(bed.get("target")),
    }


def parse_job(payload: object) -> dict[str, Any]:
    """job.file.name, progress.completion/printTime/printTimeLeft and state from GET /api/job."""
    body = _mapping(payload)
    file_info = _mapping(_mapping(body.get("job")).get("file"))
    progress = _mapping(body.get("progress"))
    name = file_info.get("name")
    filename = ""
    if isinstance(name, str):
        cleaned = name.strip()
        if cleaned and len(cleaned) <= _MAX_NAME and not any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
            filename = cleaned
    state = body.get("state")
    return {
        "filename": filename,
        "progress_percent": _percent(progress.get("completion")),
        "time_printing": _seconds(progress.get("printTime")),
        "time_remaining": _seconds(progress.get("printTimeLeft")),
        "job_state": state[:64] if isinstance(state, str) else "",
    }
