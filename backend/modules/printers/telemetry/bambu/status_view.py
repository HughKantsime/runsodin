"""Legacy-compatible view over V2 `PrinterStatus`.

The V2 pipeline produces canonical `PrinterStatus` with 9-state enum
and typed fields. Legacy routes read from `adapters.bambu.PrinterStatus`
— a different dataclass with 6-state enum, int progress, etc.

`BambuV2StatusView` wraps V2's canonical status and exposes attributes
that match legacy's shape so the 10 migration callsites don't need to
rewrite their field accessors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.modules.printers.telemetry.state import PrinterState

if TYPE_CHECKING:  # pragma: no cover
    from backend.modules.printers.telemetry.state import PrinterStatus


# Map V2 canonical state → legacy state string.
# Legacy enum: idle | printing | paused | error | offline | unknown.
# V2 canonical states FINISHED/FAILED/PREPARING/DEGRADED don't exist in
# legacy — we collapse them here for compatibility. Callers that care
# about V2's extra states can read `.v2_state_raw`.
_STATE_TO_LEGACY: dict[PrinterState, str] = {
    PrinterState.IDLE: "idle",
    PrinterState.PRINTING: "printing",
    PrinterState.PREPARING: "printing",       # legacy had no PREPARING
    PrinterState.PAUSED: "paused",
    PrinterState.ERROR: "error",
    PrinterState.OFFLINE: "offline",
    PrinterState.FINISHED: "idle",            # legacy collapses (documented)
    PrinterState.FAILED: "idle",              # legacy collapses (documented)
    PrinterState.DEGRADED: "unknown",         # legacy fallback
}


@dataclass
class AMSSlotCompat:
    """Legacy AMSSlot shape. Only fields routes actually read."""
    slot_number: int
    filament_type: str = ""
    color: str = ""
    color_hex: str = ""
    remaining_percent: int = 0
    rfid_tag: str = ""
    sub_brand: str = ""
    empty: bool = True


class BambuV2StatusView:
    """Read-only legacy-shaped projection of a V2 PrinterStatus.

    Usage:
        view = BambuV2StatusView(v2_status)
        view.state              # "idle" | "printing" | ... (legacy enum str)
        view.print_progress     # int 0-100
        view.bed_temp           # float
        view.ams_slots          # list[AMSSlotCompat]

    Write-backs are intentionally absent — routes that mutate status
    should migrate to the V2 source of truth, not the compatibility
    view.
    """

    def __init__(self, v2_status: "PrinterStatus"):
        self._v2 = v2_status

    # ---- Legacy field surface ----

    @property
    def state(self) -> str:
        return _STATE_TO_LEGACY.get(self._v2.state, "unknown")

    @property
    def print_progress(self) -> int:
        # Legacy exposed int 0-100; V2 has float | None.
        if self._v2.progress_percent is None:
            return 0
        return int(self._v2.progress_percent)

    @property
    def layer_current(self) -> int:
        return self._v2.layer_current or 0

    @property
    def layer_total(self) -> int:
        return self._v2.layer_total or 0

    @property
    def time_remaining_minutes(self) -> int:
        if self._v2.time_remaining_sec is None:
            return 0
        return self._v2.time_remaining_sec // 60

    @property
    def current_file(self) -> str:
        return self._v2.current_file or ""

    @property
    def bed_temp(self) -> float:
        return self._v2.bed_temp if self._v2.bed_temp is not None else 0.0

    @property
    def bed_target(self) -> float:
        return self._v2.bed_target if self._v2.bed_target is not None else 0.0

    @property
    def nozzle_temp(self) -> float:
        return self._v2.nozzle_temp if self._v2.nozzle_temp is not None else 0.0

    @property
    def nozzle_target(self) -> float:
        return self._v2.nozzle_target if self._v2.nozzle_target is not None else 0.0

    @property
    def fan_speed(self) -> int:
        # V2 doesn't model fan_speed as a top-level int; legacy read
        # cooling_fan_speed (str on wire) and coerced to int. Routes
        # that need fan speed should move to reading the BambuPrintSection
        # directly. For now, return 0 to keep legacy contract.
        return 0

    @property
    def ams_slots(self) -> list[AMSSlotCompat]:
        # V2 doesn't materialize AMS slots in PrinterStatus (it's in
        # the BambuReportEvent.section). Routes that need AMS should
        # either (a) be migrated to read from the event directly, or
        # (b) use the `ams_slots_from_section` helper. Empty for now.
        return []

    @property
    def error_message(self) -> str:
        # Legacy had a single error_message string; V2 has ActiveError[].
        # Concatenate for compatibility.
        if not self._v2.active_errors:
            return ""
        return "; ".join(e.message for e in self._v2.active_errors)

    @property
    def printer_type(self) -> str:
        # Legacy read this from the `print` section's `printer_type`
        # field. V2 pulls it from info.module[].product_name via
        # firmware_versions. Return empty string if not set.
        for name, _ in self._v2.firmware_versions:
            if name == "ota":
                # ota module's product_name is the printer model.
                # Not exposed in firmware_versions tuple — would need
                # separate surfacing. For now, empty.
                return ""
        return ""

    @property
    def raw_data(self) -> dict:
        # Legacy had a raw_data dict. V2 doesn't retain raw payloads at
        # the status level. Routes that depend on raw_data must migrate
        # to reading BambuPrintSection directly.
        return {}

    # ---- V2-specific passthroughs — attributes with no legacy equivalent ----

    @property
    def v2_state_raw(self) -> str:
        """The V2 canonical state string (includes FINISHED/FAILED/etc.)."""
        return self._v2.state.value

    @property
    def active_errors(self) -> tuple:
        return self._v2.active_errors

    @property
    def stage_code(self):
        return self._v2.stage_code

    @property
    def firmware_versions(self) -> tuple:
        return self._v2.firmware_versions


def ams_slots_from_section(section) -> list[AMSSlotCompat]:
    """Extract legacy-shaped AMS slot list from a BambuPrintSection.

    Routes that read `.ams_slots` on the legacy status can call this
    with the latest BambuReportEvent.section.
    """
    if section is None or section.ams is None:
        return []

    slots: list[AMSSlotCompat] = []
    slot_number = 1
    for unit in section.ams.ams:
        for tray in unit.tray:
            color_hex = (tray.tray_color or "")[:6] if tray.tray_color else ""
            slots.append(AMSSlotCompat(
                slot_number=slot_number,
                filament_type=tray.tray_type or "",
                color=tray.tray_color or "",
                color_hex=color_hex,
                remaining_percent=int(tray.remain) if tray.remain is not None else 0,
                rfid_tag=tray.tag_uid or "",
                sub_brand=tray.tray_sub_brands or "",
                empty=(not tray.tray_type),
            ))
            slot_number += 1
    return slots
