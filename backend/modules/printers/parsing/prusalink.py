"""Pure PrusaLink status parsing with no transport or command imports."""

from __future__ import annotations

from typing import Any


STATE_MAP = {
    "IDLE": "IDLE", "READY": "IDLE", "OPERATIONAL": "IDLE",
    "PRINTING": "PRINTING", "PAUSED": "PAUSED", "ATTENTION": "ATTENTION",
    "BUSY": "BUSY", "ERROR": "ERROR", "FINISHED": "FINISHED", "STOPPED": "STOPPED",
    "UNKNOWN": "UNKNOWN",
}


def map_state(value: object) -> str:
    return STATE_MAP.get(str(value).upper(), "UNKNOWN")


def parse_v1_status(data: dict[str, Any]) -> dict[str, Any]:
    printer = data.get("printer") if isinstance(data.get("printer"), dict) else {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    state = map_state(printer.get("state", "UNKNOWN"))
    parsed = {
        "state": state, "internal_state": str(printer.get("state", "UNKNOWN")).upper(),
        "bed_temp": printer.get("temp_bed", 0.0), "bed_target": printer.get("target_bed", 0.0),
        "nozzle_temp": printer.get("temp_nozzle", 0.0), "nozzle_target": printer.get("target_nozzle", 0.0),
        "axis_z": printer.get("axis_z", 0.0), "flow": printer.get("flow", 100),
        "speed": printer.get("speed", 100), "fan_hotend": printer.get("fan_hotend", 0),
        "fan_print": printer.get("fan_print", 0), "job_id": job.get("id"),
        "progress_percent": job.get("progress", 0.0), "time_printing": job.get("time_printing", 0),
        "time_remaining": job.get("time_remaining", 0), "raw_data": data,
    }
    file_info = job.get("file") if isinstance(job.get("file"), dict) else {}
    parsed["filename"] = file_info.get("display", "") or file_info.get("name", "")
    return parsed


def parse_legacy_status(
    printer_data: dict[str, Any] | None, job_data: dict[str, Any] | None,
) -> dict[str, Any]:
    if printer_data is None:
        return {"state": "DISCONNECTED", "internal_state": "OFFLINE", "raw_data": {}}
    temp = printer_data.get("temperature") if isinstance(printer_data.get("temperature"), dict) else {}
    tool0 = temp.get("tool0") if isinstance(temp.get("tool0"), dict) else {}
    bed = temp.get("bed") if isinstance(temp.get("bed"), dict) else {}
    flags = ((printer_data.get("state") or {}).get("flags") or {}) if isinstance(printer_data.get("state"), dict) else {}
    state = "DISCONNECTED"
    internal = "OFFLINE"
    if flags.get("printing"):
        state, internal = "PRINTING", "PRINTING"
    elif flags.get("paused") or flags.get("pausing"):
        state, internal = "PAUSED", "PAUSED"
    elif flags.get("error") or flags.get("closedOnError"):
        state, internal = "ERROR", "ERROR"
    elif flags.get("ready") or flags.get("operational"):
        state, internal = "IDLE", "IDLE"
    telemetry = printer_data.get("telemetry") if isinstance(printer_data.get("telemetry"), dict) else {}
    job_root = job_data if isinstance(job_data, dict) else {}
    job = job_root.get("job") if isinstance(job_root.get("job"), dict) else {}
    progress = job_root.get("progress") if isinstance(job_root.get("progress"), dict) else {}
    file_info = job.get("file") if isinstance(job.get("file"), dict) else {}
    return {
        "state": state, "internal_state": internal,
        "nozzle_temp": tool0.get("actual", 0.0), "nozzle_target": tool0.get("target", 0.0),
        "bed_temp": bed.get("actual", 0.0), "bed_target": bed.get("target", 0.0),
        "axis_z": telemetry.get("z-height", 0.0),
        "progress_percent": progress.get("completion", 0.0) or 0.0,
        "time_printing": progress.get("printTime", 0) or 0,
        "time_remaining": progress.get("printTimeLeft", 0) or 0,
        "filename": file_info.get("display", "") or file_info.get("name", ""),
        "job_id": job.get("id"), "raw_data": printer_data,
    }
