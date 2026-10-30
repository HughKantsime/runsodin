"""Boundary for filament data owned by an external Filament Ledger display.

Inventory writers must not import the printers display adapter to decide whether
a printer's filament slots are managed elsewhere. The printers module registers
a provider at startup; callers use this boundary instead. When bindings exist
but no provider is registered (partial startup), the boundary still refuses a
local write from configuration rather than allowing one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastapi import HTTPException

from core.config import settings
from core.registry import registry

INTERFACE_NAME = "FilamentOwnershipProvider"
EXTERNALLY_MANAGED_DETAIL = "Filament data for this printer is managed by Filament Ledger"


def configured_ledger_printers() -> dict[int, str]:
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


@runtime_checkable
class FilamentOwnershipProvider(Protocol):
    def ensure_local_management(self, printer_id: int) -> None: ...


def _registered_provider() -> FilamentOwnershipProvider | None:
    if not registry.has_provider(INTERFACE_NAME):
        return None
    provider = registry.get_provider(INTERFACE_NAME)
    if not isinstance(provider, FilamentOwnershipProvider):
        return None
    return provider


def ensure_local_filament_management(printer_id: int) -> None:
    """Refuse a local filament mutation when an external display owns it."""
    try:
        printer_id = int(printer_id)
    except (TypeError, ValueError):
        return
    provider = _registered_provider()
    if provider is not None:
        provider.ensure_local_management(printer_id)
        return
    if printer_id in configured_ledger_printers():
        raise HTTPException(status_code=409, detail=EXTERNALLY_MANAGED_DETAIL)
