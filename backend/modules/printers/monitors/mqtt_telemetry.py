"""
Telemetry parsing helpers for Bambu MQTT monitor.

Pure functions — no class state dependencies. Extracted from PrinterMonitor._on_status()
to keep that method focused on state management.
"""

import math
from typing import Optional


# Stage code to human-readable label mapping from Bambu MQTT protocol
STAGE_MAP = {
    0: 'Idle', 1: 'Auto-leveling', 2: 'Heatbed preheating', 3: 'Sweeping XY',
    4: 'Changing filament', 5: 'Paused', 6: 'Filament runout', 7: 'Heating hotend',
    8: 'Calibrating', 9: 'Homing', 10: 'Cleaning nozzle', 11: 'Heating bed',
    12: 'Scanning bed', 13: 'First layer check', 14: 'Printing', 255: 'Idle', -1: 'Idle'
}

ACTIVE_STATES = ('RUNNING', 'PREPARE', 'PAUSE')


def parse_bambu_speed(state: dict) -> dict:
    """Report protocol mode and measured percentage without guessing either."""
    from modules.printers.telemetry.validators import coerce_int_or_raise
    result = {'speed_level': None, 'speed_percent': None}
    for raw_key, output_key in (('spd_lvl', 'speed_level'), ('spd_mag', 'speed_percent')):
        value = state.get(raw_key)
        if value is None:
            continue
        try:
            value = coerce_int_or_raise(value)
        except ValueError:
            continue
        if output_key == 'speed_level' and value in (1, 2, 3, 4):
            result[output_key] = value
        elif output_key == 'speed_percent' and 0 <= value <= 9007199254740991:
            # JSON consumers must be able to represent the integer exactly.
            result[output_key] = value
    return result


def resolve_stage_label(stg_cur: int, gcode_state: Optional[str]) -> str:
    """Convert numeric stage code to human-readable label.

    Clears to 'Idle' when not in an active print state.
    """
    stage = STAGE_MAP.get(stg_cur, f'Stage {stg_cur}')
    if gcode_state not in ACTIVE_STATES:
        stage = 'Idle'
    return stage


def parse_lights(lights_report: list) -> Optional[bool]:
    """Extract lights_on bool from MQTT lights_report list.

    Returns None if lights_report is empty/missing.
    """
    if not lights_report:
        return None
    return any(l.get('mode') == 'on' for l in lights_report)


def parse_ams_env(ams_raw: dict) -> list:
    """
    Extract AMS unit humidity and temperature from raw AMS payload.
    Returns list of {unit_idx, humidity, temperature} dicts.
    Only includes units that have at least one non-None value.
    """
    results = []
    ams_units = ams_raw.get('ams', []) if isinstance(ams_raw, dict) else []
    if not isinstance(ams_units, list):
        return []
    for unit_idx, unit in enumerate(ams_units):
        if not isinstance(unit, dict):
            continue
        humidity = unit.get('humidity')
        temperature = unit.get('temp')
        # Bambu reports humidity as string "1"-"5" or int
        if humidity is not None:
            try:
                if isinstance(humidity, bool):
                    raise ValueError('Boolean humidity is not a reading')
                parsed_humidity = int(humidity)
                if isinstance(humidity, float) and humidity != parsed_humidity:
                    raise ValueError('Humidity level must be an integer')
                humidity = parsed_humidity if 1 <= parsed_humidity <= 5 else None
            except (ValueError, TypeError, OverflowError):
                humidity = None
        if temperature is not None:
            try:
                if isinstance(temperature, bool):
                    raise ValueError('Boolean temperature is not a reading')
                temperature = float(temperature)
                if not math.isfinite(temperature):
                    temperature = None
            except (ValueError, TypeError, OverflowError):
                temperature = None
        if humidity is not None or temperature is not None:
            results.append({'unit_idx': unit_idx, 'humidity': humidity, 'temperature': temperature})
    return results


