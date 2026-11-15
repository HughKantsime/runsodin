"""Read-only mapping presentation uses the same catalogue labels as slot editing."""
import json
import ast
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from core.ams_mapping import preview, validate_for_printer
from modules.inventory.models import FilamentLibrary
from modules.printers.models import FilamentSlot
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

from tests.test_contracts.test_archive_reprint_behavior import reprint_client  # noqa: F401

ACTOR = {'id': 3, 'role': 'admin', 'group_id': None}


def parent_module(path, *, function=None, globals_from=None):
    """Execute the pinned pre-screenfix implementation, never the current function."""
    root = Path(__file__).resolve().parents[2]
    source = subprocess.check_output(['git', 'show', f'27d98a59:{path}'], cwd=root, text=True)
    tree = ast.parse(source)
    if function:
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function]
        assert len(tree.body) == 1
        # The route decorator is irrelevant to a direct comparison with the same session.
        tree.body[0].decorator_list = []
    module = ModuleType('pre_screenfix')
    if globals_from:
        module.__dict__.update(globals_from)
    exec(compile(tree, f'27d98a59:{path}', 'exec'), module.__dict__)
    return module


def wire(value):
    return json.dumps(value, separators=(',', ':')).encode()


@contextmanager
def read_only(db):
    """Fail on a real ORM flush or any SQL write, including writes later rolled back."""
    statements, flushes = [], []
    def sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    def flush(*_args):
        flushes.append(True)
    engine = db.get_bind()
    assert not db.dirty and not db.new and not db.deleted
    event.listen(engine, 'before_cursor_execute', sql)
    event.listen(db, 'before_flush', flush)
    try:
        yield
        assert not flushes
        assert statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements)
        assert not db.dirty and not db.new and not db.deleted
    finally:
        event.remove(engine, 'before_cursor_execute', sql)
        event.remove(db, 'before_flush', flush)


def seed_labels(db):
    db.add(FilamentLibrary(brand='Synthetic', name='ASA-CF Invented Black', material='ASA-CF',
        color_hex='2B2D31', catalogue_code='SCREENFIX-PARITY'))
    db.add_all([
        FilamentSlot(printer_id=9, slot_number=2, filament_type='empty'),
        FilamentSlot(printer_id=9, slot_number=3, filament_type='ASA_CF', color_hex='2B2D31'),
        FilamentSlot(printer_id=9, slot_number=4, filament_type='PETG', do_not_feed=True),
    ])
    db.commit()


def outcome(call):
    try:
        return wire({'value': call()})
    except HTTPException as exc:
        return wire({'status': exc.status_code, 'detail': exc.detail, 'headers': exc.headers})


def test_preview_and_all_validation_outcomes_match_parent_without_writes(review_db):
    parent = parent_module('backend/core/ams_mapping.py')
    seed_labels(review_db)
    sources = [[], [{'slot': 1, 'type': 'PLA'}], [{'slot': 1, 'type': 'ASA-CF', 'color': '#2B2D31'}],
               [{'slot': 1, 'type': 'PLA'}, {'slot': 3, 'type': 'PETG'}]]
    mappings = [None, [], *([{'source_slot': 1, 'target_slot': slot}] for slot in (1, 2, 3, 4, 5, 17)),
                [{'source_slot': True, 'target_slot': 1}], [{'source_slot': 0, 'target_slot': 1}],
                [{'source_slot': 1, 'target_slot': 1}, {'source_slot': 3, 'target_slot': 4}]]
    with read_only(review_db):
        for source in sources:
            encoded = json.dumps(source)
            def current_preview():
                actual = preview(review_db, encoded, 9, ACTOR)
                assert all('catalogue_name' in slot for slot in actual['available'])
                assert next(slot for slot in actual['available'] if slot['slot'] == 3)['catalogue_name'] == 'ASA-CF Invented Black'
                return {**actual, 'available': [{k: v for k, v in slot.items() if k != 'catalogue_name'}
                                                for slot in actual['available']]}
            assert outcome(current_preview) == outcome(lambda: parent.preview(review_db, encoded, 9, ACTOR))
            for mapping in mappings:
                for check_material in (True, False):
                    assert outcome(lambda: validate_for_printer(review_db, mapping, encoded, 9, ACTOR,
                                                                check_material=check_material)) == outcome(
                        lambda: parent.validate_for_printer(review_db, mapping, encoded, 9, ACTOR,
                                                           check_material=check_material))


