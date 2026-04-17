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
from backend.modules.printers.telemetry.bambu.enums import BambuGcodeState
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


# ===== The `print` section itself =====

# Float coercion for fields that Bambu sends as both int and float.
# Empirical: bed_temper/nozzle_temper split across float:27067 and int:434
# on h2d/x1c — different firmware versions emit different types.
FloatLoose = Annotated[Optional[float], BeforeValidator(
    lambda v: None if v is None else (
        float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else
        float(str(v).strip()) if isinstance(v, str) and str(v).strip() else
        None
    )
)]


class BambuPrintSection(_StrictBase):
    """The top-level `print` object in a Bambu MQTT report.

    Models ~45 first-class fields out of 105 top-level scalar fields
    observed across 4 printer models in `run-2026-04-16`. Fields
    deliberately NOT modeled here (aux, ap_err, fun, fun2, flag3,
    fail_reason, etc.) flow to the UnmappedFieldObserver via
    `extra="allow"` so we see them without modeling opaque semantics.
    """

    # ---- protocol envelope ----
    command: Optional[str] = None                   # "push_status", "gcode_line", etc.
    sequence_id: Optional[str] = None
    msg: Optional[int] = None                       # sequence counter

    # ---- lifecycle / state ----
    # Strict enum — unknown values raise ValidationError. The adapter
    # catches that as DEGRADED state + loud log + observer ping, instead
    # of the legacy silent-map-to-IDLE-UNKNOWN.
    gcode_state: Optional[BambuGcodeState] = None

    # stg_cur is polymorphic — int OR str-of-int. Normalize to int or raise.
    stg_cur: IntLoose = None
    stg_cd: Optional[int] = None                    # h2d only; stage change detail
    mc_print_stage: Optional[str] = None            # legacy str form; numeric form is stg_cur
    mc_print_sub_stage: Optional[int] = None

    # ---- progress ----
    mc_percent: Optional[int] = None
    percent: Optional[int] = None                   # duplicate-ish; source of truth TBD
    layer_num: Optional[int] = None
    total_layer_num: Optional[int] = None
    mc_remaining_time: Optional[int] = None         # minutes
    remain_time: Optional[int] = None               # duplicate-ish
    prepare_per: Optional[int] = None               # prepare phase percent
    gcode_file_prepare_percent: Optional[str] = None  # stringified

    # ---- temperatures (polymorphic int/float) ----
    bed_temper: FloatLoose = None
    bed_target_temper: FloatLoose = None
    nozzle_temper: FloatLoose = None
    nozzle_target_temper: FloatLoose = None
    chamber_temper: FloatLoose = None               # h2d only

    # ---- fans (all stringified numbers on wire) ----
    cooling_fan_speed: Optional[str] = None
    heatbreak_fan_speed: Optional[str] = None
    big_fan1_speed: Optional[str] = None
    big_fan2_speed: Optional[str] = None
    fan_gear: Optional[int] = None

    # ---- print job identity ----
    gcode_file: Optional[str] = None
    gcode_start_time: Optional[str] = None
    file: Optional[str] = None                      # gcode file path
    profile_id: Optional[str] = None                # slicer profile
    project_id: Optional[str] = None                # Bambu cloud project
    subtask_id: Optional[str] = None                # Bambu cloud subtask
    subtask_name: Optional[str] = None
    task_id: Optional[str] = None                   # Bambu cloud task
    job_id: Optional[str] = None                    # local job id
    model_id: Optional[str] = None
    design_id: Optional[str] = None

    # ---- speed preset ----
    spd_lvl: Optional[int] = None                   # 1=silent, 2=standard, 3=sport, 4=ludicrous
    spd_mag: Optional[int] = None                   # speed magnitude percent

    # ---- error channel ----
    print_error: Optional[int] = None               # 0 = no error; non-zero surfaces as ERROR
    mc_print_error_code: Optional[str] = None
    hms: list[BambuHMSEvent] = Field(default_factory=list)

    # ---- nested structures ----
    ams: Optional[BambuAMSRoot] = None
    ams_status: Optional[int] = None
    ams_rfid_status: Optional[int] = None

    # Less-prioritized nested structures — kept as raw dict/list for now; can
    # be modeled later as the need arises (observer flags them as unmodeled).
    device: Optional[dict] = None                   # h2d chamber/airduct/extruder info
    net: Optional[dict] = None                      # x1c network info
    care: Optional[list[dict]] = None               # maintenance reminders
    lights_report: Optional[list[dict]] = None

    # ---- connectivity / hardware ----
    wifi_signal: Optional[str] = None               # e.g. "-62dBm"
    sdcard: Optional[bool] = None
    home_flag: Optional[int] = None
    hw_switch_state: Optional[int] = None
    nozzle_diameter: Optional[str] = None
    nozzle_type: Optional[str] = None
    printer_type: Optional[str] = None              # historically dead-read but occasionally present

    # ---- legacy / opaque but always-present (avoid observer noise) ----
    cfg: Optional[str] = None                       # printer config blob (stringified)
    ver: Optional[str] = None
    state: Optional[int] = None                     # low-level state; gcode_state is canonical


# ===== The `info` section (firmware/module identity) =====

class BambuInfoModule(_StrictBase):
    """One module's firmware record — e.g. `ota`, `mc`, `ams`, `extruder_0`.

    Empirical basis: bambu-a1 has 5 modules, p1s has 6, x1c has 8, h2d
    has 11. Each module reports its software/hardware version and serial.
    """

    name: str                                       # "ota", "mc", "ams", "extruder_0", "esp32", etc.
    sw_ver: str                                     # "01.07.00.00"
    hw_ver: Optional[str] = None                    # "OTA", "N/A", "v1.0"
    loader_ver: Optional[str] = None                # "00.00.00.00"
    sn: Optional[str] = None                        # module serial (often matches printer serial on ota)
    product_name: Optional[str] = None              # Only on some modules (e.g. "Bambu Lab A1")
    visible: Optional[bool] = None
    flag: Optional[int] = None                      # status bitflag; semantics TBD
    new_ver: Optional[str] = None                   # present when firmware update available (p1s case)


class BambuInfoSection(_StrictBase):
    """The `info` object — printer/module identity + firmware.

    Sent in response to a `get_version` command; also periodically by some
    models. Much less frequent than `print` reports.
    """

    command: str                                    # "get_version"
    sequence_id: Optional[str] = None
    module: list[BambuInfoModule] = Field(default_factory=list)

    # a1/p1s also include these two — ACK envelope from cloud
    reason: Optional[str] = None
    result: Optional[str] = None


# ===== Top-level envelope =====

class InvalidBambuReport(ValueError):
    """Raised when a Bambu MQTT payload has neither `print` nor `info` — fail loud."""


class BambuReport(_StrictBase):
    """Top-level envelope of a Bambu MQTT report.

    A Bambu printer's MQTT topic `device/<serial>/report` emits messages
    that have either a `print` key (status reports, the 99% case), an
    `info` key (module firmware identity, ~15 samples per session), or
    both.

    If a message has neither, it is an unknown envelope — raise
    `InvalidBambuReport` so the adapter can log + surface DEGRADED state
    instead of silently dropping.
    """

    print: Optional[BambuPrintSection] = None
    info: Optional[BambuInfoSection] = None

    def model_post_init(self, __context) -> None:
        if self.print is None and self.info is None:
            raise InvalidBambuReport(
                "Bambu report has neither `print` nor `info` section. "
                f"Keys present: {list(self.model_extra or {})}"
            )

