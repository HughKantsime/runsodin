"""Pure Ultimaker status parsing with no transport or command imports.

Built only from Ultimaker's public local API documentation (GET /system, GET /printer,
GET /print_job). NOT verified on hardware. Every function fails closed: a body that is not what
the documentation describes yields UNKNOWN, None or 0, never IDLE or PRINTING.
"""

from __future__ import annotations

import math
from typing import Any, Optional

UNKNOWN = "UNKNOWN"
READINGS = frozenset({"IDLE", "PRINTING", "PAUSED", "ERROR"})
_MAX_NAME = 255
_MAX_TEXT = 64
_MAX_EXTRUDERS = 2

# GET /printer "status". maintenance and booting are documented but are neither idle nor
# printing, so they are UNKNOWN (never guessed).
PRINTER_STATUS = {"idle": "IDLE", "printing": "PRINTING", "error": "ERROR"}

# GET /print_job "state". wait_cleanup is a finished job waiting for the plate to be cleared.
JOB_STATE = {
    "none": "IDLE",
    "pre_print": "PRINTING",
    "printing": "PRINTING",
    "post_print": "PRINTING",
    "resuming": "PRINTING",
    "pausing": "PAUSED",
    "paused": "PAUSED",
    "wait_user_action": "PAUSED",
    "wait_cleanup": "IDLE",
}
FINISHED_JOB_STATES = frozenset({"wait_cleanup"})


def map_printer_status(value: object) -> str:
    """'IDLE' | 'PRINTING' | 'ERROR' | 'UNKNOWN' from the GET /printer status string."""
    return PRINTER_STATUS.get(value, UNKNOWN) if isinstance(value, str) else UNKNOWN


def map_job_state(value: object) -> str:
    """'IDLE' | 'PRINTING' | 'PAUSED' | 'UNKNOWN' from the GET /print_job state string."""
    return JOB_STATE.get(value, UNKNOWN) if isinstance(value, str) else UNKNOWN


def _number(value: object):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _seconds(value: object) -> int:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else 0


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit or any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
        return ""
    return cleaned


def parse_system(payload: object) -> dict[str, str]:
    """name, variant, firmware and guid from GET /system (empty string when absent or odd)."""
    body = _mapping(payload)
    return {
        "name": _text(body.get("name"), _MAX_NAME),
        "variant": _text(body.get("variant"), _MAX_TEXT),
        "firmware": _text(body.get("firmware"), _MAX_TEXT),
        "guid": _text(body.get("guid"), _MAX_TEXT),
    }


def parse_printer(payload: object) -> dict[str, Any]:
    """Status, bed temperatures and the extruders (hotend temperatures, active material) from GET /printer.

    The first extruder is the nozzle. At most two extruders are read."""
    body = _mapping(payload)
    bed = _mapping(_mapping(body.get("bed")).get("temperature"))
    heads = body.get("heads")
    head = _mapping(heads[0]) if isinstance(heads, list) and heads else {}
    raw_extruders = head.get("extruders")
    extruders = []
    if isinstance(raw_extruders, list):
        for item in raw_extruders[:_MAX_EXTRUDERS]:
            item = _mapping(item)
            temperature = _mapping(_mapping(item.get("hotend")).get("temperature"))
            material = _mapping(item.get("active_material"))
            extruders.append({
                "temp": _number(temperature.get("current")),
                "target": _number(temperature.get("target")),
                "material_guid": _text(material.get("guid"), _MAX_TEXT),
            })
    first = extruders[0] if extruders else {}
    return {
        "status": _text(body.get("status"), 32),
        "state": map_printer_status(body.get("status")),
        "bed_temp": _number(bed.get("current")), "bed_target": _number(bed.get("target")),
        "nozzle_temp": first.get("temp"), "nozzle_target": first.get("target"),
        "extruders": extruders,
    }


def parse_job(payload: object) -> dict[str, Any]:
    """name, state, uuid, progress (0..1 -> percent, clamped) and times from GET /print_job."""
    body = _mapping(payload)
    raw_state = body.get("state")
    state_raw = raw_state if isinstance(raw_state, str) and len(raw_state) <= _MAX_TEXT else ""
    progress = _number(body.get("progress"))
    percent = None if progress is None else round(min(max(progress, 0.0), 1.0) * 100.0, 2)
    elapsed = _seconds(body.get("time_elapsed"))
    total = _seconds(body.get("time_total"))
    return {
        "filename": _text(body.get("name"), _MAX_NAME),
        "state_raw": state_raw,
        "state": map_job_state(raw_state),
        "uuid": _text(body.get("uuid"), _MAX_TEXT),
        "progress_percent": percent,
        "time_printing": elapsed,
        "time_total": total,
        "time_remaining": max(total - elapsed, 0) if total > 0 else 0,
        "finished": state_raw in FINISHED_JOB_STATES,
    }


def combine_state(printer_state: str, job: Optional[dict]) -> tuple[str, bool]:
    """(ODIN state, finished) from the GET /printer state and the GET /print_job reading.

    ``job`` is parse_job's result, or None when GET /print_job answered 404 (no job).
    The printer saying printing with no job, or idle while a job is printing, contradict each
    other and are UNKNOWN. An active job without a uuid cannot be tracked and is UNKNOWN.
    A finished job (wait_cleanup) is IDLE with finished=True."""
    if printer_state == "ERROR":
        return "ERROR", False
    if printer_state not in ("IDLE", "PRINTING"):
        return UNKNOWN, False
    if job is None:
        return ("IDLE", False) if printer_state == "IDLE" else (UNKNOWN, False)
    state = job["state"]
    if state not in READINGS:
        return UNKNOWN, False
    if job["state_raw"] != "none" and not job["uuid"]:
        return UNKNOWN, False
    if state == "PRINTING":
        return ("PRINTING", False) if printer_state == "PRINTING" else (UNKNOWN, False)
    if state == "PAUSED":
        return "PAUSED", False
    if job["finished"]:
        return "IDLE", True
    return ("IDLE", False) if printer_state == "IDLE" else (UNKNOWN, False)
