"""Read-only Filament Ledger display enrichment for configured Odin printers."""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from typing import Any

import httpx

from core.config import settings
from core.itar import check_url_allowed, pin_for_request

log = logging.getLogger("odin.filament_ledger_display")

_CACHE_SECONDS = 15.0
_REQUEST_TIMEOUT = 2.0
_cache_lock = threading.Lock()
_cached_url: str | None = None
_cached_until = 0.0
_cached_printers: dict[str, dict[str, Any]] | None = None
_cached_available = False


def _configured_printers() -> dict[int, str]:
    """Return explicit, valid Odin-id to Ledger-key bindings only."""
    configured = getattr(settings, "filament_ledger_printers", None) or {}
    result: dict[int, str] = {}
    if not isinstance(configured, dict):
        return result
    for raw_id, raw_key in configured.items():
        try:
            printer_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if printer_id > 0 and isinstance(raw_key, str) and raw_key.strip():
            result[printer_id] = raw_key.strip()
    return result


def ensure_local_filaments(printer_id: int) -> None:
    """Reject local filament mutations for a printer owned by the Ledger view."""
    from fastapi import HTTPException

    if int(printer_id) in _configured_printers():
        raise HTTPException(
            status_code=409,
            detail="Filament data for this printer is managed by Filament Ledger",
        )


def _valid_payload(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("printers"), list):
        raise ValueError("invalid display payload")
    result: dict[str, dict[str, Any]] = {}
    for printer in payload["printers"]:
        if not isinstance(printer, dict) or not isinstance(printer.get("key"), str):
            raise ValueError("invalid display printer")
        tools = printer.get("tools")
        if not isinstance(tools, list):
            raise ValueError("invalid display tools")
        key = printer["key"]
        if key in result:
            raise ValueError("duplicate display printer key")
        normalized_tools = []
        seen_indexes = set()
        for tool in tools:
            if not isinstance(tool, dict) or type(tool.get("tool_index")) is not int or tool["tool_index"] < 0:
                raise ValueError("invalid display tool")
            if tool["tool_index"] in seen_indexes:
                raise ValueError("duplicate display tool index")
            seen_indexes.add(tool["tool_index"])
            status = tool.get("spool_status")
            spool_id = tool.get("spool_id")
            spool = tool.get("spool")
            if status not in {"mapped", "unmapped", "unavailable"}:
                raise ValueError("invalid spool mapping status")
            if status == "mapped":
                if not isinstance(spool_id, (int, str)) or isinstance(spool_id, bool) or spool is None:
                    raise ValueError("mapped spool missing identity or details")
                if not isinstance(spool, dict):
                    raise ValueError("invalid mapped spool")
                if spool.get("id") != spool_id:
                    raise ValueError("mapped spool identity mismatch")
                for field in ("material", "name", "brand", "color_hex"):
                    if spool.get(field) is not None and not isinstance(spool[field], str):
                        raise ValueError("invalid mapped spool field")
                color = spool.get("color_hex")
                if color is not None and not re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
                    raise ValueError("invalid mapped spool color")
                for field in ("remaining_weight_g", "initial_weight_g"):
                    value = spool.get(field)
                    if value is not None and (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(value)
                    ):
                        raise ValueError("invalid mapped spool weight")
            elif spool is not None:
                raise ValueError("unmapped spool must be null")
            normalized_tools.append(tool)
        result[key] = {"key": key, "tools": normalized_tools}
    return result


