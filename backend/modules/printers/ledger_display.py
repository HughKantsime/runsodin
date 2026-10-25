"""Read-only Filament Ledger display enrichment for configured Odin printers."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from typing import Any

import httpx

from core.config import settings
from core.interfaces.filament_ownership import (
    EXTERNALLY_MANAGED_DETAIL,
    INTERFACE_NAME as FILAMENT_OWNERSHIP_INTERFACE,
    configured_ledger_printers,
    ensure_local_filament_management,
)
from core.itar import is_private_destination
from core.registry import registry
from core.webhook_utils import pinned_device_destination

log = logging.getLogger("odin.filament_ledger_display")

_CACHE_SECONDS = 15.0
# One deadline for the whole refresh: DNS resolution, connect, headers and body.
_REQUEST_TIMEOUT = 2.0
_MAX_RESPONSE_BYTES = 512 * 1024
_cache_lock = threading.Lock()
_cached_url: str | None = None
_cached_until = 0.0
_cached_printers: dict[str, dict[str, Any]] | None = None
_cached_available = False
_refreshing_url: str | None = None


def _configured_printers() -> dict[int, str]:
    """Return explicit, valid Odin-id to Ledger-key bindings only."""
    return configured_ledger_printers()


class _LedgerFilamentOwnership:
    """Registered provider for the core filament-ownership boundary."""

    def ensure_local_management(self, printer_id: int) -> None:
        from fastapi import HTTPException

        if int(printer_id) in _configured_printers():
            raise HTTPException(
                status_code=409,
                detail=EXTERNALLY_MANAGED_DETAIL,
            )


_OWNERSHIP = _LedgerFilamentOwnership()


def register_providers(registry) -> None:
    registry.register_provider(FILAMENT_OWNERSHIP_INTERFACE, _OWNERSHIP)


def ensure_local_filaments(printer_id: int) -> None:
    """Reject local filament mutations for a printer owned by the Ledger view."""
    ensure_local_filament_management(printer_id)


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


def _fetch_payload(url: str, deadline: float) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Fetch and validate one display payload before ``deadline``.

    Runs inside the bounded refresh worker. Every failure returns an empty
    unavailable result without exposing response bodies, URLs or exception
    details to callers.
    """
    endpoint = f"{url}/api/filament-display"
    try:
        with pinned_device_destination(endpoint) as ips:
            # Validate the exact addresses the connection will use.
            if not ips or not all(is_private_destination(ip) for ip in ips):
                raise ValueError("display destination must be private")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with httpx.Client(
                timeout=httpx.Timeout(remaining),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream("GET", endpoint) as response:
                    if response.status_code != 200:
                        raise ValueError("unexpected display status")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            raise ValueError("display response too large")
                        if time.monotonic() > deadline:
                            raise TimeoutError
        return True, _valid_payload(json.loads(bytes(body)))
    except Exception as exc:
        # Never surface response bodies, URLs, or exception details to clients.
        log.info("Filament Ledger display unavailable (%s)", type(exc).__name__)
        return False, {}


def _fetch_within_deadline(url: str) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Run the refresh in a worker so DNS resolution cannot exceed the deadline.

    The caller waits at most ``_REQUEST_TIMEOUT``. A worker that overruns the
    deadline is abandoned; it cannot publish a late result because only this
    caller updates the cache.
    """
    deadline = time.monotonic() + _REQUEST_TIMEOUT
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def work() -> None:
        try:
            outcome["result"] = _fetch_payload(url, deadline)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            outcome["error"] = exc
        finally:
            finished.set()

    worker = threading.Thread(
        target=work,
        name="filament-ledger-refresh",
        daemon=True,
    )
    worker.start()
    if not finished.wait(max(0.0, deadline - time.monotonic())):
        log.info("Filament Ledger display unavailable (TimeoutError)")
        return False, {}
    if "error" in outcome:
        log.info("Filament Ledger display unavailable (%s)", type(outcome["error"]).__name__)
        return False, {}
    return outcome.get("result", (False, {}))


def _refresh(url: str) -> None:
    """Refresh one URL in a background thread and publish a completed result."""
    global _cached_url, _cached_until, _cached_printers, _cached_available, _refreshing_url
    try:
        available, printers = _fetch_within_deadline(url)
    except Exception:  # pragma: no cover - defensive thread boundary
        available, printers = False, {}
    with _cache_lock:
        if _refreshing_url != url:
            return  # superseded by a newer configuration
        _refreshing_url = None
        _cached_url = url
        _cached_available = available
        _cached_printers = printers if available else None
        # Expiry is measured from when the refresh actually finished.
        _cached_until = time.monotonic() + _CACHE_SECONDS


def _fetch_printers() -> tuple[bool, dict[str, dict[str, Any]]]:
    """Read cached display printers without waiting for a refresh.

    At most one refresh runs for a URL. A request that finds the cache expired
    starts it in the background and is served the last completed snapshot
    (stale is acceptable) or unavailable. Requests never wait on DNS or the
    provider.
    """
    global _refreshing_url
    url = (getattr(settings, "filament_ledger_url", None) or "").strip().rstrip("/")
    if not url:
        return False, {}

    with _cache_lock:
        now = time.monotonic()
        if _cached_url == url and now < _cached_until:
            return _cached_available, _cached_printers or {}
        if _refreshing_url == url:
            # One refresh is already running; serve stale or unavailable now.
            if _cached_url == url and _cached_available:
                return True, _cached_printers or {}
            return False, {}
        _refreshing_url = url
        if _cached_url == url and _cached_available:
            current = (True, _cached_printers or {})
        else:
            current = (False, {})

    threading.Thread(
        target=_refresh,
        args=(url,),
        name="filament-ledger-refresh",
        daemon=True,
    ).start()
    return current


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
            "loaded_colors": [],
            "filament_source": "filament-ledger",
            "filament_source_status": "unavailable",
        })
        return PrinterResponse.model_validate(base)

    if not ledger_printer["tools"]:
        base = PrinterResponse.model_validate(row).model_dump()
        base.update({
            "filament_slots": [],
            "loaded_colors": [],
            "filament_source": "filament-ledger",
            "filament_source_status": "unavailable",
        })
        return PrinterResponse.model_validate(base)
    slots = [_slot_from_tool(tool) for tool in ledger_printer["tools"]]
    partial = any(slot["mapping_status"] != "mapped" for slot in slots)
    base = PrinterResponse.model_validate(row).model_dump()
    base.update({
        "filament_slots": slots,
        "loaded_colors": [],
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


# Register the ownership boundary for app startup (via printers/__init__.py) and
# for route-level harnesses that import this adapter directly.
register_providers(registry)