def test_archive_preview_preserves_parent_fields_without_writes(reprint_client, review_db):
    from modules.archives.routes import archives_crud
    parent = parent_module('backend/modules/archives/routes/archives_crud.py', function='ams_preview',
                           globals_from=vars(archives_crud))
    seed_labels(review_db)
    review_db.execute(text('UPDATE print_files SET filaments_json=:source WHERE id=101'),
                      {'source': json.dumps([{'slot': 1, 'type': 'ASA-CF', 'color': '#2B2D31'}])})
    review_db.commit()
    with read_only(review_db):
        before = parent.ams_preview(101, 9, ACTOR, review_db)
        actual = archives_crud.ams_preview(101, 9, ACTOR, review_db)
        assert next(slot for slot in actual['available'] if slot['slot'] == 3)['catalogue_name'] == 'ASA-CF Invented Black'
        # Archive previews also expose the raw empty-state discriminator added in round 3.
        # Neither display field participates in suggestions; every pre-existing byte is preserved.
        assert next(slot for slot in actual['available'] if slot['slot'] == 2)['filament_type'] == 'empty'
        without_labels = {**actual, 'available': [{k: v for k, v in slot.items()
                                                 if k not in ('catalogue_name', 'filament_type')}
                                                for slot in actual['available']]}
        assert wire(without_labels) == wire(before)


def test_mapping_preview_includes_catalogue_name_without_changing_raw_material(review_db):
    review_db.add(FilamentLibrary(brand='Synthetic', name='ASA-CF Invented Black', material='ASA-CF',
        color_hex='2B2D31', catalogue_code='SCREENFIX-TEST'))
    review_db.add(FilamentSlot(printer_id=9, slot_number=4, filament_type='ASA_CF', color_hex='2B2D31'))
    review_db.commit()
    result = preview(review_db, json.dumps([{'slot': 1, 'type': 'ASA-CF'}]), 9, ACTOR)
    slot = next(s for s in result['available'] if s['slot'] == 4)
    assert slot['catalogue_name'] == 'ASA-CF Invented Black'
    assert slot['material'] == 'ASA_CF'
    assert not review_db.dirty


def test_empty_preview_slot_remains_empty_and_material_validation_refuses_it(review_db):
    review_db.add(FilamentSlot(printer_id=9, slot_number=4, filament_type='empty'))
    review_db.commit()
    filaments = json.dumps([{'slot': 1, 'type': 'PLA'}])
    result = preview(review_db, filaments, 9, ACTOR)
    empty = next(s for s in result['available'] if s['slot'] == 4)
    assert empty['material'] == 'empty'
    assert not any(s['target_slot'] == 4 for s in result['suggested_mapping'])
    with pytest.raises(HTTPException, match='Selected tray material does not match'):
        validate_for_printer(review_db, [{'source_slot': 1, 'target_slot': 4}], filaments, 9, ACTOR)


def test_archive_mapping_exposes_catalogue_and_explicit_empty_state(reprint_client, review_db):
    client, _, _ = reprint_client
    review_db.add(FilamentLibrary(brand='Synthetic', name='ASA-CF Invented Black', material='ASA-CF',
        color_hex='2B2D31', catalogue_code='SCREENFIX-ARCHIVE'))
    review_db.add_all([
        FilamentSlot(printer_id=9, slot_number=3, filament_type='ASA_CF', color_hex='2B2D31'),
        FilamentSlot(printer_id=9, slot_number=4, filament_type='empty'),
    ])
    review_db.commit()
    response = client.get('/archives/101/ams-preview?printer_id=9')
    assert response.status_code == 200
    slots = {s['slot']: s for s in response.json()['available']}
    assert slots[3]['catalogue_name'] == 'ASA-CF Invented Black'
    assert slots[4]['filament_type'] == 'empty'
    assert slots[4]['catalogue_name'] is None