def _fetch_printers() -> tuple[bool, dict[str, dict[str, Any]]]:
    global _cached_url, _cached_until, _cached_printers, _cached_available
    url = (getattr(settings, "filament_ledger_url", None) or "").strip().rstrip("/")
    if not url:
        return False, {}

    now = time.monotonic()
    with _cache_lock:
        now = time.monotonic()
        if _cached_url == url and now < _cached_until:
            return _cached_available, _cached_printers or {}

        # This integration is deliberately restricted to an explicitly safe URL.
        allowed, _reason = check_url_allowed(url)
        if not allowed:
            _cached_url, _cached_until = url, now + _CACHE_SECONDS
            _cached_printers, _cached_available = None, False
            return False, {}

        endpoint = f"{url}/api/filament-display"
        try:
            with pin_for_request(endpoint):
                with httpx.Client(
                    timeout=_REQUEST_TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = client.get(endpoint)
            if response.status_code != 200:
                raise ValueError("unexpected display status")
            printers = _valid_payload(response.json())
        except Exception as exc:
            # Never surface response bodies, URLs, or exception details to clients.
            log.info("Filament Ledger display unavailable (%s)", type(exc).__name__)
            _cached_url, _cached_until = url, now + _CACHE_SECONDS
            _cached_printers, _cached_available = None, False
            return False, {}

        _cached_url, _cached_until = url, now + _CACHE_SECONDS
        _cached_printers, _cached_available = printers, True
        return True, printers


def _slot_from_tool(tool: dict[str, Any]) -> dict[str, Any]:
    from core.base import FilamentType

    status = tool["spool_status"]
    spool = tool.get("spool") if status == "mapped" else None
    raw_material = spool.get("material") if spool else None
    category = FilamentType.from_bambu_code(raw_material) if raw_material else FilamentType.UNKNOWN
    color_hex = spool.get("color_hex") if spool else None
    if isinstance(color_hex, str):
        color_hex = color_hex.strip().lstrip("#").upper() or None
    else:
        color_hex = None
    initial = spool.get("initial_weight_g") if spool else None
    remaining = spool.get("remaining_weight_g") if spool else None
    percentage = None
    if isinstance(initial, (int, float)) and not isinstance(initial, bool) and math.isfinite(initial) and initial > 0 \
            and isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and math.isfinite(remaining):
        percentage = max(0.0, min(100.0, float(remaining) / float(initial) * 100.0))
    brand = spool.get("brand") if spool else None
    name = spool.get("name") if spool else None
    display_name = tool.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = f"Tool {tool['tool_index'] + 1}"
    color = " ".join(str(value).strip() for value in (brand, name) if value)
    return {
        "slot_number": tool["tool_index"] + 1,
        "filament_type": category,
        "color": color or None,
        "color_hex": color_hex,
        # This field is external identity only; local spool assignment is never reused.
        "spoolman_spool_id": None,
        "assigned_spool_id": None,
        "spool_confirmed": None,
        "id": -(tool["tool_index"] + 1),
        "printer_id": None,
        "loaded_at": None,
        "remaining": percentage,
        "material_type": raw_material,
        "display_name": display_name or None,
        "mapping_status": status,
        "external_spool_id": spool.get("id") if spool else None,
    }


def _to_printer_dto(row: Any, ledger_printer: dict[str, Any] | None, available: bool):
    from modules.printers.schemas import PrinterResponse

    printer_id = int(getattr(row, "id"))
    if not available or ledger_printer is None:
        # An outage or absent configured printer must not reveal stale local slots.
        base = PrinterResponse.model_validate(row).model_dump()
        base.update({
            "filament_slots": [],
            "filament_source": "filament-ledger",
            "filament_source_status": "unavailable",
        })
        return PrinterResponse.model_validate(base)

    if not ledger_printer["tools"]:
        base = PrinterResponse.model_validate(row).model_dump()
        base.update({
            "filament_slots": [],
            "filament_source": "filament-ledger",
            "filament_source_status": "unavailable",
        })
        return PrinterResponse.model_validate(base)
    slots = [_slot_from_tool(tool) for tool in ledger_printer["tools"]]
    partial = any(slot["mapping_status"] != "mapped" for slot in slots)
    base = PrinterResponse.model_validate(row).model_dump()
    base.update({
        "filament_slots": slots,
        "filament_source": "filament-ledger",
        "filament_source_status": "partial" if partial else "fresh",
    })
    for slot in slots:
        slot["printer_id"] = printer_id
    return PrinterResponse.model_validate(base)


def enrich_printers(rows: list[Any]) -> list[Any]:
    """Enrich explicitly-bound rows; preserve unconfigured ORM rows unchanged."""
    configured = _configured_printers()
    selected = [row for row in rows if getattr(row, "id", None) in configured]
    if not selected:
        return rows
    url = (getattr(settings, "filament_ledger_url", None) or "").strip()
    if not url:
        return [
            _to_printer_dto(row, None, False) if getattr(row, "id", None) in configured else row
            for row in rows
        ]
    available, ledger_printers = _fetch_printers()
    result = []
    for row in rows:
        printer_id = getattr(row, "id", None)
        if printer_id not in configured:
            result.append(row)
            continue
        key = configured[printer_id]
        result.append(_to_printer_dto(row, ledger_printers.get(key) if available else None, available))
    return result
