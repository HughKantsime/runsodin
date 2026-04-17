"""Bambu MQTT payload models — raw wire shape.

Every field in this module maps to a real key observed in
`odin-e2e/captures/run-2026-04-16` across 4 Bambu printer models (a1,
p1s, h2d, x1c). Fields are Optional unless they appear in 100% of
captured payloads — this matters because different models emit
different subsets of fields (e.g. bambu-p1s has trays with only `id`,
while h2d/x1c trays are fully populated).

Type rationale:
- `extra="allow"` is inherited from `_StrictBase`. Unknown fields flow
  to the UnmappedFieldObserver (T1.9), not silently dropped.
- Fields captured as stringified numbers (`tray_weight: "1000"`) are
  kept as `str` here. Downstream code that needs numeric values parses
  explicitly — keeping the raw string preserves Bambu's exact wire
  format for loss-free replay.
- Fields where Bambu sends both int and str of the same value use
  `BeforeValidator(coerce_int_or_raise)` from `telemetry.validators`
  so the normalization is explicit and fail-loud.
"""
from __future__ import annotations

from typing import Annotated, Optional

from pydantic import BeforeValidator, Field

from backend.modules.printers.telemetry.base import _StrictBase
from backend.modules.printers.telemetry.bambu.hms import BambuHMSEvent
from backend.modules.printers.telemetry.validators import coerce_int_or_raise

# Polymorphic int type used for fields that Bambu sends as both int and
# str-of-int depending on firmware version. Unparseable values raise.
IntLoose = Annotated[Optional[int], BeforeValidator(
    lambda v: None if v is None else coerce_int_or_raise(v)
)]


# ===== AMS tray (the filament slot) =====

class BambuAMSTray(_StrictBase):
    """One filament slot in an AMS unit.

    Observed in captures: 100% of messages have `id`. EVERY other field
    is missing in some messages (notably all of bambu-p1s, which sends
    trays with only `{id}` for several thousand messages). All non-id
    fields must be Optional.

    Empirical basis: per-printer AMS key inventory from
    triage-summary.json + shape inspection of raw.jsonl.
    """

    id: str

    # filament identity (all optional)
    tray_type: Optional[str] = None                 # "PLA", "PETG", etc.
    tray_sub_brands: Optional[str] = None           # "PLA Basic", ""
    tray_color: Optional[str] = None                # "FFFFFFFF" (RRGGBBAA — 8 hex chars, alpha included)
    tray_id_name: Optional[str] = None              # "A00-W01"
    tray_info_idx: Optional[str] = None             # "GFA00" (filament preset ID)
    tray_uuid: Optional[str] = None                 # 32-char hex, unique per filament ID tag
    tag_uid: Optional[str] = None                   # 16-char hex (RFID tag)

    # physical dimensions — Bambu sends stringified numbers
    tray_weight: Optional[str] = None               # "1000" grams
    tray_diameter: Optional[str] = None             # "1.75" mm
    total_len: Optional[int] = None                 # e.g. 330000 (mm × 100? needs verification)
    remain: Optional[int] = None                    # -1 = unknown, 0..100 = percent remaining

    # slicing profile targets — stringified numbers
    nozzle_temp_min: Optional[str] = None
    nozzle_temp_max: Optional[str] = None
    bed_temp: Optional[str] = None
    bed_temp_type: Optional[str] = None
    tray_temp: Optional[str] = None                 # a1 only
    tray_time: Optional[str] = None                 # a1 only
    drying_temp: Optional[str] = None               # h2d/x1c
    drying_time: Optional[str] = None               # h2d/x1c

    # state
    state: Optional[int] = None                     # per-tray state; semantics TBD (unknown enum)
    ctype: Optional[int] = None                     # cartridge type? seen 0, 2
    cali_idx: Optional[int] = None                  # calibration index; -1 = uncalibrated

    # calibration (k = flow coefficient, n = ?)
    k: Optional[float] = None
    n: Optional[int] = None

    # multi-color support — h2d/x1c send a list of hex colors
    cols: Optional[list[str]] = None

    # camera/vision metadata
    xcam_info: Optional[str] = None


# ===== AMS unit (a single AMS hardware unit holding 4 trays) =====

class BambuAMSDrySetting(_StrictBase):
    """Filament drying config — only present on H2D."""

    dry_duration: Optional[int] = None
    dry_filament: Optional[str] = None
    dry_temperature: Optional[int] = None


class BambuAMSUnit(_StrictBase):
    """One physical AMS unit. A printer can have multiple (AMS 2).

    Field presence varies by printer model:
    - a1: check, chip_id, dry_time, humidity, humidity_raw, id, info, temp, tray
    - p1s: ams_id, check, chip_id, dry_time, humidity, humidity_raw, id, info, temp, tray
    - h2d: dry_setting, dry_sf_reason, dry_time, humidity, humidity_raw, id, info, temp, tray
    - x1c: dry_time, humidity, humidity_raw, id, info, temp, tray
    """

    id: str                                         # "0", "1", ...
    info: Optional[str] = None                      # observed but opaque

    # environmental sensors (h2d/x1c AMS 2, a1, p1s all emit these)
    humidity: Optional[str] = None                  # stringified number
    humidity_raw: Optional[str] = None
    temp: Optional[str] = None                      # chamber temp of the AMS itself
    dry_time: Optional[int] = None

    # hardware identity
    chip_id: Optional[str] = None                   # a1, p1s
    ams_id: Optional[str] = None                    # p1s only
    check: Optional[int] = None                     # a1, p1s

    # drying (H2D only)
    dry_setting: Optional[BambuAMSDrySetting] = None
    # NOTE: empirically observed as `list` (always empty `[]` in captures).
    # Bambu firmware documentation isn't public; this may be a reserved
    # field for future reason codes. Keep as list; if int forms appear
    # later we'll switch to Union.
    dry_sf_reason: Optional[list] = None

    tray: list[BambuAMSTray] = Field(default_factory=list)


# ===== AMS root (top-level `print.ams` object) =====

class BambuAMSRoot(_StrictBase):
    """Top-level AMS status block — `print.ams` in the wire payload.

    Shapes seen:
    - p1s: only `{ams: [...]}`.
    - a1/h2d/x1c: rich set with tray_now/tray_exist_bits/etc.
    """

    ams: list[BambuAMSUnit] = Field(default_factory=list)

    # current/previous/target tray selections (hex indices as strings)
    tray_now: Optional[str] = None
    tray_pre: Optional[str] = None
    tray_tar: Optional[str] = None

    # bitmasks (stringified hex) — individual flags TBD, kept opaque for now
    ams_exist_bits: Optional[str] = None
    ams_exist_bits_raw: Optional[str] = None
    tray_exist_bits: Optional[str] = None
    tray_is_bbl_bits: Optional[str] = None
    tray_read_done_bits: Optional[str] = None
    tray_reading_bits: Optional[str] = None
    tray_hall_out_bits: Optional[str] = None        # h2d only
    insert_flag: Optional[bool] = None
    power_on_flag: Optional[bool] = None

    # calibration session state
    cali_id: Optional[int] = None
    cali_stat: Optional[int] = None
    unbind_ams_stat: Optional[int] = None

    version: Optional[int] = None
