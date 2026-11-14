"""Review regressions; all catalogue entries are synthetic."""
import asyncio
import inspect
import json

import pytest
from sqlalchemy import event, text

from core.dependencies import get_current_user
from core.display_json import display_tray_state
from modules.inventory.models import FilamentLibrary, Spool
from modules.printers.models import FilamentSlot, Printer
from modules.printers.schemas import FilamentSlotResponse
from tests.test_contracts.test_bambu_catalogue import ADMIN, USER, payload, upload, slot_fixture
from tests.test_contracts.test_inventory_claim_acceptance import inventory_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.mark.parametrize('extra', [{'remaining': -1, 'pending': True}, {'remaining': 150, 'pending': True}, {'id': 42, 'pending': True}, {}])
def test_display_preserves_decodable_fields(review_db, extra):
    state = {'observed': {'type': 'PLA', 'color': '1234AB'}, **extra}
    raw = json.dumps(state)
    slot = FilamentSlot(tray_observation=raw)
    assert display_tray_state(raw) == state
    assert slot.remaining == state.get('remaining')
    values = FilamentSlotResponse._slot_spool_grams(slot)
    assert values['spool_change_pending'] == state.get('pending', False)
    assert values['observed_state'] == state.get('id')


@pytest.mark.parametrize('raw', ['{', '[]', 'null'])
def test_unknown_display_blocks_actions(review_db, raw):
    values = FilamentSlotResponse._slot_spool_grams(FilamentSlot(tray_observation=raw))
    assert values['spool_change_pending'] is True
    assert values['observed_state'] is None


def setup_choice(client, db, *, spool_material='PLA', slot_material='PLA', api='moonraker'):
    upload(client)
    db.get(FilamentLibrary, 990).material = spool_material
    db.commit()
    sid = client.post('/api/spools', json={'filament_id': 990}).json()['id']
    printer, slot = slot_fixture(db, api=api, material=slot_material)
    slot.assigned_spool_id = sid
    db.commit()
    target = db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-ORBIT').one()
    return printer, slot, db.get(Spool, sid), target


@pytest.mark.parametrize('route', ['patch', 'choice', 'helper'])
@pytest.mark.parametrize('mismatch', ['spool', 'slot', 'noncatalogue'])
def test_label_only_refusals(inventory_client, review_db, route, mismatch):
    from fastapi import HTTPException
    from modules.inventory.catalogue_slots import select_spool_filament
    printer, slot, spool, target = setup_choice(inventory_client, review_db,
        spool_material='PETG' if mismatch == 'spool' else 'PLA',
        slot_material='PETG' if mismatch == 'slot' else 'PLA')
    if mismatch == 'noncatalogue':
        target = FilamentLibrary(brand='Synthetic', name='Custom', material='PLA')
        review_db.add(target); review_db.commit()
    if route == 'helper':
        with pytest.raises(HTTPException) as exc:
            select_spool_filament(review_db, spool, target.id, ADMIN)
        assert exc.value.status_code == 409
    elif route == 'patch':
        response = inventory_client.patch(f'/api/spools/{spool.id}', json={'filament_id': target.id, 'notes': 'must not persist'})
        assert response.status_code == 409, response.text
    else:
        response = inventory_client.post(f'/api/printers/{printer.id}/slots/1/catalogue', json={'spool_id': spool.id, 'filament_id': target.id})
        assert response.status_code == 409, response.text
        if mismatch == 'spool': assert 'disagree' in response.json()['detail']
    review_db.expire_all()
    assert spool.filament_id == 990 and spool.notes is None
    assert review_db.execute(text("SELECT count(*) FROM audit_logs WHERE action='spool.catalogue_selected'")).scalar() == 0


@pytest.mark.parametrize('filament_id', [None, 990])
def test_unchanged_or_null_spool_choice_has_no_audit(inventory_client, review_db, filament_id):
    _, _, spool, _ = setup_choice(inventory_client, review_db)
    response = inventory_client.patch(f'/api/spools/{spool.id}', json={'filament_id': filament_id, 'notes': 'still editable'})
    assert response.status_code == 200, response.text
    review_db.refresh(spool)
    assert spool.filament_id == 990 and spool.notes == 'still editable'
    assert review_db.execute(text("SELECT count(*) FROM audit_logs WHERE action='spool.catalogue_selected'")).scalar() == 0


def test_repeated_slot_choice_is_noop(inventory_client, review_db):
    printer, _, spool, target = setup_choice(inventory_client, review_db)
    path = f'/api/printers/{printer.id}/slots/1/catalogue'
    body = {'spool_id': spool.id, 'filament_id': target.id}
    assert inventory_client.post(path, json=body).status_code == 200
    assert inventory_client.post(path, json=body).status_code == 200
    assert review_db.execute(text("SELECT count(*) FROM audit_logs WHERE action='spool.catalogue_selected'")).scalar() == 1


@pytest.mark.parametrize('raw', ['{', '[]', 'null'])
def test_unknown_tray_refuses_choice_with_plain_reason(inventory_client, review_db, raw):
    printer, slot, spool, target = setup_choice(inventory_client, review_db)
    slot.tray_observation = raw; review_db.commit()
    response = inventory_client.post(f'/api/printers/{printer.id}/slots/1/catalogue', json={'spool_id': spool.id, 'filament_id': target.id})
    assert response.status_code == 409
    assert 'spool-change state' in response.json()['detail']
    review_db.refresh(spool); assert spool.filament_id == 990


