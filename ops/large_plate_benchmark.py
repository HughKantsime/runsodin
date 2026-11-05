"""Synthetic multicolour H2D plates, no customer data or hardware access.

Run the opt-in contract with ODIN_LARGE_PLATE_BENCHMARK=154 (or 256).
"""
import random
import zipfile


def make_plate(path, mib):
    rng = random.Random(4242)
    # Real command shapes, invented coordinates/feed/extrusion values.
    block = ''.join(f'G1 X{rng.uniform(5,300):.3f} Y{rng.uniform(5,300):.3f} E{rng.uniform(.01,2):.5f} F{rng.randrange(1000,16000)}\n'
                    for _ in range(8192)).encode()
    start = (b'; filament_type = PETG;PETG\n; CONFIG_BLOCK_START\n'
             b'; physical_extruder_map = 1,0\n; filament_map = 2,2\n; CONFIG_BLOCK_END\n'
             b'M620 S0A\nT0\nM621 S0A\nM620 S1A\nT1\nM621 S1A\n')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as package:
        package.writestr('Metadata/project_settings.config',
            '{"printer_model":"Bambu Lab H2D","filament_type":["PETG","PETG"],"nozzle_diameter":["0.4","0.4"],"physical_extruder_map":[1,0]}')
        package.writestr('Metadata/slice_info.config',
            '<config><plate><metadata key="index" value="1"/><metadata key="filament_maps" value="2 2"/>'
            '<filament id="1" type="PETG" used_g="10"/><filament id="2" type="PETG" used_g="12"/></plate></config>')
        package.writestr('Metadata/plate_1.json', '{"filament_ids":[0,1]}')
        remaining = mib * 1024**2 - len(start)
        with package.open('Metadata/plate_1.gcode', 'w') as code:
            code.write(start)
            while remaining >= len(block):
                code.write(block)
                remaining -= len(block)
            # Finish with complete movement lines, then a bounded comment.
            lines = block[:remaining].rsplit(b'\n', 1)[0] + b'\n' if remaining else b''
            if len(lines) <= remaining:
                code.write(lines)
                remaining -= len(lines)
            if remaining:
                code.write(b'\n' if remaining == 1 else b';' + b'x' * (remaining - 2) + b'\n')