def finite_nozzle_measurement(value) -> Optional[float]:
    """Keep unknown or malformed sensor values distinct from measured zero."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        measured = float(value)
    except (ValueError, OverflowError):
        return None
    return measured if math.isfinite(measured) else None


def h2d_extruder_temperatures(print_section: dict) -> dict:
    """Decode print.device.extruder.info[] into {extruder_id: (current, target)}.

    Real H2D frames report per-extruder temperatures only here, packed as
    ``current | target << 16``; they never send ``nozzle_temper_1``.
    """
    device = print_section.get('device') if isinstance(print_section, dict) else None
    extruder = device.get('extruder') if isinstance(device, dict) else None
    info = extruder.get('info') if isinstance(extruder, dict) else None
    result = {}
    if not isinstance(info, list):
        return result
    for entry in info:
        if not isinstance(entry, dict):
            continue
        ident, packed = entry.get('id'), entry.get('temp')
        if (isinstance(ident, bool) or not isinstance(ident, int)
                or isinstance(packed, bool) or not isinstance(packed, int) or not 0 <= packed <= 0xFFFFFFFF):
            continue
        result[ident] = (float(packed & 0xFFFF), float(packed >> 16))
    return result


def parse_h2d_nozzles(state: dict) -> Optional[dict]:
    """
    Extract dual-nozzle temperature data for H2D printers.
    Returns dict with nozzle_0 and nozzle_1 data, or None if no second nozzle data present.

    nozzle_0/nozzle_1 are Bambu extruder ids 0/1 from device.extruder.info
    when reported (real H2D firmware); legacy nozzle_temper(_1) otherwise.
    """
    extruders = h2d_extruder_temperatures(state)
    if 1 in extruders:
        first = extruders.get(0, (finite_nozzle_measurement(state.get('nozzle_temper')),
                                  finite_nozzle_measurement(state.get('nozzle_target_temper'))))
        return {
            'nozzle_0': {'temp': first[0], 'target': first[1]},
            'nozzle_1': {'temp': extruders[1][0], 'target': extruders[1][1]},
        }
    noz_t = finite_nozzle_measurement(state.get('nozzle_temper'))
    noz_tt = finite_nozzle_measurement(state.get('nozzle_target_temper'))
    noz1_t = finite_nozzle_measurement(state.get('nozzle_temper_1'))
    noz1_tt = finite_nozzle_measurement(state.get('nozzle_target_temper_1'))
    if noz1_t is None and noz1_tt is None:
        return None
    return {
        'nozzle_0': {'temp': noz_t, 'target': noz_tt},
        'nozzle_1': {'temp': noz1_t, 'target': noz1_tt},
    }


def _external_spool(ext) -> Optional[dict]:
    if not isinstance(ext, dict) or not ext.get('tray_type'):
        return None
    color_raw = ext.get('tray_color', '')
    if not isinstance(color_raw, str):
        color_raw = ''
    remaining = None
    if ext.get('remain') is not None:
        from modules.printers.telemetry.validators import coerce_int_or_raise
        try:
            remaining = coerce_int_or_raise(ext['remain'])
        except ValueError:
            pass
    return {
        'material': ext.get('tray_type', ''),
        'color': f"#{color_raw}" if color_raw and not color_raw.startswith('#') else color_raw,
        'remain_percent': remaining,
    }


# Bambu virtual tray ids for the external spool holders on dual-extruder
# printers: 254 = deputy (left) extruder, 255 = main (right) extruder.
_VIR_SLOT_SIDES = {'254': 'left', '255': 'right'}


def parse_h2d_external_spools(state: dict) -> Optional[dict]:
    """
    Extract H2D external spool data (Ext-L / Ext-R).

    Real H2D frames report them as print.vir_slot (ids 254/255). The legacy
    print.ams.vt_tray shape is still accepted. ``state`` is the merged print
    section; a bare AMS dict (legacy callers) is accepted too.
    Returns dict of {left: {...}, right: {...}} or None if no external spool data.
    """
    if not isinstance(state, dict):
        return None
    external_spools = {}
    vir_slot = state.get('vir_slot')
    if isinstance(vir_slot, list):
        for idx, ext in enumerate(vir_slot[:2]):
            side = _VIR_SLOT_SIDES.get(str(ext.get('id')) if isinstance(ext, dict) else None,
                                       'left' if idx == 0 else 'right')
            spool = _external_spool(ext)
            if spool:
                external_spools[side] = spool
        return external_spools or None
    ams_raw = state.get('ams') if isinstance(state.get('ams'), dict) else {}
    vt_tray = state.get('vt_tray') or ams_raw.get('vt_tray')
    if not vt_tray:
        return None
    ext_spools_raw = vt_tray if isinstance(vt_tray, list) else [vt_tray]
    for idx, ext in enumerate(ext_spools_raw[:2]):
        spool = _external_spool(ext)
        if spool:
            external_spools['left' if idx == 0 else 'right'] = spool
    return external_spools if external_spools else None
