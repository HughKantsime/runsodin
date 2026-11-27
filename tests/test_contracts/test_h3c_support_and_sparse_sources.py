"""H3C support filament labels, unused filament 1 startup queries, and M620.13."""
import hashlib
import io
import json
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ams_mapping import validate_for_printer
from core.ams_source_inspection import inspect_ams_sources, inspect_single_source_ams
from core.h3c_plate import RackPlateError
from core.print_payload import (
    MappingMaterialMismatch,
    canonical_material,
    material_match,
    validate_mapping,
)
from modules.jobs.scheduler_materials import loaded_materials, needed_materials
from modules.printers.models import Printer
from modules.printers.nozzle_capability import installed_nozzles
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


FIXTURES = Path(__file__).parents[1] / 'fixtures/telemetry'
STUDIO = FIXTURES / 'h3c-studio-sent.3mf'
X1C = FIXTURES / 'x1c-pla-source-sanitized.3mf'


def _build_h3c_support_slice(
    *,
    summary_type='PLA-S',
    project_type='PLA',
    support_flag='1',
    include_support_field=True,
    startup_queries=('M620 T0', 'M620 T3'),
    source5_extra=(';prime_tower_interface', 'M620.13 W0 L20 T240 R0.0', ';prime_tower_interface'),
    after_first_block=(),
):
    """Copy committed h3c-studio-sent.3mf and edit text members to a 3-filament slice (sources 3, 4, 6)."""
    out = io.BytesIO()
    used_sources = (2, 3, 5)  # 0-based indices for filaments 3, 4, 6 (filament 1 unused)
    groups = {2: 2, 3: 1, 5: 0}
    colors = {2: '#FFFFFF', 3: '#000000', 5: '#C0C0C0'}

    with ZipFile(STUDIO) as src, ZipFile(out, 'w') as dst:
        gcode_bytes = None
        for member in src.namelist():
            data = src.read(member)
            if member == 'Metadata/slice_info.config':
                root = ElementTree.fromstring(data)
                plate = root.find('plate')
                for tag in ('filament', 'nozzle'):
                    for node in list(plate.findall(tag)):
                        plate.remove(node)
                for idx in used_sources:
                    mat = summary_type if idx == 5 else 'PLA'
                    ElementTree.SubElement(
                        plate, 'filament',
                        id=str(idx + 1), type=mat, color=colors[idx],
                        used_m='5.00', used_g='15.00', group_id=str(groups[idx]),
                        nozzle_diameter='0.40', volume_type='Standard',
                        used_for_object='true', used_for_support='true' if idx == 5 else 'false',
                    )
                for group in sorted(groups.values()):
                    ElementTree.SubElement(
                        plate, 'nozzle',
                        id=str(group), extruder_id='1', nozzle_diameter='0.4', volume_type='Standard',
                    )
                data = ElementTree.tostring(root, encoding='utf-8', xml_declaration=True)
            elif member == 'Metadata/project_settings.config':
                settings = json.loads(data)
                types = ['PLA'] * 8
                types[5] = project_type
                settings['filament_type'] = types
                if include_support_field:
                    flags = ['0'] * 8
                    flags[5] = support_flag
                    settings['filament_is_support'] = flags
                else:
                    settings.pop('filament_is_support', None)
                data = json.dumps(settings).encode('utf-8')
            elif member == 'Metadata/plate_1.json':
                plate_json = json.loads(data)
                plate_json['filament_ids'] = list(used_sources)
                plate_json['filament_colors'] = [colors[idx] for idx in used_sources]
                data = json.dumps(plate_json).encode('utf-8')
            elif member == 'Metadata/plate_1.gcode':
                header_types = ';'.join('PLA' if i != 5 else project_type for i in range(8))
                lines = [
                    '; extruder_nozzle_count = 9',
                    f'; filament_type = {header_types}',
                    '; printer_model = Bambu Lab H3C',
                    'M620.29 P ; print nozzle info',
                    'M620 M ; enable remap',
                    *startup_queries,
                    'M620.35 S1',
                    'M620.10 A0 F623.628 H0.4 T240 P220 S1',
                    'M620.10 A1 F623.628 H0.4 T240 P220 S1 V10.3938',
                    'M620.11 P0 I2 E0',
                    'M620.11 K0 I2 R0',
                    'M620 S2A',
                    'T2',
                    'M621 S2A',
                    'M620.6 I2 H-1 W1',
                    *after_first_block,
                    'M620 S3A',
                    'T3',
                    'M621 S3A',
                    'M620.6 I3 H-1 W1',
                    'M620 S5A',
                    *source5_extra,
                    'T5',
                    'M621 S5A',
                    'M620.6 I5 H-1 W1',
                    'M620 S65535',
                    'T65535',
                    'M621 S65535',
                ]
                gcode_bytes = ('\n'.join(lines) + '\n').encode('utf-8')
                data = gcode_bytes
            elif member == 'Metadata/plate_1.gcode.md5':
                continue
            dst.writestr(member, data)
        assert gcode_bytes is not None
        dst.writestr('Metadata/plate_1.gcode.md5', hashlib.md5(gcode_bytes).hexdigest().upper())
    return out.getvalue()


