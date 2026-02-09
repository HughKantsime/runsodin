"""
HMS Error Code Decoder for Bambu Lab Printers.

Decodes HMS codes from their attr+code structure rather than a static lookup table.
Format: attr (32-bit hex) + code (32-bit hex) = 'AABBCCDD_EEFFGGHH'

attr breakdown:
  AA = device (05=AMS Hub, 03=AMS, 07=XCam, 0C=Extruder, 12=Heatbed, etc.)
  BB = module index (01=unit 1, 02=unit 2, etc.)
  CC = error class
  DD = sub-error

code breakdown:
  EEFF = error category
  GGHH = specific error / slot number
"""

DEVICES = {
    0x01: 'Motion Controller',
    0x02: 'Mainboard',
    0x03: 'AMS',
    0x04: 'AMS Hub',
    0x05: 'AMS Hub',
    0x07: 'Camera/XCam',
    0x0A: 'Toolhead',
    0x0C: 'Extruder',
    0x0D: 'Extruder',
    0x10: 'Chamber',
    0x12: 'Heatbed',
}

# Error class meanings by device
ERROR_CLASSES = {
    # AMS / AMS Hub (0x03, 0x05)
    (0x03, 0x01): 'filament runout',
    (0x03, 0x02): 'filament broken or unable to feed',
    (0x03, 0x03): 'filament tangled',
    (0x03, 0x04): 'RFID read failure',
    (0x03, 0x05): 'filament buffer error',
    (0x03, 0x06): 'environment sensor error',
    (0x05, 0x01): 'communication error',
    (0x05, 0x02): 'cutter failure',
    (0x05, 0x03): 'motor overload',
    (0x05, 0x04): 'filament load/unload failure',
    # Extruder (0x0C)
    (0x0C, 0x01): 'temperature abnormal',
    (0x0C, 0x02): 'heating failure',
    (0x0C, 0x03): 'nozzle clog detected',
    (0x0C, 0x04): 'motor stall or jam',
    (0x0C, 0x05): 'filament sensor error',
    # Heatbed (0x12)
    (0x12, 0x01): 'temperature abnormal',
    (0x12, 0x02): 'heating failure',
    (0x12, 0x03): 'adhesion failure detected',
    # Motion (0x01)
    (0x01, 0x01): 'motor stall or endstop error',
    (0x01, 0x02): 'homing failure',
    (0x01, 0x03): 'vibration sensor error',
    (0x01, 0x04): 'calibration failure',
    # Mainboard (0x02)
    (0x02, 0x01): 'memory/storage error',
    (0x02, 0x02): 'firmware error',
    (0x02, 0x03): 'communication bus error',
    (0x02, 0x04): 'power supply error',
    (0x02, 0x05): 'LED controller error',
    # Camera (0x07)
    (0x07, 0x01): 'inspection/detection error',
    (0x07, 0x02): 'lidar error',
    (0x07, 0x03): 'print quality issue detected',
    # Chamber (0x10)
    (0x10, 0x01): 'temperature or fan error',
    (0x10, 0x02): 'door opened during print',
}

