"""Parser-only bridge from bounded wire objects to normalized in-memory samples."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from modules.printers.parsing.elegoo import parse_status as parse_elegoo_status
from modules.printers.parsing.moonraker import parse_status as parse_moonraker_status
from modules.printers.parsing.prusalink import parse_v1_status
from modules.printers.printer_models import normalize_model_name
from modules.printers.telemetry.bambu.raw import BambuReport
from modules.printers.telemetry.events import BambuReportEvent
from modules.printers.telemetry.state import PrinterStatus
from modules.printers.telemetry.transition import transition


class ParseError(ValueError):
    pass


SAFE_VERSION = re.compile(r"^[A-Za-z0-9._+ -]{1,64}$")


@dataclass(frozen=True)
class ParsedSample:
    state: str
    filename: str
    job_id: int | str | None = None
    capabilities: frozenset[str] = frozenset()
    ams_slots: int | None = None
    model_family: str | None = None


def safe_version(value: object) -> str:
    text = str(value or "unknown")
    return text if SAFE_VERSION.fullmatch(text) else "unknown"


def _number(value: object, *, minimum: float, maximum: float, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ParseError(f"{field} is not numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ParseError(f"{field} is outside bounds")
    return result


def _validate_common(parsed: dict[str, Any]) -> None:
    for name in ("bed_temp", "bed_target", "nozzle_temp", "nozzle_target", "chamber_temp"):
        value = parsed.get(name)
        if value is not None:
            _number(value, minimum=-50, maximum=600, field=name)
    if parsed.get("progress_percent") is not None:
        _number(parsed["progress_percent"], minimum=0, maximum=100, field="progress_percent")
    for name in ("current_layer", "total_layers"):
        value = parsed.get(name)
        if value is not None:
            _number(value, minimum=0, maximum=10_000_000, field=name)


def parse_bambu_sample(
    payload: bytes | str | dict[str, Any], *, previous: PrinterStatus, timestamp: float,
) -> tuple[ParsedSample, PrinterStatus]:
    try:
        report = BambuReport.model_validate_json(payload) if isinstance(payload, (bytes, str)) else BambuReport.model_validate(payload)
    except Exception as exc:
        raise ParseError("Bambu envelope validation failed") from exc
    if report.print is None:
        raise ParseError("Bambu report does not contain status")
    try:
        current, _events = transition(
            previous, BambuReportEvent(printer_id="target-1", ts=timestamp, section=report.print)
        )
    except Exception as exc:
        raise ParseError("Bambu state mapping failed") from exc
    parsed = {
        "bed_temp": current.bed_temp, "bed_target": current.bed_target,
        "nozzle_temp": current.nozzle_temp, "nozzle_target": current.nozzle_target,
        "chamber_temp": current.chamber_temp, "progress_percent": current.progress_percent,
        "current_layer": current.layer_current, "total_layers": current.layer_total,
    }
    _validate_common(parsed)
    ams_slots = sum(len(unit.tray) for unit in report.print.ams.ams) if report.print.ams else None
    capabilities = {"status"}
    if report.print.ams is not None:
        capabilities.add("ams_read")
    return ParsedSample(
        state=current.state.value, filename=current.current_file or "", job_id=current.job_id,
        capabilities=frozenset(capabilities), ams_slots=ams_slots,
        model_family=normalize_model_name("bambu", report.print.printer_type),
    ), current


def parse_moonraker_sample(payload: dict[str, Any]) -> ParsedSample:
    result = payload.get("result")
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, dict):
        raise ParseError("Moonraker object status is missing")
    parsed = parse_moonraker_status(status)
    _validate_common(parsed)
    state = str(parsed["state"])
    if state == "ready":
        state = "idle"
    return ParsedSample(
        state=state, filename=str(parsed.get("filename") or ""),
        capabilities=frozenset({"status"}),
    )


def parse_prusalink_sample(status_payload: dict[str, Any], job_payload: dict[str, Any]) -> ParsedSample:
    printer = status_payload.get("printer")
    job = job_payload.get("job")
    if not isinstance(printer, dict) or not isinstance(job, dict):
        raise ParseError("PrusaLink status or job shape is missing")
    parsed = parse_v1_status({"printer": printer, "job": job})
    _validate_common(parsed)
    job_id = parsed.get("job_id")
    if job_id is not None and (not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 0):
        raise ParseError("PrusaLink job ID is invalid")
    return ParsedSample(
        state=str(parsed["state"]).lower(), filename=str(parsed.get("filename") or ""),
        job_id=job_id, capabilities=frozenset({"status"}),
    )


def parse_elegoo_sample(payload: dict[str, Any]) -> ParsedSample:
    topic = payload.get("Topic")
    if not isinstance(topic, str) or not topic.startswith(("sdcp/status/", "sdcp/notice/")):
        raise ParseError("Elegoo frame is not unsolicited telemetry")
    parsed = parse_elegoo_status(payload)
    if not parsed:
        raise ParseError("Elegoo status shape is missing")
    _validate_common(parsed)
    return ParsedSample(
        state=str(parsed["internal_state"]).lower(), filename=str(parsed.get("filename") or ""),
        capabilities=frozenset({"status"}),
    )