def _build_x1c_support_slice(*, summary_type='PLA-S', support_flag='1', header_type='PLA'):
    """Copy committed x1c-pla-source-sanitized.3mf and label filament 1 as a support filament."""
    out = io.BytesIO()
    with ZipFile(X1C) as src, ZipFile(out, 'w') as dst:
        for member in src.namelist():
            data = src.read(member)
            if member == 'Metadata/slice_info.config':
                assert b'type="PLA"' in data
                data = data.replace(b'type="PLA"', f'type="{summary_type}"'.encode('utf-8'), 1)
            elif member == 'Metadata/project_settings.config':
                settings = json.loads(data)
                settings['filament_type'] = ['PLA']
                if support_flag is None:
                    settings.pop('filament_is_support', None)
                else:
                    settings['filament_is_support'] = [support_flag]
                data = json.dumps(settings).encode('utf-8')
            elif member == 'Metadata/plate_1.gcode':
                assert b'; filament_type = PLA' in data
                data = data.replace(b'; filament_type = PLA', f'; filament_type = {header_type}'.encode('utf-8'), 1)
            dst.writestr(member, data)
    return out.getvalue()


@pytest.mark.parametrize('support_flag', ['1', 1])
def test_h3c_support_filament_sparse_sources_and_m620_13_verify(support_flag):
    payload = _build_h3c_support_slice(support_flag=support_flag)
    verified = inspect_ams_sources(payload)
    assert verified.plate_index == 0
    assert [
        (s.source_index, s.material, s.nozzle_diameter, s.volume_type, s.group_id)
        for s in verified.sources
    ] == [
        (2, 'PLA', 0.4, 'Standard', 2),
        (3, 'PLA', 0.4, 'Standard', 1),
        (5, 'PLA-S', 0.4, 'Standard', 0),
    ]


def test_h3c_accepts_t8_query_before_first_source_block():
    payload = _build_h3c_support_slice(startup_queries=('M620 T8',))
    assert [source.source_index for source in inspect_ams_sources(payload).sources] == [2, 3, 5]


def test_validate_for_printer_accepts_h3c_support_slot_and_refuses_plain_pla(review_db, tmp_path):
    engine = review_db.get_bind()
    report = json.loads((FIXTURES / 'h3c-ams3h-loaded-2026-11-21.json').read_bytes())['message']['print']
    tools = installed_nozzles(report, include_rack=True)
    path = tmp_path / 'h3c-support-sparse.3mf'
    path.write_bytes(_build_h3c_support_slice())
    filaments = [
        {'slot': 3, 'type': 'PLA', 'color': '#FFFFFF'},
        {'slot': 4, 'type': 'PLA', 'color': '#000000'},
        {'slot': 6, 'type': 'PLA-S', 'color': '#C0C0C0'},
    ]
    filaments_json = json.dumps(filaments)
    mapping = [
        {'source_slot': 3, 'target_slot': 3},
        {'source_slot': 4, 'target_slot': 4},
        {'source_slot': 6, 'target_slot': 6},
    ]
    actor = {'id': 1, 'role': 'admin', 'group_id': None}

    with Session(engine) as db:
        db.add(Printer(id=911, name='Support rack', model='H3C', api_type='bambu', installed_nozzles=tools))
        db.flush()
        for slot, material in ((3, 'PLA'), (4, 'PLA'), (6, 'PLA_SUPPORT')):
            db.execute(
                text('INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) '
                     'VALUES(911,:slot,:material,:color)'),
                {'slot': slot, 'material': material, 'color': 'FFFFFF'},
            )
        db.commit()

        assert validate_for_printer(db, mapping, filaments_json, 911, actor, stored_path=path) == [
            -1, -1, 2, 3, -1, 5
        ]

        # Position 6 holding plain PLA must still be refused by the material rule.
        db.execute(text("UPDATE filament_slots SET filament_type='PLA' WHERE printer_id=911 AND slot_number=6"))
        db.commit()
        with pytest.raises(MappingMaterialMismatch) as exc:
            validate_for_printer(db, mapping, filaments_json, 911, actor, stored_path=path)
        assert exc.value.status_code == 400
        assert exc.value.detail == 'Filament 6 needs PLA-S. Position 6 holds PLA.'