# Specific known codes (exact match — high priority real-world codes)
KNOWN_CODES = {
    '05010400_00030004': 'AMS1: Filament load/unload failure on slot 4. Check PTFE path and filament tip.',
    '05010400_00030001': 'AMS1: Filament load/unload failure on slot 1. Check PTFE path and filament tip.',
    '05010400_00030002': 'AMS1: Filament load/unload failure on slot 2.',
    '05010400_00030003': 'AMS1: Filament load/unload failure on slot 3.',
    '05010300_00010001': 'AMS1: Motor current overload on slot 1. Check for filament jam.',
    '05010300_00010002': 'AMS1: Motor current overload on slot 2.',
    '05010300_00010003': 'AMS1: Motor current overload on slot 3.',
    '05010300_00010004': 'AMS1: Motor current overload on slot 4.',
    '05010100_00010001': 'AMS1: Hub communication error. Check AMS cable connection.',
    '05010200_00010001': 'AMS1: Cutter failed. Retry or check cutter mechanism.',
    '03010100_00010001': 'AMS1 Slot 1: Filament has run out. Load new filament.',
    '03010100_00010002': 'AMS1 Slot 2: Filament has run out.',
    '03010100_00010003': 'AMS1 Slot 3: Filament has run out.',
    '03010100_00010004': 'AMS1 Slot 4: Filament has run out.',
    '03010200_00010001': 'AMS1 Slot 1: Filament broken or tangled. Check spool.',
    '03010200_00010002': 'AMS1 Slot 2: Filament broken or tangled.',
    '03010200_00010003': 'AMS1 Slot 3: Filament broken or tangled.',
    '03010200_00010004': 'AMS1 Slot 4: Filament broken or tangled.',
    '03010400_00010001': 'AMS1 Slot 1: RFID tag read failure.',
    '03010400_00010002': 'AMS1 Slot 2: RFID tag read failure.',
    '03010400_00010003': 'AMS1 Slot 3: RFID tag read failure.',
    '03010400_00010004': 'AMS1 Slot 4: RFID tag read failure.',
    '0C010100_00010001': 'Nozzle temperature abnormally high. Possible thermal runaway.',
    '0C010200_00010001': 'Nozzle heating failed. Check heater cartridge and thermistor.',
    '0C010300_00010001': 'Nozzle clog detected. Clean or replace nozzle.',
    '0C010400_00010001': 'Extruder motor stalled. Check for filament jam in gears.',
    '12010100_00010001': 'Heatbed temperature abnormal. Check thermistor connection.',
    '12010200_00010001': 'Heatbed heating failed. Check heater pad.',
    '01010100_00010001': 'X-axis motor stall. Check for obstructions.',
    '01010200_00010001': 'Y-axis motor stall. Check for obstructions.',
    '01010300_00010001': 'Z-axis motor stall. Check lead screw.',
    '01020100_00010001': 'Homing failed. Check endstops and axis movement.',
    '01040100_00010001': 'Auto-leveling failed. Clean nozzle tip and retry.',
    '07010100_00010001': 'First layer inspection failed. Possible adhesion issue.',
    '07010200_00010001': 'Spaghetti detection triggered. Print failure likely.',
    '07020100_00010001': 'Lidar scan failed. Clean lidar window.',
    '02010100_00010001': 'System memory low. Restart printer.',
    '02010200_00010001': 'SD card read error. Check micro SD card.',
    '02010300_00010001': 'Network connection lost.',
    '02020100_00010001': 'Firmware update available.',
    '02040100_00010001': 'Power supply voltage abnormal.',
    '10010100_00010001': 'Chamber temperature too high.',
    '10010200_00010001': 'Chamber fan error.',
    '10020100_00010001': 'Door opened during print. Print paused.',
}


def lookup_hms_code(code: str) -> str:
    """
    Look up human-readable message for an HMS error code.
    Uses exact match first, then structural decode as fallback.
    """
    code = code.upper().strip()
    
    # 1. Exact match
    if code in KNOWN_CODES:
        return KNOWN_CODES[code]
    
    # 2. Structural decode
    if '_' not in code or len(code) < 17:
        return f'Unknown HMS error: {code}'
    
    try:
        attr_hex, code_hex = code.split('_', 1)
        attr_int = int(attr_hex, 16)
        code_int = int(code_hex, 16)
        
        device_id = (attr_int >> 24) & 0xFF
        module = (attr_int >> 16) & 0xFF
        error_class = (attr_int >> 8) & 0xFF
        sub = attr_int & 0xFF
        
        code_high = (code_int >> 16) & 0xFFFF
        code_low = code_int & 0xFFFF
        
        device_name = DEVICES.get(device_id, f'Device 0x{device_id:02X}')
        
        # Try error class lookup
        error_desc = ERROR_CLASSES.get((device_id, error_class))
        if not error_desc:
            error_desc = f'error 0x{error_class:02X}'
        
        # Build message
        parts = [device_name]
        
        # Add unit number if relevant
        if device_id in (0x03, 0x05) and module > 0:
            parts[0] = f'AMS{module}'
        
        parts.append(error_desc)
        
        # Add slot info if code_low looks like a slot number (1-4)
        if 1 <= code_low <= 4 and device_id in (0x03, 0x05):
            parts.append(f'(slot {code_low})')
        
        msg = ': '.join(parts[:2])
        if len(parts) > 2:
            msg += f' {parts[2]}'
        
        return f'{msg}.'
    except Exception:
        return f'HMS error: {code}'


def get_code_count() -> int:
    """Return the number of known HMS codes."""
    return len(KNOWN_CODES)
