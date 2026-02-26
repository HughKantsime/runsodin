"""
Telemetry parsing helpers for Bambu MQTT monitor.

Pure functions — no class state dependencies. Extracted from PrinterMonitor._on_status()
to keep that method focused on state management.
"""

from typing import Optional


# Stage code to human-readable label mapping from Bambu MQTT protocol
STAGE_MAP = {
    0: 'Idle', 1: 'Auto-leveling', 2: 'Heatbed preheating', 3: 'Sweeping XY',
    4: 'Changing filament', 5: 'Paused', 6: 'Filament runout', 7: 'Heating hotend',
    8: 'Calibrating', 9: 'Homing', 10: 'Cleaning nozzle', 11: 'Heating bed',
    12: 'Scanning bed', 13: 'First layer check', 14: 'Printing', 255: 'Idle', -1: 'Idle'
}

ACTIVE_STATES = ('RUNNING', 'PREPARE', 'PAUSE')


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
    for unit_idx, unit in enumerate(ams_units):
        if not isinstance(unit, dict):
            continue
        humidity = unit.get('humidity')
        temperature = unit.get('temp')
        # Bambu reports humidity as string "1"-"5" or int
        if humidity is not None:
            try:
                humidity = int(humidity)
            except (ValueError, TypeError):
                humidity = None
        if temperature is not None:
            try:
                temperature = float(temperature)
            except (ValueError, TypeError):
                temperature = None
        if humidity is not None or temperature is not None:
            results.append({'unit_idx': unit_idx, 'humidity': humidity, 'temperature': temperature})
    return results


def parse_h2d_nozzles(state: dict) -> Optional[dict]:
    """
    Extract dual-nozzle temperature data for H2D printers.
    Returns dict with nozzle_0 and nozzle_1 data, or None if no second nozzle data present.
    """
    noz_t = state.get('nozzle_temper')
    noz_tt = state.get('nozzle_target_temper')
    noz1_t = state.get('nozzle_temper_1')
    noz1_tt = state.get('nozzle_target_temper_1')
    if noz1_t is None and noz1_tt is None:
        return None
    return {
        'nozzle_0': {'temp': noz_t, 'target': noz_tt},
        'nozzle_1': {'temp': noz1_t, 'target': noz1_tt},
    }


def parse_h2d_external_spools(ams_raw: dict) -> Optional[dict]:
    """
    Extract H2D external spool data (Ext-L / Ext-R) from vt_tray in AMS payload.
    Returns dict of {left: {...}, right: {...}} or None if no external spool data.
    """
    if not isinstance(ams_raw, dict):
        return None
    vt_tray = ams_raw.get('vt_tray')
    if not vt_tray:
        return None
    ext_spools_raw = vt_tray if isinstance(vt_tray, list) else [vt_tray]
    external_spools = {}
    for idx, ext in enumerate(ext_spools_raw[:2]):
        if isinstance(ext, dict) and ext.get('tray_type'):
            side = 'left' if idx == 0 else 'right'
            color_raw = ext.get('tray_color', '')
            external_spools[side] = {
                'material': ext.get('tray_type', ''),
                'color': f"#{color_raw}" if color_raw and not color_raw.startswith('#') else color_raw,
                'remain_percent': int(ext.get('remain', 0)) if ext.get('remain') is not None else None,
            }
    return external_spools if external_spools else None
