"""Pure Elegoo SDCP status parsing with no transport or command imports."""

from __future__ import annotations

from typing import Any


def parse_status(data: dict[str, Any]) -> dict[str, Any]:
    status = data.get("Status")
    if not isinstance(status, dict) or not status:
        nested = data.get("Data")
        status = nested.get("Status") if isinstance(nested, dict) else {}
    if not isinstance(status, dict) or not status:
        return {}
    current = status.get("CurrentStatus", [0])
    current_status = current[0] if isinstance(current, list) and current else current
    print_info = status.get("PrintInfo") if isinstance(status.get("PrintInfo"), dict) else {}
    print_status = print_info.get("Status", 0)
    if print_status == 8 or current_status == 1:
        internal = "PRINTING"
    elif print_status in {5, 6}:
        internal = "PAUSED"
    elif print_status == 16:
        internal = "FINISHED"
    elif print_status == 7:
        internal = "STOPPING"
    elif current_status == 8:
        internal = "HEATING"
    elif current_status == 9:
        internal = "HOMING"
    elif current_status == 6:
        internal = "LEVELING"
    elif print_status == 0 and current_status == 0:
        internal = "IDLE"
    else:
        internal = "UNKNOWN"
    fans = status.get("CurrentFanSpeed") if isinstance(status.get("CurrentFanSpeed"), dict) else {}
    current_ticks = print_info.get("CurrentTicks", 0)
    total_ticks = print_info.get("TotalTicks", 0)
    return {
        "connected": True, "internal_state": internal,
        "mainboard_id": data.get("MainboardID", ""),
        "bed_temp": status.get("TempOfHotbed", 0.0), "nozzle_temp": status.get("TempOfNozzle", 0.0),
        "box_temp": status.get("TempOfBox", 0.0), "bed_target": status.get("TempTargetHotbed", 0.0),
        "nozzle_target": status.get("TempTargetNozzle", 0.0), "box_target": status.get("TempTargetBox", 0.0),
        "model_fan": fans.get("ModelFan", 0), "auxiliary_fan": fans.get("AuxiliaryFan", 0),
        "box_fan": fans.get("BoxFan", 0), "current_status": current_status, "print_status": print_status,
        "current_layer": print_info.get("CurrentLayer", 0), "total_layers": print_info.get("TotalLayer", 0),
        "current_ticks": current_ticks, "total_ticks": total_ticks,
        "filename": print_info.get("Filename", ""), "progress_percent": print_info.get("Progress", 0.0),
        "time_remaining": max(0, total_ticks - current_ticks) if total_ticks and current_ticks else 0,
        "raw_data": data,
    }


def parse_response(data: Any) -> dict[str, Any] | None:
    """A ``sdcp/response/`` frame -> {"cmd", "request_id", "ack"}, or None unless every field is well formed.

    Shape: SDCP public documentation, unverified on hardware:
    {"Data": {"Cmd": int, "Data": {"Ack": int}, "RequestID": str, ...}, "Topic": "sdcp/response/<id>"}.
    A malformed reply is None, so the caller sees "no answer", never a guessed success."""
    if not isinstance(data, dict):
        return None
    body = data.get("Data")
    if not isinstance(body, dict):
        return None
    cmd, request_id, inner = body.get("Cmd"), body.get("RequestID"), body.get("Data")
    if type(cmd) is not int or not isinstance(request_id, str) or not request_id or not isinstance(inner, dict):
        return None
    ack = inner.get("Ack")
    if type(ack) is not int:
        return None
    return {"cmd": cmd, "request_id": request_id, "ack": ack}


def parse_upload_reply(body: Any) -> bool:
    """True only for the documented upload success body {"code": "000000", "success": true, ...}.

    Source: SDCP public documentation (POST /uploadFile/upload), unverified on hardware."""
    return isinstance(body, dict) and body.get("success") is True and body.get("code") == "000000"