@pytest.mark.parametrize('kwargs,error_type,match', [
    (dict(support_flag='0'), ValueError, '^Unverifiable source mapping$'),
    (dict(include_support_field=False), ValueError, '^Unverifiable source mapping$'),
    (dict(support_flag=True), ValueError, '^Unverifiable source mapping$'),
    (dict(summary_type='PETG-S', project_type='PETG', support_flag='1'), ValueError, '^Unverifiable source mapping$'),
    (dict(summary_type='PLA-S', project_type='PETG', support_flag='1'), ValueError, '^Unverifiable source mapping$'),
    (dict(summary_type='PLA-CF', project_type='PLA', support_flag='1'), ValueError, '^Unverifiable source mapping$'),
    (dict(after_first_block=('M620 T0',)), RackPlateError, 'unsupported tool change'),
    (dict(startup_queries=('M620 T9',)), RackPlateError, 'unsupported tool change'),
    (dict(source5_extra=('M620.13 S3A',)), RackPlateError, 'unsupported tool change'),
    (dict(source5_extra=('M620.13 W0 L20 T240 R0.0 S3A',)), RackPlateError, 'unsupported tool change'),
    (dict(source5_extra=('M620.99',)), RackPlateError, 'unsupported tool change'),
])
def test_h3c_false_refusal_boundaries_stay_closed(kwargs, error_type, match):
    payload = _build_h3c_support_slice(**kwargs)
    with pytest.raises(error_type, match=match):
        inspect_ams_sources(payload)


def test_non_h3c_bambu_support_filament_verifies_with_slice_summary_label():
    payload = _build_x1c_support_slice(summary_type='PLA-S', support_flag='1', header_type='PLA')
    single = inspect_single_source_ams(payload)
    assert (single.plate_index, single.source_index, single.material) == (0, 0, 'PLA-S')
    multi = inspect_ams_sources(payload)
    assert [(s.source_index, s.material, s.extruder) for s in multi.sources] == [(0, 'PLA-S', None)]

    # Support flag off or absent, or gcode header disagreeing with project_settings, stays refused.
    for bad in (
        dict(summary_type='PLA-S', support_flag='0', header_type='PLA'),
        dict(summary_type='PLA-S', support_flag=None, header_type='PLA'),
        dict(summary_type='PLA-S', support_flag='1', header_type='PLA-S'),
    ):
        with pytest.raises(ValueError, match='^Unverifiable source mapping$'):
            inspect_ams_sources(_build_x1c_support_slice(**bad))


def test_scheduler_materials_unchanged_for_pla_support_and_pla_s():
    # Scheduler keeps PLA_SUPPORT unknown (never excludes) and reads PLA-S files as in v2.7.1.
    assert canonical_material('PLA_SUPPORT') is None
    assert material_match('PLA-S', 'PLA_SUPPORT') is None
    assert loaded_materials(['PLA', 'PLA_SUPPORT']) is None
    assert loaded_materials(['PLA_SUPPORT']) is None
    assert needed_materials(json.dumps([{'slot': 1, 'type': 'PLA-S'}])) == ('PLA-S',)
    assert needed_materials(
        json.dumps([{'slot': 3, 'type': 'PLA'}, {'slot': 4, 'type': 'PLA'}, {'slot': 6, 'type': 'PLA-S'}])
    ) == ('PLA', 'PLA-S')
    assert needed_materials(None, 'PLA_SUPPORT') is None


def test_non_h3c_bambu_gate_accepts_pla_s_on_pla_support_and_refuses_plain_pla():
    filaments_json = json.dumps([{'slot': 1, 'type': 'PLA-S', 'color': '#C0C0C0'}])
    mapping = [{'source_slot': 1, 'target_slot': 2}]

    assert validate_mapping(mapping, filaments_json, {2: 'PLA_SUPPORT'}, 'bambu') == [1]
    assert validate_mapping(mapping, filaments_json, {2: ' pla_support '}, 'bambu') == [1]

    with pytest.raises(MappingMaterialMismatch) as exc:
        validate_mapping(mapping, filaments_json, {2: 'PLA'}, 'bambu')
    assert exc.value.status_code == 400
    assert exc.value.detail == 'Selected tray material does not match the source filament'
    assert exc.value.target_slot == 2
    assert exc.value.confirmed is True

    # Plain PLA source is still refused on a PLA_SUPPORT slot; source side does not translate PLA_SUPPORT.
    with pytest.raises(MappingMaterialMismatch) as exc:
        validate_mapping(mapping, json.dumps([{'slot': 1, 'type': 'PLA'}]), {2: 'PLA_SUPPORT'}, 'bambu')
    assert exc.value.confirmed is True

    with pytest.raises(MappingMaterialMismatch) as exc:
        validate_mapping(mapping, json.dumps([{'slot': 1, 'type': 'PLA_SUPPORT'}]), {2: 'PLA_SUPPORT'}, 'bambu')
    assert exc.value.confirmed is False
