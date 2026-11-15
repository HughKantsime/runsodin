"""Read-only mapping presentation uses the same catalogue labels as slot editing."""
import json

import pytest
from fastapi import HTTPException
from core.ams_mapping import preview, validate_for_printer
from modules.inventory.models import FilamentLibrary
from modules.printers.models import FilamentSlot
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

from tests.test_contracts.test_archive_reprint_behavior import reprint_client  # noqa: F401

ACTOR = {'id': 3, 'role': 'admin', 'group_id': None}


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