@pytest.mark.parametrize('path', ['/api/printers', '/api/printers/{id}', '/api/printers/{id}/slots'])
def test_get_routes_keep_slot_number_order(inventory_client, review_db, path):
    from core.base import FilamentType
    from modules.printers.routes_crud import router
    inventory_client.app.include_router(router, prefix='/api')
    printer, _ = slot_fixture(review_db, api='moonraker')
    for p in review_db.query(Printer).all():
        p.slot_count=4; p.tags=[]; p.timelapse_enabled=False; p.shared=False
    for number in [4, 2, 3]:
        review_db.add(FilamentSlot(printer_id=printer.id, slot_number=number, filament_type=FilamentType.PLA))
    review_db.commit()
    response = inventory_client.get(path.format(id=printer.id))
    assert response.status_code == 200, response.text
    data = response.json()
    if path == '/api/printers': data = next(p for p in data if p['id'] == printer.id)['filament_slots']
    elif not path.endswith('/slots'): data = data['filament_slots']
    assert [s['slot_number'] for s in data] == [1, 2, 3, 4]


def test_import_400_entries_is_bounded_and_off_event_loop(inventory_client, review_db):
    data = {'data': [{**payload()['data'][0], 'fila_color_code': f'ODIN-TEST-{i}'} for i in range(400)], 'total': 400}
    selects, event_loop_calls = [], []
    def check(conn, cursor, sql, *args):
        if 'filament_library' in sql.lower():
            try: asyncio.get_running_loop()
            except RuntimeError: pass
            else: event_loop_calls.append(sql)
            if sql.lstrip().upper().startswith('SELECT'): selects.append(sql)
    event.listen(review_db.bind, 'before_cursor_execute', check)
    try:
        for expected in ['created', 'updated']:
            selects.clear()
            response = upload(inventory_client, data)
            assert response.status_code == 200, response.text
            assert response.json()[expected] == 400
            assert len(selects) == 1
            assert event_loop_calls == []
    finally: event.remove(review_db.bind, 'before_cursor_execute', check)


def test_filament_crud_uses_sync_worker_routes():
    from modules.inventory.routes.drying_logs import update_filament, delete_filament
    assert not inspect.iscoroutinefunction(update_filament)
    assert not inspect.iscoroutinefunction(delete_filament)


def test_admin_lowercase_colour_choice_matches_display(inventory_client, review_db):
    from modules.inventory.catalogue_slots import enrich_catalogue_slots
    printer, slot, spool, target = setup_choice(inventory_client, review_db)
    assert inventory_client.patch(f'/api/filaments/{target.id}', json={'color_hex': '1234ab'}).status_code == 200
    enrich_catalogue_slots(review_db, [printer], ADMIN)
    assert target.id in [c['id'] for c in slot.catalogue_choices]
    response = inventory_client.post(f'/api/printers/{printer.id}/slots/1/catalogue', json={'spool_id': spool.id, 'filament_id': target.id})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('material', ['PETG', 'OTHER'])
def test_unassigned_spool_cannot_change_material(inventory_client, review_db, material):
    upload(inventory_client)
    review_db.get(FilamentLibrary, 990).material = material; review_db.commit()
    sid = inventory_client.post('/api/spools', json={'filament_id': 990}).json()['id']
    target = review_db.query(FilamentLibrary).filter_by(catalogue_code='ODIN-TEST-ORBIT').one()
    response = inventory_client.patch(f'/api/spools/{sid}', json={'filament_id': target.id})
    assert response.status_code == 409 and 'same material' in response.json()['detail']
    review_db.expire_all(); assert review_db.get(Spool, sid).filament_id == 990


@pytest.mark.parametrize('field', ['fila_type', 'fila_color_code', 'fila_color_name'])
def test_existing_import_bounds_refuse_whole_file(inventory_client, review_db, field):
    data = payload()
    data['data'][-1][field] = {'en': 'X' * 101} if field == 'fila_color_name' else 'X' * 101
    response = upload(inventory_client, data)
    assert response.status_code == 400
    assert review_db.query(FilamentLibrary).filter(FilamentLibrary.catalogue_code.isnot(None)).count() == 0


def test_combined_name_is_bounded_by_inputs_and_column_is_unbounded(inventory_client, review_db):
    from sqlalchemy.dialects import postgresql
    data = payload()
    data['data'][0]['fila_type'] = 'T' * 100
    data['data'][0]['fila_color_name']['en'] = 'C' * 100
    assert FilamentLibrary.__table__.c.name.type.compile(dialect=postgresql.dialect()) == 'VARCHAR'
    assert upload(inventory_client, data).status_code == 200
    row = review_db.query(FilamentLibrary).filter_by(catalogue_code=data['data'][0]['fila_color_code']).one()
    assert len(row.name) == 201


def test_release_note_is_under_added():
    from pathlib import Path
    changelog = (Path(__file__).resolve().parents[2] / 'CHANGELOG.md').read_text()
    start = changelog.index('## [Unreleased]')
    added = changelog.index('### Added', start)
    note = changelog.index('- Import your Bambu filament catalogue', start)
    assert added < note < changelog.index('### Changed', added)
