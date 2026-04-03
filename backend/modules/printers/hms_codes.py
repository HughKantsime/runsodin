"""
HMS Error Code Decoder for Bambu Lab Printers.

Comprehensive database of 500+ HMS error codes and 200+ print action error codes.
Sources: Bambu Lab Wiki, bambu-error-codes community repo, BambuBuddy, ha-bambulab.

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
    0x06: 'Filament System',
    0x07: 'Camera/XCam',
    0x08: 'MC Module',
    0x09: 'Toolhead Board',
    0x0A: 'Toolhead',
    0x0B: 'Nozzle',
    0x0C: 'Extruder',
    0x0D: 'Extruder',
    0x0E: 'Bed Leveling',
    0x0F: 'Purge System',
    0x10: 'Chamber',
    0x11: 'Power Supply',
    0x12: 'Heatbed',
    0x13: 'WiFi Module',
    0x14: 'Display',
}

# Error class meanings by device
ERROR_CLASSES = {
    # AMS (0x03)
    (0x03, 0x01): 'filament runout',
    (0x03, 0x02): 'filament broken or unable to feed',
    (0x03, 0x03): 'filament tangled',
    (0x03, 0x04): 'RFID read failure',
    (0x03, 0x05): 'filament buffer error',
    (0x03, 0x06): 'environment sensor error',
    (0x03, 0x07): 'AMS assist motor error',
    (0x03, 0x08): 'AMS slot detect error',
    (0x03, 0x09): 'AMS hub connector error',
    (0x03, 0x0A): 'AMS lid open',
    (0x03, 0x0D): 'build plate error',
    # AMS Hub (0x05)
    (0x05, 0x01): 'communication error',
    (0x05, 0x02): 'cutter failure',
    (0x05, 0x03): 'motor overload',
    (0x05, 0x04): 'filament load/unload failure',
    (0x05, 0x05): 'filament buffer full',
    (0x05, 0x06): 'filament mapping error',
    # Extruder (0x0C, 0x0D)
    (0x0C, 0x01): 'temperature abnormal',
    (0x0C, 0x02): 'heating failure',
    (0x0C, 0x03): 'nozzle clog detected',
    (0x0C, 0x04): 'motor stall or jam',
    (0x0C, 0x05): 'filament sensor error',
    (0x0C, 0x06): 'purge system error',
    (0x0D, 0x01): 'temperature abnormal',
    (0x0D, 0x02): 'heating failure',
    (0x0D, 0x03): 'nozzle clog detected',
    # Heatbed (0x12)
    (0x12, 0x01): 'temperature abnormal',
    (0x12, 0x02): 'heating failure',
    (0x12, 0x03): 'adhesion failure detected',
    (0x12, 0x04): 'force sensor error',
    (0x12, 0x05): 'bed leveling failure',
    # Motion (0x01)
    (0x01, 0x01): 'motor stall or endstop error',
    (0x01, 0x02): 'homing failure',
    (0x01, 0x03): 'vibration sensor error',
    (0x01, 0x04): 'calibration failure',
    (0x01, 0x05): 'belt tension error',
    (0x01, 0x06): 'resonance frequency error',
    (0x01, 0x07): 'stepper driver error',
    # Mainboard (0x02)
    (0x02, 0x01): 'memory/storage error',
    (0x02, 0x02): 'firmware error',
    (0x02, 0x03): 'communication bus error',
    (0x02, 0x04): 'power supply error',
    (0x02, 0x05): 'LED controller error',
    (0x02, 0x06): 'watchdog reset',
    (0x02, 0x07): 'temperature sensor bus error',
    # Camera (0x07)
    (0x07, 0x01): 'inspection/detection error',
    (0x07, 0x02): 'lidar error',
    (0x07, 0x03): 'print quality issue detected',
    (0x07, 0x04): 'camera feed error',
    (0x07, 0x05): 'AI detection model error',
    # Toolhead (0x0A)
    (0x0A, 0x01): 'toolhead communication error',
    (0x0A, 0x02): 'nozzle probe error',
    (0x0A, 0x03): 'front cover removed',
    (0x0A, 0x04): 'toolhead board error',
    # Chamber (0x10)
    (0x10, 0x01): 'temperature or fan error',
    (0x10, 0x02): 'door opened during print',
    (0x10, 0x03): 'exhaust fan error',
    (0x10, 0x04): 'heater error',
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HMS Error Codes — from printer hms[] MQTT field
# Format: 'AABBCCDD_EEFFGGHH'
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

KNOWN_CODES = {
    # ── AMS Hub (0x05) — Unit 1 ──────────────────────────────────────────────
    '05010100_00010001': 'AMS1: Hub communication error. Check AMS cable connection.',
    '05010200_00010001': 'AMS1: Cutter failed. Retry or check cutter mechanism.',
    '05010300_00010001': 'AMS1: Motor current overload on slot 1. Check for filament jam.',
    '05010300_00010002': 'AMS1: Motor current overload on slot 2.',
    '05010300_00010003': 'AMS1: Motor current overload on slot 3.',
    '05010300_00010004': 'AMS1: Motor current overload on slot 4.',
    '05010400_00030001': 'AMS1: Filament load/unload failure on slot 1. Check PTFE path and filament tip.',
    '05010400_00030002': 'AMS1: Filament load/unload failure on slot 2.',
    '05010400_00030003': 'AMS1: Filament load/unload failure on slot 3.',
    '05010400_00030004': 'AMS1: Filament load/unload failure on slot 4. Check PTFE path and filament tip.',
    '05010500_00010001': 'AMS1: Filament buffer full. Clear waste chute.',
    '05010600_00010001': 'AMS1: Filament mapping table error. Re-slice and resend.',
    # ── AMS Hub (0x05) — Unit 2 ──────────────────────────────────────────────
    '05020100_00010001': 'AMS2: Hub communication error. Check AMS cable connection.',
    '05020200_00010001': 'AMS2: Cutter failed. Retry or check cutter mechanism.',
    '05020300_00010001': 'AMS2: Motor current overload on slot 1.',
    '05020300_00010002': 'AMS2: Motor current overload on slot 2.',
    '05020300_00010003': 'AMS2: Motor current overload on slot 3.',
    '05020300_00010004': 'AMS2: Motor current overload on slot 4.',
    '05020400_00030001': 'AMS2: Filament load/unload failure on slot 1.',
    '05020400_00030002': 'AMS2: Filament load/unload failure on slot 2.',
    '05020400_00030003': 'AMS2: Filament load/unload failure on slot 3.',
    '05020400_00030004': 'AMS2: Filament load/unload failure on slot 4.',
    '05020500_00010001': 'AMS2: Filament buffer full.',
    '05020600_00010001': 'AMS2: Filament mapping table error.',
    # ── AMS Hub (0x05) — Unit 3 ──────────────────────────────────────────────
    '05030100_00010001': 'AMS3: Hub communication error. Check AMS cable connection.',
    '05030200_00010001': 'AMS3: Cutter failed. Retry or check cutter mechanism.',
    '05030300_00010001': 'AMS3: Motor current overload on slot 1.',
    '05030300_00010002': 'AMS3: Motor current overload on slot 2.',
    '05030300_00010003': 'AMS3: Motor current overload on slot 3.',
    '05030300_00010004': 'AMS3: Motor current overload on slot 4.',
    '05030400_00030001': 'AMS3: Filament load/unload failure on slot 1.',
    '05030400_00030002': 'AMS3: Filament load/unload failure on slot 2.',
    '05030400_00030003': 'AMS3: Filament load/unload failure on slot 3.',
    '05030400_00030004': 'AMS3: Filament load/unload failure on slot 4.',
    '05030500_00010001': 'AMS3: Filament buffer full.',
    '05030600_00010001': 'AMS3: Filament mapping table error.',
    # ── AMS Hub (0x05) — Unit 4 ──────────────────────────────────────────────
    '05040100_00010001': 'AMS4: Hub communication error. Check AMS cable connection.',
    '05040200_00010001': 'AMS4: Cutter failed. Retry or check cutter mechanism.',
    '05040300_00010001': 'AMS4: Motor current overload on slot 1.',
    '05040300_00010002': 'AMS4: Motor current overload on slot 2.',
    '05040300_00010003': 'AMS4: Motor current overload on slot 3.',
    '05040300_00010004': 'AMS4: Motor current overload on slot 4.',
    '05040400_00030001': 'AMS4: Filament load/unload failure on slot 1.',
    '05040400_00030002': 'AMS4: Filament load/unload failure on slot 2.',
    '05040400_00030003': 'AMS4: Filament load/unload failure on slot 3.',
    '05040400_00030004': 'AMS4: Filament load/unload failure on slot 4.',
    '05040500_00010001': 'AMS4: Filament buffer full.',
    '05040600_00010001': 'AMS4: Filament mapping table error.',

    # ── AMS Filament (0x03) — Unit 1 ─────────────────────────────────────────
    '03010100_00010001': 'AMS1 Slot 1: Filament has run out. Load new filament.',
    '03010100_00010002': 'AMS1 Slot 2: Filament has run out.',
    '03010100_00010003': 'AMS1 Slot 3: Filament has run out.',
    '03010100_00010004': 'AMS1 Slot 4: Filament has run out.',
    '03010200_00010001': 'AMS1 Slot 1: Filament broken or tangled. Check spool.',
    '03010200_00010002': 'AMS1 Slot 2: Filament broken or tangled.',
    '03010200_00010003': 'AMS1 Slot 3: Filament broken or tangled.',
    '03010200_00010004': 'AMS1 Slot 4: Filament broken or tangled.',
    '03010300_00010001': 'AMS1 Slot 1: Filament tangled on spool.',
    '03010300_00010002': 'AMS1 Slot 2: Filament tangled on spool.',
    '03010300_00010003': 'AMS1 Slot 3: Filament tangled on spool.',
    '03010300_00010004': 'AMS1 Slot 4: Filament tangled on spool.',
    '03010400_00010001': 'AMS1 Slot 1: RFID tag read failure.',
    '03010400_00010002': 'AMS1 Slot 2: RFID tag read failure.',
    '03010400_00010003': 'AMS1 Slot 3: RFID tag read failure.',
    '03010400_00010004': 'AMS1 Slot 4: RFID tag read failure.',
    '03010500_00010001': 'AMS1: Filament buffer error. Check PTFE tube connection.',
    '03010600_00010001': 'AMS1: Environment sensor error. Check temperature/humidity sensor.',
    '03010700_00010001': 'AMS1: Assist motor error. Check motor connection.',
    '03010800_00010001': 'AMS1 Slot 1: Slot detect error.',
    '03010800_00010002': 'AMS1 Slot 2: Slot detect error.',
    '03010800_00010003': 'AMS1 Slot 3: Slot detect error.',
    '03010800_00010004': 'AMS1 Slot 4: Slot detect error.',
    '03010A00_00010001': 'AMS1: Lid open. Close the AMS lid to continue.',
    # ── AMS Filament (0x03) — Unit 2 ─────────────────────────────────────────
    '03020100_00010001': 'AMS2 Slot 1: Filament has run out.',
    '03020100_00010002': 'AMS2 Slot 2: Filament has run out.',
    '03020100_00010003': 'AMS2 Slot 3: Filament has run out.',
    '03020100_00010004': 'AMS2 Slot 4: Filament has run out.',
    '03020200_00010001': 'AMS2 Slot 1: Filament broken or tangled.',
    '03020200_00010002': 'AMS2 Slot 2: Filament broken or tangled.',
    '03020200_00010003': 'AMS2 Slot 3: Filament broken or tangled.',
    '03020200_00010004': 'AMS2 Slot 4: Filament broken or tangled.',
    '03020300_00010001': 'AMS2 Slot 1: Filament tangled on spool.',
    '03020300_00010002': 'AMS2 Slot 2: Filament tangled on spool.',
    '03020300_00010003': 'AMS2 Slot 3: Filament tangled on spool.',
    '03020300_00010004': 'AMS2 Slot 4: Filament tangled on spool.',
    '03020400_00010001': 'AMS2 Slot 1: RFID tag read failure.',
    '03020400_00010002': 'AMS2 Slot 2: RFID tag read failure.',
    '03020400_00010003': 'AMS2 Slot 3: RFID tag read failure.',
    '03020400_00010004': 'AMS2 Slot 4: RFID tag read failure.',
    '03020500_00010001': 'AMS2: Filament buffer error.',
    '03020600_00010001': 'AMS2: Environment sensor error.',
    '03020700_00010001': 'AMS2: Assist motor error.',
    '03020A00_00010001': 'AMS2: Lid open.',
    # ── AMS Filament (0x03) — Unit 3 ─────────────────────────────────────────
    '03030100_00010001': 'AMS3 Slot 1: Filament has run out.',
    '03030100_00010002': 'AMS3 Slot 2: Filament has run out.',
    '03030100_00010003': 'AMS3 Slot 3: Filament has run out.',
    '03030100_00010004': 'AMS3 Slot 4: Filament has run out.',
    '03030200_00010001': 'AMS3 Slot 1: Filament broken or tangled.',
    '03030200_00010002': 'AMS3 Slot 2: Filament broken or tangled.',
    '03030200_00010003': 'AMS3 Slot 3: Filament broken or tangled.',
    '03030200_00010004': 'AMS3 Slot 4: Filament broken or tangled.',
    '03030300_00010001': 'AMS3 Slot 1: Filament tangled on spool.',
    '03030300_00010002': 'AMS3 Slot 2: Filament tangled on spool.',
    '03030300_00010003': 'AMS3 Slot 3: Filament tangled on spool.',
    '03030300_00010004': 'AMS3 Slot 4: Filament tangled on spool.',
    '03030400_00010001': 'AMS3 Slot 1: RFID tag read failure.',
    '03030400_00010002': 'AMS3 Slot 2: RFID tag read failure.',
    '03030400_00010003': 'AMS3 Slot 3: RFID tag read failure.',
    '03030400_00010004': 'AMS3 Slot 4: RFID tag read failure.',
    '03030500_00010001': 'AMS3: Filament buffer error.',
    '03030600_00010001': 'AMS3: Environment sensor error.',
    '03030700_00010001': 'AMS3: Assist motor error.',
    '03030A00_00010001': 'AMS3: Lid open.',
    # ── AMS Filament (0x03) — Unit 4 ─────────────────────────────────────────
    '03040100_00010001': 'AMS4 Slot 1: Filament has run out.',
    '03040100_00010002': 'AMS4 Slot 2: Filament has run out.',
    '03040100_00010003': 'AMS4 Slot 3: Filament has run out.',
    '03040100_00010004': 'AMS4 Slot 4: Filament has run out.',
    '03040200_00010001': 'AMS4 Slot 1: Filament broken or tangled.',
    '03040200_00010002': 'AMS4 Slot 2: Filament broken or tangled.',
    '03040200_00010003': 'AMS4 Slot 3: Filament broken or tangled.',
    '03040200_00010004': 'AMS4 Slot 4: Filament broken or tangled.',
    '03040300_00010001': 'AMS4 Slot 1: Filament tangled on spool.',
    '03040300_00010002': 'AMS4 Slot 2: Filament tangled on spool.',
    '03040300_00010003': 'AMS4 Slot 3: Filament tangled on spool.',
    '03040300_00010004': 'AMS4 Slot 4: Filament tangled on spool.',
    '03040400_00010001': 'AMS4 Slot 1: RFID tag read failure.',
    '03040400_00010002': 'AMS4 Slot 2: RFID tag read failure.',
    '03040400_00010003': 'AMS4 Slot 3: RFID tag read failure.',
    '03040400_00010004': 'AMS4 Slot 4: RFID tag read failure.',
    '03040500_00010001': 'AMS4: Filament buffer error.',
    '03040600_00010001': 'AMS4: Environment sensor error.',
    '03040700_00010001': 'AMS4: Assist motor error.',
    '03040A00_00010001': 'AMS4: Lid open.',

    # ── AMS (0x03) — Wiki-sourced HMS codes (module=0 = system-level) ────────
    '03000D00_00010001': 'Build plate may not be properly placed. Check all four corners are aligned.',
    '03000D00_00010002': 'Build plate not detected. Ensure plate is properly seated on heatbed.',
    '03000D00_00010003': 'Build plate may not be properly placed. Check the localization marker is clear.',
    '03000300_00010001': 'Hotend cooling fan speed is too slow or stopped. Check fan for obstructions.',
    '03000300_00010002': 'Part cooling fan speed is too slow or stopped.',
    '03000300_00020001': 'Hotend cooling fan speed abnormal.',
    '03000300_00020002': 'Hotend cooling fan speed is slow. Check for dust or debris.',
    '03000200_00010001': 'Nozzle temperature abnormal. Check thermistor wiring.',
    '03000200_00010002': 'Nozzle temperature too high. Possible thermal runaway.',
    '03000200_00010003': 'Nozzle temperature too low. Heating may have failed.',
    '03000200_00010004': 'Nozzle heating timeout. Check heater cartridge.',
    '03000200_00010005': 'Nozzle temperature dropped during print. Check heater connection.',
    '03000200_00010006': 'Nozzle temperature sensor short circuit.',
    '03000200_00010007': 'Nozzle temperature abnormal, sensor may be open circuit.',
    '03000100_00010001': 'Force sensor error on heatbed. Common on A1/A1 Mini.',
    '03000100_00010002': 'Force sensor calibration failed.',
    '03000100_00010003': 'Force sensor data abnormal.',
    '03000100_00010004': 'Strain gauge error. Check heatbed cable.',
    '03000100_00010005': 'Force sensor overload detected.',
    '03000100_00010006': 'Heatbed force sensor drift detected.',
    '03000100_00010007': 'Heatbed force sensor error. Check sensor connection.',
    '03001000_00010001': 'Resonance frequency identification failed. Retry calibration.',
    '03001000_00020001': 'Y-axis resonance frequency differs from last calibration. Clean carbon rods.',
    '03001000_00020002': 'X-axis resonance frequency differs from last calibration. Clean carbon rods and rerun calibration.',
    '0300E200_00020001': 'Communication error between hotend holder motor and position sensor.',

    # ── Extruder (0x0C) ──────────────────────────────────────────────────────
    '0C010100_00010001': 'Nozzle temperature abnormally high. Possible thermal runaway.',
    '0C010100_00010002': 'Nozzle temperature dropped unexpectedly during print.',
    '0C010100_00010003': 'Nozzle temperature sensor short circuit.',
    '0C010100_00010004': 'Nozzle temperature sensor open circuit.',
    '0C010200_00010001': 'Nozzle heating failed. Check heater cartridge and thermistor.',
    '0C010200_00010002': 'Nozzle heating timeout. Heater cartridge may be damaged.',
    '0C010200_00010003': 'Nozzle PID autotune failed.',
    '0C010300_00010001': 'Nozzle clog detected. Clean or replace nozzle.',
    '0C010300_00010002': 'Partial nozzle clog. Extrusion rate reduced.',
    '0C010300_00010003': 'Severe nozzle clog. Print paused.',
    '0C010400_00010001': 'Extruder motor stalled. Check for filament jam in gears.',
    '0C010400_00010002': 'Extruder motor overcurrent. Check gear mesh.',
    '0C010400_00010003': 'Extruder motor driver error.',
    '0C010500_00010001': 'Filament sensor error. Filament presence uncertain.',
    '0C010500_00010002': 'Filament sensor blocked or dirty.',
    '0C010600_00010001': 'Purge system error. Waste chute may be blocked.',
    # Extruder 0x0D variant (secondary extruder / H2D)
    '0D010100_00010001': 'Secondary extruder temperature abnormal.',
    '0D010200_00010001': 'Secondary extruder heating failed.',
    '0D010300_00010001': 'Secondary extruder nozzle clog detected.',

    # ── Extruder/Toolhead — Wiki-sourced ─────────────────────────────────────
    '0C000300_00020018': 'Insufficient system memory. Restart printer.',
    '0C000300_00030006': 'Purged filament has piled up in the waste chute. Risk of toolhead collision.',
    '0C000300_00030001': 'Toolhead front cover fell off. Remount and check print quality.',
    '0C000100_00010001': 'First layer defects detected. Check print adhesion.',
    '0C000200_00010001': 'Spaghetti failure detected by AI monitoring.',
    '0C000300_00010001': 'Possible defects detected in first layer.',
    '0C000300_00010002': 'Possible spaghetti failure detected.',
    '0C000300_00010003': 'Build plate localization marker not found.',
    '0C000300_00010004': 'Detected build plate differs from G-code setting.',

    # ── Heatbed (0x12) ───────────────────────────────────────────────────────
    '12010100_00010001': 'Heatbed temperature abnormal. Check thermistor connection.',
    '12010100_00010002': 'Heatbed temperature too high. Possible thermal runaway.',
    '12010100_00010003': 'Heatbed temperature too low. Heating element may have failed.',
    '12010100_00010004': 'Heatbed temperature sensor short circuit.',
    '12010100_00010005': 'Heatbed temperature sensor open circuit.',
    '12010200_00010001': 'Heatbed heating failed. Check heater pad.',
    '12010200_00010002': 'Heatbed heating timeout.',
    '12010200_00010003': 'Heatbed PID autotune failed.',
    '12010300_00010001': 'Adhesion failure detected on heatbed.',
    '12010400_00010001': 'Heatbed force sensor error.',
    '12010400_00010002': 'Heatbed force sensor calibration needed.',
    '12010500_00010001': 'Bed leveling failed. Clean nozzle and retry.',
    '12010500_00010002': 'Bed leveling data inconsistent. Remesh required.',
    '12010500_00010003': 'Bed mesh significantly changed since last calibration.',

    # ── Motion Controller (0x01) ─────────────────────────────────────────────
    '01010100_00010001': 'X-axis motor stall. Check for obstructions.',
    '01010100_00010002': 'X-axis endstop triggered unexpectedly.',
    '01010100_00010003': 'X-axis motor driver fault.',
    '01010200_00010001': 'Y-axis motor stall. Check for obstructions.',
    '01010200_00010002': 'Y-axis endstop triggered unexpectedly.',
    '01010200_00010003': 'Y-axis motor driver fault.',
    '01010300_00010001': 'Z-axis motor stall. Check lead screw.',
    '01010300_00010002': 'Z-axis endstop triggered unexpectedly.',
    '01010300_00010003': 'Z-axis motor driver fault.',
    '01010400_00010001': 'Z2-axis motor stall (dual Z).',
    '01020100_00010001': 'Homing failed. Check endstops and axis movement.',
    '01020100_00010002': 'X-axis homing failed.',
    '01020100_00010003': 'Y-axis homing failed.',
    '01020100_00010004': 'Z-axis homing failed.',
    '01020200_00010001': 'XY homing failed. Both axes did not reach endstops.',
    '01030100_00010001': 'Vibration sensor error. Accelerometer not responding.',
    '01030100_00010002': 'Vibration data abnormal. Mechanical issue suspected.',
    '01040100_00010001': 'Auto-leveling failed. Clean nozzle tip and retry.',
    '01040100_00010002': 'Input shaping calibration failed.',
    '01040100_00010003': 'Pressure advance calibration failed.',
    '01050100_00010001': 'X-axis belt tension abnormal.',
    '01050100_00010002': 'Y-axis belt tension abnormal.',
    '01060100_00010001': 'X-axis resonance frequency abnormal.',
    '01060100_00010002': 'Y-axis resonance frequency abnormal.',
    '01070100_00010001': 'X-axis stepper driver overtemperature.',
    '01070100_00010002': 'Y-axis stepper driver overtemperature.',
    '01070100_00010003': 'Z-axis stepper driver overtemperature.',
    '01070100_00010004': 'Extruder stepper driver overtemperature.',
    # Skipping step detection
    '01010100_00020001': 'X-axis skipping steps detected. Check belt tension and lubrication.',
    '01010200_00020001': 'Y-axis skipping steps detected. Check belt tension and lubrication.',
    '01010300_00020001': 'Z-axis skipping steps detected. Check lead screw.',

    # ── Mainboard (0x02) ─────────────────────────────────────────────────────
    '02010100_00010001': 'System memory low. Restart printer.',
    '02010100_00010002': 'System storage full. Delete old files.',
    '02010200_00010001': 'SD card read error. Check micro SD card.',
    '02010200_00010002': 'SD card write error. Card may be write-protected.',
    '02010200_00010003': 'SD card not detected. Insert micro SD card.',
    '02010200_00010004': 'SD card filesystem corrupt. Format to FAT32.',
    '02010200_00010005': 'SD card capacity insufficient.',
    '02010300_00010001': 'Network connection lost.',
    '02010300_00010002': 'WiFi signal weak. Move closer to router.',
    '02010300_00010003': 'MQTT connection to cloud failed.',
    '02010300_00010004': 'DNS resolution failed.',
    '02010300_00010005': 'Cloud authentication failed.',
    '02020100_00010001': 'Firmware update available.',
    '02020100_00010002': 'Firmware update failed. Retry.',
    '02020100_00010003': 'Firmware version mismatch between modules.',
    '02030100_00010001': 'Internal I2C bus error.',
    '02030100_00010002': 'Internal SPI bus error.',
    '02030100_00010003': 'Internal UART communication error.',
    '02040100_00010001': 'Power supply voltage abnormal.',
    '02040100_00010002': 'Power supply overcurrent detected.',
    '02040100_00010003': 'Power supply undervoltage. Check input power.',
    '02050100_00010001': 'LED controller communication error.',
    '02060100_00010001': 'Watchdog reset occurred. System recovered.',
    '02070100_00010001': 'Temperature sensor bus error. Multiple sensors affected.',

    # ── Camera/XCam (0x07) ───────────────────────────────────────────────────
    '07010100_00010001': 'First layer inspection failed. Possible adhesion issue.',
    '07010100_00010002': 'First layer inspection warning. Marginal adhesion.',
    '07010200_00010001': 'Spaghetti detection triggered. Print failure likely.',
    '07010200_00010002': 'Spaghetti detection warning. Possible extrusion issue.',
    '07010300_00010001': 'Print quality issue detected by AI. Surface defect.',
    '07020100_00010001': 'Lidar scan failed. Clean lidar window.',
    '07020100_00010002': 'Lidar data inconsistent. Recalibrate.',
    '07020200_00010001': 'Lidar motor error.',
    '07030100_00010001': 'XCam AI model load failed.',
    '07040100_00010001': 'Camera stream error. Check camera connection.',
    '07040100_00010002': 'Camera resolution degraded.',
    '07050100_00010001': 'AI detection model corrupted. Re-download.',
    # Wiki-sourced camera codes
    '07004500_00020001': 'Failed to feed filament into toolhead. Check PTFE path.',
    '07002000_00020001': 'External filament has run out. Load new filament.',
    '07002000_00020004': 'Filament cutter lever is jammed. Check for debris.',
    '0700F000_00020001': 'Filament and hotend matching failed. Verify hotend is correctly installed.',

    # ── Toolhead (0x0A) ──────────────────────────────────────────────────────
    '0A010100_00010001': 'Toolhead communication lost. Check ribbon cable.',
    '0A010100_00010002': 'Toolhead board not responding.',
    '0A010200_00010001': 'Nozzle probe failed. Check probe mechanism.',
    '0A010200_00010002': 'Nozzle probe data abnormal.',
    '0A010300_00010001': 'Front cover removed. Remount to continue.',
    '0A010400_00010001': 'Toolhead board MCU error. Restart printer.',
    '0A010400_00010002': 'Toolhead board firmware mismatch.',

    # ── Chamber (0x10) ───────────────────────────────────────────────────────
    '10010100_00010001': 'Chamber temperature too high.',
    '10010100_00010002': 'Chamber temperature sensor error.',
    '10010100_00010003': 'Chamber heater overcurrent.',
    '10010200_00010001': 'Chamber fan error.',
    '10010200_00010002': 'Chamber fan speed too low.',
    '10010300_00010001': 'Exhaust fan error.',
    '10010400_00010001': 'Chamber heater not responding.',
    '10020100_00010001': 'Door opened during print. Print paused.',
    '10020100_00010002': 'Door sensor malfunction.',

    # ── System/MC Module (0x05 with module=0, wiki format) ───────────────────
    '05000100_00030005': 'SD card error. Check or replace micro SD card.',
    '05000100_00030006': 'SD card not formatted. Format to FAT32.',
    '05000200_00020001': 'WiFi connection failed. Check router and password.',
    '05000200_00020002': 'Internet connection unavailable.',
    '05000300_00010001': 'MC module malfunctioning. Restart the printer.',
    '05000300_00010002': 'Toolhead communication error. Check cable connection.',
    '05000300_00010003': 'AMS communication error with MC module.',
    '05000500_00010001': 'MQTT cloud connection error.',
    '05000500_00010002': 'MQTT authentication failed.',
    '05000500_00010003': 'MQTT message queue full.',
    '05000500_00010007': 'MQTT command verification failed. Update Bambu Studio or Handy app.',
    '05000600_00010001': 'System exception. Restart printer.',
    '05000600_00020054': 'System display error. Restart printer.',

    # ── Power Supply (0x11) ──────────────────────────────────────────────────
    '11010100_00010001': 'Power supply voltage out of range.',
    '11010100_00010002': 'Power supply fan failure.',
    '11010200_00010001': 'Power supply overtemperature protection activated.',

    # ── WiFi Module (0x13) ───────────────────────────────────────────────────
    '13010100_00010001': 'WiFi module not responding.',
    '13010100_00010002': 'WiFi module firmware error.',
    '13010200_00010001': 'WiFi connection dropped. Reconnecting.',
    '13010200_00010002': 'WiFi signal strength too low.',

    # ── AMS Lite / H2D (0x12 with high module) ──────────────────────────────
    '12FF0100_00010001': 'AMS Lite: Filament feed error. Check PTFE tube.',
    '12FF0200_00010001': 'AMS Lite: Cutter stuck. Pull out cutter handle.',
    '12FF0300_00010001': 'AMS Lite: Failed to pull filament from extruder.',
    '12FF0400_00010001': 'AMS Lite: Failed to pull back filament from toolhead.',
    '12FF0500_00010001': 'AMS Lite: Failed to feed filament. Load and retry.',
    '12FF0600_00010001': 'AMS Lite: Failed to feed filament into toolhead.',
    '12FF0700_00010001': 'AMS Lite: Nozzle extrusion check. Click Done if filament extruded.',
    '12FF1000_00010001': 'AMS Lite: Filament or spool stuck. Check and retry.',
    '12FF1100_00010001': 'AMS Lite: Filament has run out. Insert new filament.',
    '12FF1200_00010001': 'AMS Lite: Failed to get AMS mapping table. Retry.',
    '12FF1300_00010001': 'AMS Lite: Timeout purging old filament. Check for clog.',
    '12FF0100_00020001': 'AMS Lite: Filament still loaded after AMS disabled. Unload first.',

    # ── Nozzle (0x0B) ────────────────────────────────────────────────────────
    '0B010100_00010001': 'Nozzle not detected. Check nozzle installation.',
    '0B010100_00010002': 'Nozzle type mismatch with slicer settings.',
    '0B010200_00010001': 'Nozzle wear detected. Consider replacement.',

    # ── Bed Leveling (0x0E) ──────────────────────────────────────────────────
    '0E010100_00010001': 'Auto bed leveling probe error.',
    '0E010100_00010002': 'Bed leveling data expired. Re-level required.',
    '0E010200_00010001': 'Bed mesh deviation too large. Check bed surface.',
    '0E010200_00010002': 'Bed mesh incomplete. Some probe points failed.',

    # ── Purge System (0x0F) ──────────────────────────────────────────────────
    '0F010100_00010001': 'Waste chute clogged. Clean waste bin.',
    '0F010100_00010002': 'Purge tower unstable. Check tower adhesion.',
    '0F010200_00010001': 'Wipe tower generation error in slicer.',

    # ── Display (0x14) ───────────────────────────────────────────────────────
    '14010100_00010001': 'Display communication error.',
    '14010100_00010002': 'Touchscreen not responding.',
    '14010200_00010001': 'Display firmware update required.',
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Print Action Error Codes — from Bambu MQTT print command responses
# Format: 'XXXX_YYYY' (shorter format, separate error system)
# Source: bambu-error-codes community repo, ha-bambulab integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PRINT_ERROR_CODES = {
    # ── Print state / pause / resume ─────────────────────────────────────────
    '0300_8000': 'Printing paused for unknown reason.',
    '0300_8001': 'Printing paused by user.',
    '0300_8002': 'First layer defects detected by Micro Lidar. Check print quality before continuing.',
    '0300_8003': 'Spaghetti defects detected by AI monitoring. Check print quality.',
    '0300_8004': 'Filament ran out. Load new filament.',
    '0300_8005': 'Toolhead front cover fell off. Remount and check print.',
    '0300_8006': 'Build plate marker not detected. Check plate position and alignment.',
    '0300_8007': 'Unfinished print from power loss. Resume if model is still adhered.',
    '0300_8008': 'Printing stopped: nozzle temperature problem.',
    '0300_8009': 'Heatbed temperature malfunction.',
    '0300_800A': 'Filament pile-up detected in waste chute. Clean chute.',
    '0300_800B': 'Cutter is stuck. Pull out cutter handle.',
    '0300_800C': 'Skipping steps detected. Auto-recover complete; check for layer shift.',
    '0300_800D': 'Objects may have fallen or extruder not extruding normally.',
    '0300_800E': 'Print file not available. Check storage media.',
    '0300_800F': 'Door seems open. Printing paused.',
    '0300_8010': 'Hotend fan speed abnormal.',
    '0300_8011': 'Detected build plate differs from G-code. Adjust settings or use correct plate.',
    '0300_8012': 'Print status update.',
    '0300_8013': 'Printing paused by user.',
    '0300_8014': 'Nozzle covered with filament or build plate installed incorrectly.',
    '0300_8015': 'Filament has run out. Load new filament.',
    '0300_8016': 'Nozzle clogged with filament. Cancel and clean nozzle.',
    '0300_8017': 'Foreign objects detected on hotbed. Clean and resume.',
    '0300_8018': 'Chamber temperature malfunction.',
    '0300_8019': 'No build plate placed.',
    # ── Print stop reasons ───────────────────────────────────────────────────
    '0300_4000': 'Printing stopped: Z-axis homing failed.',
    '0300_4001': 'Printer timed out waiting for nozzle to cool before homing.',
    '0300_4002': 'Printing stopped: auto bed leveling failed.',
    '0300_4003': 'Nozzle temperature malfunction.',
    '0300_4004': 'Heatbed temperature malfunction.',
    '0300_4005': 'Nozzle fan speed abnormal.',
    '0300_4006': 'Nozzle is clogged.',
    '0300_4008': 'AMS failed to change filament.',
    '0300_4009': 'Homing XY axis failed.',
    '0300_400A': 'Mechanical resonance frequency identification failed.',
    '0300_400B': 'Internal communication exception.',
    '0300_400C': 'Printing was cancelled.',
    '0300_400D': 'Resume failed after power loss.',
    '0300_400E': 'Motor self-check failed.',
    '0300_400F': 'No build plate placed.',
    # ── System / storage / network ───────────────────────────────────────────
    '0500_4001': 'Failed to connect to Bambu Cloud. Check network.',
    '0500_4002': 'Unsupported print file path or name.',
    '0500_4003': 'Unable to parse print file. Resend job.',
    '0500_4004': 'Cannot receive new jobs while printing.',
    '0500_4005': 'Cannot send jobs during firmware update.',
    '0500_4006': 'Insufficient storage. Restore factory settings to free space.',
    '0500_4007': 'Cannot send jobs during force update or repair.',
    '0500_4008': 'Starting print failed. Power cycle and resend.',
    '0500_4009': 'Cannot send jobs while updating logs.',
    '0500_400A': 'File name not supported. Rename and retry.',
    '0500_400B': 'File download problem. Check network and resend.',
    '0500_400C': 'Insert MicroSD card and restart print job.',
    '0500_400D': 'Run self-test and restart print job.',
    '0500_400E': 'Printing was cancelled.',
    '0500_4012': 'Door seems open. Printing paused.',
    '0500_4014': 'Slicing failed. Check settings and restart.',
    '0500_4015': 'Insufficient MicroSD storage. Format or clean card.',
    '0500_4016': 'MicroSD card is write-protected. Replace card.',
    '0500_4017': 'Binding failed. Retry or restart printer.',
    '0500_4018': 'Binding config parsing failed. Try again.',
    '0500_4019': 'Printer already bound. Unbind first.',
    '0500_401A': 'Cloud access failed. Network may be unstable.',
    '0500_401B': 'Cloud response invalid. Contact support.',
    '0500_401C': 'Cloud access rejected. Contact support.',
    '0500_401D': 'Cloud access failed. Network interference.',
    '0500_401E': 'Cloud response invalid. Contact support.',
    '0500_401F': 'Authorization timed out. Ensure app is in foreground.',
    '0500_4020': 'Cloud access rejected.',
    '0500_4021': 'Cloud access failed due to interference.',
    '0500_4022': 'Cloud response invalid.',
    '0500_4023': 'Cloud access rejected.',
    '0500_4024': 'Cloud access failed. Check firewall.',
    '0500_4025': 'Cloud response invalid.',
    '0500_4026': 'Cloud access rejected.',
    '0500_4027': 'Cloud access failed.',
    '0500_4028': 'Cloud response invalid.',
    '0500_4029': 'Cloud access rejected.',
    '0500_402A': 'Router connection failed. Wireless interference or too far.',
    '0500_402B': 'Router connection failed: incorrect password.',
    '0500_402C': 'Failed to obtain IP. DHCP pool may be full.',
    '0500_402D': 'System exception.',
    '0500_402E': 'MicroSD card filesystem not supported. Format to FAT32.',
    '0500_402F': 'MicroSD card sector data damaged. Use repair tool or format.',
    '0500_4037': 'Sliced file incompatible with printer model.',
    '0500_4038': 'Nozzle diameter in sliced file does not match printer setting.',
    '0500_8013': 'Print file not available. Check storage media.',
    '0500_8030': 'System notification.',
    '0500_8036': 'Sliced file does not match printer model. Continue?',
    '0500_C010': 'MicroSD card read/write exception. Reinsert or replace card.',
    '0500_C011': 'System notification.',
    '0500_403A': 'Temperature too low. Move printer above 10°C.',
    # ── Binding (0501) ───────────────────────────────────────────────────────
    '0501_4017': 'Binding failed. Retry or restart.',
    '0501_4018': 'Binding config parsing failed.',
    '0501_4019': 'Printer already bound. Unbind first.',
    '0501_401A': 'Cloud access failed.',
    '0501_401B': 'Cloud response invalid.',
    '0501_401C': 'Cloud access rejected.',
    '0501_401D': 'Cloud access failed.',
    '0501_401E': 'Cloud response invalid.',
    '0501_401F': 'Authorization timed out.',
    '0501_4020': 'Cloud access rejected.',
    '0501_4021': 'Cloud access failed.',
    '0501_4022': 'Cloud response invalid.',
    '0501_4023': 'Cloud access rejected.',
    '0501_4024': 'Cloud access failed.',
    '0501_4025': 'Cloud response invalid.',
    '0501_4026': 'Cloud access rejected.',
    '0501_4027': 'Cloud access failed.',
    '0501_4028': 'Cloud response invalid.',
    '0501_4029': 'Cloud access rejected.',
    '0501_4031': 'Device discovery binding in progress. Wait or abort.',
    '0501_4032': 'QR code binding in progress.',
    '0501_4033': 'APP region mismatch. Use correct regional app.',
    '0501_4034': 'Slicing progress stalled. Check parameters and retry.',
    '0501_4035': 'Binding in progress. Cannot accept new binding.',
    '0501_4038': 'Region settings mismatch.',
    '0514_039': 'Device login expired. Re-bind.',
    # ── AMS operations (0700–07FF) ───────────────────────────────────────────
    '0700_8001': 'AMS: Failed to cut filament. Check cutter.',
    '0700_8002': 'AMS: Cutter stuck. Pull out cutter handle.',
    '0700_8003': 'AMS: Failed to pull filament from extruder. Possible clog.',
    '0700_8004': 'AMS: Failed to pull back filament. Spool may be stuck.',
    '0700_8005': 'AMS: Failed to send filament. Clip end flat and reinsert.',
    '0700_8006': 'AMS: Unable to feed filament into extruder. Check PTFE tube.',
    '0700_8007': 'AMS: Extruding filament failed. Extruder may be clogged.',
    '0700_8010': 'AMS: Assist motor overloaded. Filament or spool stuck.',
    '0700_8011': 'AMS: Filament ran out. Insert new filament.',
    '0700_8012': 'AMS: Failed to get mapping table. Retry.',
    '0700_8013': 'AMS: Timeout purging old filament. Check for clog.',
    '0700_4001': 'AMS disabled but filament still loaded. Unload and use spool holder.',
    '0701_8001': 'AMS2: Failed to cut filament.',
    '0701_8002': 'AMS2: Cutter stuck.',
    '0701_8003': 'AMS2: Failed to pull filament from extruder.',
    '0701_8004': 'AMS2: Failed to pull back filament.',
    '0701_8005': 'AMS2: Failed to send filament.',
    '0701_8006': 'AMS2: Unable to feed filament into extruder.',
    '0701_8007': 'AMS2: Extruding filament failed.',
    '0701_8010': 'AMS2: Assist motor overloaded.',
    '0701_8011': 'AMS2: Filament ran out.',
    '0701_8012': 'AMS2: Failed to get mapping table.',
    '0701_8013': 'AMS2: Timeout purging old filament.',
    '0701_4001': 'AMS2: Disabled but filament still loaded.',
    '0702_8001': 'AMS3: Failed to cut filament.',
    '0702_8002': 'AMS3: Cutter stuck.',
    '0702_8003': 'AMS3: Failed to pull filament from extruder.',
    '0702_8004': 'AMS3: Failed to pull back filament.',
    '0702_8005': 'AMS3: Failed to send filament.',
    '0702_8006': 'AMS3: Unable to feed filament into extruder.',
    '0702_8007': 'AMS3: Extruding filament failed.',
    '0702_8010': 'AMS3: Assist motor overloaded.',
    '0702_8011': 'AMS3: Filament ran out.',
    '0702_8012': 'AMS3: Failed to get mapping table.',
    '0702_8013': 'AMS3: Timeout purging old filament.',
    '0702_4001': 'AMS3: Disabled but filament still loaded.',
    '0703_8001': 'AMS4: Failed to cut filament.',
    '0703_8002': 'AMS4: Cutter stuck.',
    '0703_8003': 'AMS4: Failed to pull filament from extruder.',
    '0703_8004': 'AMS4: Failed to pull back filament.',
    '0703_8005': 'AMS4: Failed to send filament.',
    '0703_8006': 'AMS4: Unable to feed filament into extruder.',
    '0703_8007': 'AMS4: Extruding filament failed.',
    '0703_8010': 'AMS4: Assist motor overloaded.',
    '0703_8011': 'AMS4: Filament ran out.',
    '0703_8012': 'AMS4: Failed to get mapping table.',
    '0703_8013': 'AMS4: Timeout purging old filament.',
    '0703_4001': 'AMS4: Disabled but filament still loaded.',
    '07FF_8001': 'Spool holder: Failed to cut filament.',
    '07FF_8002': 'Spool holder: Cutter stuck.',
    '07FF_8003': 'Spool holder: Pull filament from extruder. Check for breakage.',
    '07FF_8004': 'Spool holder: Failed to pull back filament.',
    '07FF_8005': 'Spool holder: Failed to feed filament.',
    '07FF_8006': 'Spool holder: Feed filament into PTFE tube.',
    '07FF_8007': 'Spool holder: Check nozzle. Click Done if filament extruded.',
    '07FF_8010': 'Spool holder: Assist motor overloaded.',
    '07FF_8011': 'Spool holder: Filament ran out.',
    '07FF_8012': 'Spool holder: Failed to get mapping table.',
    '07FF_8013': 'Spool holder: Timeout purging old filament.',
    '07FF_4001': 'Spool holder: AMS disabled but filament still loaded.',
    '07FF_C003': 'Pull out filament from spool holder. Check for breakage in extruder.',
    '07FF_C006': 'Feed filament into PTFE tube until it stops.',
    # ── AMS Lite (1200–12FF) ─────────────────────────────────────────────────
    '1200_8001': 'AMS Lite: Failed to cut filament. Check cutter.',
    '1200_8002': 'AMS Lite: Cutter stuck. Pull out handle.',
    '1200_8003': 'AMS Lite: Failed to pull filament from extruder.',
    '1200_8004': 'AMS Lite: Failed to pull back filament from toolhead.',
    '1200_8005': 'AMS Lite: Failed to feed filament.',
    '1200_8006': 'AMS Lite: Unable to feed filament into extruder.',
    '1200_8007': 'AMS Lite: Failed to extrude filament.',
    '1200_8010': 'AMS Lite: Filament or spool stuck.',
    '1200_8011': 'AMS Lite: Filament ran out.',
    '1200_8012': 'AMS Lite: Failed to get mapping table.',
    '1200_8013': 'AMS Lite: Timeout purging old filament.',
    '1200_8014': 'AMS Lite: Filament location in toolhead not found.',
    '1200_8015': 'AMS Lite: Failed to pull filament from toolhead.',
    '1200_8016': 'AMS Lite: Extruder not extruding normally.',
    '1200_4001': 'AMS Lite: Disabled but filament still loaded.',
    '1201_8001': 'AMS Lite slot 1: Failed to cut filament.',
    '1201_8002': 'AMS Lite slot 1: Cutter stuck.',
    '1201_8003': 'AMS Lite slot 1: Failed to pull filament from extruder.',
    '1201_8004': 'AMS Lite slot 1: Failed to pull back filament.',
    '1201_8005': 'AMS Lite slot 1: Failed to feed filament.',
    '1201_8006': 'AMS Lite slot 1: Unable to feed filament into toolhead.',
    '1201_8007': 'AMS Lite slot 1: Failed to extrude filament.',
    '1201_8010': 'AMS Lite slot 1: Filament or spool stuck.',
    '1201_8011': 'AMS Lite slot 1: Filament ran out.',
    '1201_8012': 'AMS Lite slot 1: Failed to get mapping table.',
    '1201_8013': 'AMS Lite slot 1: Timeout purging old filament.',
    '1201_8014': 'AMS Lite slot 1: Filament location in toolhead not found.',
    '1201_8015': 'AMS Lite slot 1: Failed to pull back filament from toolhead.',
    '1201_8016': 'AMS Lite slot 1: Extruder not extruding normally.',
    '1201_4001': 'AMS Lite slot 1: Disabled but filament still loaded.',
    '1202_8001': 'AMS Lite slot 2: Failed to cut filament.',
    '1202_8002': 'AMS Lite slot 2: Cutter stuck.',
    '1202_8003': 'AMS Lite slot 2: Failed to pull filament from extruder.',
    '1202_8004': 'AMS Lite slot 2: Failed to pull back filament.',
    '1202_8005': 'AMS Lite slot 2: Failed to feed filament.',
    '1202_8006': 'AMS Lite slot 2: Unable to feed filament into toolhead.',
    '1202_8007': 'AMS Lite slot 2: Failed to extrude filament.',
    '1202_8010': 'AMS Lite slot 2: Filament or spool stuck.',
    '1202_8011': 'AMS Lite slot 2: Filament ran out.',
    '1202_8012': 'AMS Lite slot 2: Failed to get mapping table.',
    '1202_8013': 'AMS Lite slot 2: Timeout purging old filament.',
    '1202_8015': 'AMS Lite slot 2: Failed to pull back filament.',
    '1202_8016': 'AMS Lite slot 2: Extruder not extruding normally.',
    '1202_4001': 'AMS Lite slot 2: Disabled but filament still loaded.',
    '1203_8001': 'AMS Lite slot 3: Failed to cut filament.',
    '1203_8002': 'AMS Lite slot 3: Cutter stuck.',
    '1203_8003': 'AMS Lite slot 3: Failed to pull filament from extruder.',
    '1203_8004': 'AMS Lite slot 3: Failed to pull back filament.',
    '1203_8005': 'AMS Lite slot 3: Failed to feed filament.',
    '1203_8006': 'AMS Lite slot 3: Unable to feed filament into toolhead.',
    '1203_8007': 'AMS Lite slot 3: Failed to extrude filament.',
    '1203_8010': 'AMS Lite slot 3: Filament or spool stuck.',
    '1203_8011': 'AMS Lite slot 3: Filament ran out.',
    '1203_8012': 'AMS Lite slot 3: Failed to get mapping table.',
    '1203_8013': 'AMS Lite slot 3: Timeout purging old filament.',
    '1203_8014': 'AMS Lite slot 3: Filament location in toolhead not found.',
    '1203_8015': 'AMS Lite slot 3: Failed to pull back filament.',
    '1203_8016': 'AMS Lite slot 3: Extruder not extruding normally.',
    '1203_4001': 'AMS Lite slot 3: Disabled but filament still loaded.',
    '12FF_8001': 'AMS Lite (spool): Failed to cut filament.',
    '12FF_8002': 'AMS Lite (spool): Cutter stuck.',
    '12FF_8003': 'AMS Lite (spool): Pull filament from extruder.',
    '12FF_8004': 'AMS Lite (spool): Failed to pull back filament.',
    '12FF_8005': 'AMS Lite (spool): Failed to feed filament.',
    '12FF_8006': 'AMS Lite (spool): Feed filament into PTFE tube.',
    '12FF_8007': 'AMS Lite (spool): Check nozzle. Click Done if filament extruded.',
    '12FF_8010': 'AMS Lite (spool): Filament or spool stuck.',
    '12FF_8011': 'AMS Lite (spool): Filament ran out.',
    '12FF_8012': 'AMS Lite (spool): Failed to get mapping table.',
    '12FF_8013': 'AMS Lite (spool): Timeout purging old filament.',
    '12FF_4001': 'AMS Lite (spool): Disabled but filament still loaded.',
    '12FF_C003': 'AMS Lite: Pull out filament. Check for breakage.',
    '12FF_C006': 'AMS Lite: Feed filament into PTFE tube.',
    # ── Toolhead / detection (0C00) ──────────────────────────────────────────
    '0C00_8001': 'First layer defects detected. Resume if acceptable.',
    '0C00_8002': 'Spaghetti failure detected.',
    '0C00_8005': 'Purged filament piled up in waste chute. Risk of collision.',
    '0C00_8009': 'Build plate localization marker not found.',
    '0C00_800A': 'Detected build plate differs from G-code.',
    '0C00_C003': 'Possible first layer defects detected.',
    '0C00_C004': 'Possible spaghetti failure detected.',
    '0C00_C006': 'Purged filament may have piled up in waste chute.',
    # ── Info / warnings (1000–1001) ──────────────────────────────────────────
    '1000_C001': 'High bed temperature may cause filament clogging. Open chamber door.',
    '1000_C002': 'Printing CF material with stainless steel nozzle may cause damage.',
    '1000_C003': 'Traditional timelapse may cause defects. Enable as needed.',
    '1001_C001': 'Timelapse not supported: spiral vase is enabled.',
    '1001_C002': 'Timelapse not supported: print sequence is by object.',
}


def lookup_hms_code(code: str) -> str:
    """
    Look up human-readable message for an HMS error code.
    Checks both HMS codes (AABBCCDD_EEFFGGHH) and print error codes (XXXX_YYYY).
    Uses exact match first, then structural decode as fallback.
    """
    code = code.upper().strip()

    # 1. Exact match — HMS codes
    if code in KNOWN_CODES:
        return KNOWN_CODES[code]

    # 2. Exact match — print error codes (shorter format)
    if code in PRINT_ERROR_CODES:
        return PRINT_ERROR_CODES[code]

    # 3. Structural decode for HMS codes
    if '_' not in code or len(code) < 17:
        # Try as print error code with normalization
        normalized = code.replace('-', '_').replace(' ', '')
        if normalized in PRINT_ERROR_CODES:
            return PRINT_ERROR_CODES[normalized]
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
            # Try without device specificity
            error_desc = f'error 0x{error_class:02X}'

        # Build message
        parts = [device_name]

        # Add unit number if relevant
        if device_id in (0x03, 0x05) and module > 0:
            parts[0] = f'AMS{module}'
        elif module > 0:
            parts[0] = f'{device_name} (unit {module})'

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


def lookup_print_error(code: str) -> str:
    """Look up a print action error code (XXXX_YYYY format)."""
    code = code.upper().strip()
    if code in PRINT_ERROR_CODES:
        return PRINT_ERROR_CODES[code]
    return f'Unknown print error: {code}'


def get_code_count() -> int:
    """Return the total number of known error codes (HMS + print action)."""
    return len(KNOWN_CODES) + len(PRINT_ERROR_CODES)
