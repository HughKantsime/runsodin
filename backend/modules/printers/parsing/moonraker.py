"""Pure Moonraker object-status parsing with no transport or command imports."""

from __future__ import annotations

from typing import Any, Iterable


INTERNAL_STATE = {
    "ready": "IDLE", "printing": "RUNNING", "paused": "PAUSE",
    "error": "FAILED", "standby": "IDLE", "disconnected": "OFFLINE",
    "unknown": "UNKNOWN",
}


def parse_status(
    payload: dict[str, Any], *, temperature_sensors: Iterable[str] = (),
    environment_sensors: Iterable[str] = (), filament_sensors: Iterable[str] = (),
) -> dict[str, Any]:
    bed = payload.get("heater_bed") if isinstance(payload.get("heater_bed"), dict) else {}
    extruder = payload.get("extruder") if isinstance(payload.get("extruder"), dict) else {}
    stats = payload.get("print_stats") if isinstance(payload.get("print_stats"), dict) else {}
    layer = stats.get("info") if isinstance(stats.get("info"), dict) else {}
    virtual_sd = payload.get("virtual_sdcard") if isinstance(payload.get("virtual_sdcard"), dict) else {}
    observed_state = str(stats.get("state", "unknown")).lower()
    state = "ready" if observed_state in {"standby", "complete", "cancelled"} else observed_state
    if state not in INTERNAL_STATE:
        state = "unknown"
    fan = payload.get("fan") if isinstance(payload.get("fan"), dict) else {}
    motion = payload.get("gcode_move") if isinstance(payload.get("gcode_move"), dict) else {}
    webhooks = payload.get("webhooks") if isinstance(payload.get("webhooks"), dict) else {}
    result: dict[str, Any] = {
        "state": state, "internal_state": INTERNAL_STATE[state], "raw_data": payload,
        "raw_print_state": observed_state if observed_state in {
            "printing", "paused", "complete", "cancelled", "error", "standby", "ready"
        } else "unknown",
        "bed_temp": bed.get("temperature", 0.0), "bed_target": bed.get("target", 0.0),
        "nozzle_temp": extruder.get("temperature", 0.0), "nozzle_target": extruder.get("target", 0.0),
        "filename": stats.get("filename", ""), "print_duration": stats.get("print_duration", 0.0),
        "filament_used_mm": stats.get("filament_used", 0.0),
        "current_layer": layer.get("current_layer", 0), "total_layers": layer.get("total_layer", 0),
        "progress_percent": round(virtual_sd.get("progress", 0.0) * 100, 1) if virtual_sd else 0.0,
        "fan_speed": round(fan.get("speed", 0.0) * 100) if fan else 0,
        "speed_factor": motion.get("speed_factor", 1.0), "extrude_factor": motion.get("extrude_factor", 1.0),
        "error_message": webhooks.get("state_message", ""), "environment_sensors": {},
        "chamber_temp": None, "filament_detected": None,
    }
    environment_names = set(environment_sensors)
    for sensor_name in temperature_sensors:
        sensor = payload.get(sensor_name)
        if not isinstance(sensor, dict) or sensor.get("temperature") is None:
            continue
        short_name = sensor_name.split(" ", 1)[1] if " " in sensor_name else sensor_name
        temperature = round(sensor["temperature"], 1)
        if result["chamber_temp"] is None and any(marker in short_name.lower() for marker in ("chamber", "enclosure")):
            result["chamber_temp"] = temperature
        if sensor_name in environment_names:
            result["environment_sensors"][short_name] = temperature
    for sensor_name in filament_sensors:
        sensor = payload.get(sensor_name)
        if isinstance(sensor, dict) and "filament_detected" in sensor:
            result["filament_detected"] = sensor["filament_detected"]
            break
    return result
