"""Synthetic HTTP contracts for planning a load without releasing unsafe jobs."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_schedule_ams_mapping import mapping_client, MAPPING  # noqa: F401


def schedule(client, auth, *, hold=True, kind='print-files', mapping=MAPPING):
    return client.post(f'/api/{kind}/501/schedule?printer_id=9', headers=auth(),
                       json={'hold': hold, 'ams_mapping': mapping})


def load(db, material):
    db.execute(text('UPDATE filament_slots SET filament_type=:m WHERE printer_id=9 AND slot_number=4'), {'m': material})
    db.commit()


@pytest.mark.parametrize('kind', ['print-files', 'models'])
@pytest.mark.parametrize('material', ['PLA', None, 'empty'])
@pytest.mark.parametrize('hold', [True, False])
def test_planning_only_skips_material(mapping_client, review_db, kind, material, hold):
    client, auth = mapping_client
    load(review_db, material)
    response = schedule(client, auth, hold=hold, kind=kind)
    assert response.status_code == (200 if hold else 400), response.text
    if not hold:
        assert response.json()['detail'] == 'Selected tray material does not match the source filament'
        assert review_db.execute(text('SELECT count(*) FROM jobs WHERE print_file_id=501')).scalar() == 0
        return
    job_id = response.json()['job_id']
    job = client.get(f'/api/jobs/{job_id}', headers=auth()).json()
    assert job['hold'] is True and job['ams_mapping'] == [3]
    assert job['ams_preview']['planned_loads'] == ['load PETG in AMS 1 slot 4']
    events = client.get(f'/api/jobs/{job_id}/timeline', headers=auth()).json()['events']
    event = next(e for e in events if e['kind'] == 'ams_mapping_changed')
    assert event['detail'] == {'ams_mapping': [3], 'previous_ams_mapping': None,
                              'message': 'mapping planned, not checked against the loaded trays'}


@pytest.mark.parametrize('mapping', [[], [{'source_slot': 0, 'target_slot': 16}],
    [{'source_slot': 0, 'target_slot': 0}], [{'source_slot': True, 'target_slot': 4}],
    [{'source_slot': 2, 'target_slot': 4}], [{'source_slot': 0, 'target_slot': 17}]])
def test_held_mapping_still_checks_structure(mapping_client, review_db, mapping):
    client, auth = mapping_client
    assert schedule(client, auth, mapping=mapping).status_code == 400
    assert review_db.execute(text('SELECT count(*) FROM jobs WHERE print_file_id=501')).scalar() == 0


@pytest.mark.parametrize('mode', ['patch', 'bulk'])
@pytest.mark.parametrize('material', ['PLA', None])
def test_release_rechecks_live_and_preserves_hold_on_refusal(mapping_client, review_db, mode, material):
    client, auth = mapping_client
    job = schedule(client, auth).json()['job_id']
    load(review_db, material)
    def release():
        if mode == 'patch':
            return client.patch(f'/api/jobs/{job}', headers=auth(), json={'hold': False, 'notes': 'atomic'})
        return client.post('/api/jobs/bulk-update', headers=auth(), json={'action': 'unhold', 'job_ids': [job]})
    response = release()
    assert response.status_code == 400, response.text
    assert response.json()['detail'] == 'Selected tray material does not match the source filament'
    row = review_db.execute(text('SELECT hold,notes FROM jobs WHERE id=:id'), {'id': job}).one()
    assert bool(row.hold) is True and row.notes is None
    load(review_db, 'PETG')
    detail = client.get(f'/api/jobs/{job}', headers=auth()).json()
    assert detail['ams_preview']['planned_loads'] == []
    assert release().status_code == 200
    assert not review_db.execute(text('SELECT hold FROM jobs WHERE id=:id'), {'id': job}).scalar()
    assert client.get(f'/api/jobs/{job}', headers=auth()).json()['ams_preview'].get('planned_loads', []) == []


def test_held_remap_and_combined_release(mapping_client, review_db):
    client, auth = mapping_client
    job = schedule(client, auth).json()['job_id']
    load(review_db, 'PLA')
    patch = lambda body: client.patch(f'/api/jobs/{job}', headers=auth(), json=body)
    assert patch({'ams_mapping': MAPPING}).status_code == 200
    assert patch({'ams_mapping': MAPPING, 'hold': False}).status_code == 400
    assert patch({'ams_mapping': None, 'hold': False}).status_code == 400
    assert review_db.execute(text('SELECT hold FROM jobs WHERE id=:id'), {'id': job}).scalar()
    assert patch({'ams_mapping': [{'source_slot': 1, 'target_slot': 3}], 'hold': False}).status_code == 200


def test_bulk_release_refusal_is_atomic(mapping_client, review_db):
    client, auth = mapping_client
    first = schedule(client, auth, kind='models', mapping=[{'source_slot': 1, 'target_slot': 3}]).json()['job_id']
    second = schedule(client, auth, kind='models').json()['job_id']
    load(review_db, 'PLA')
    response = client.post('/api/jobs/bulk-update', headers=auth(), json={'action': 'unhold', 'job_ids': [first, second]})
    assert response.status_code == 400, response.text
    assert all(review_db.execute(text('SELECT hold FROM jobs WHERE id=:id'), {'id': jid}).scalar() for jid in [first, second])


@pytest.mark.parametrize('held,loaded,expected', [
    (True, 'PLA', ['load PETG-CF in AMS 2 slot 2']),
    (True, 'PETG', ['load PETG-CF in AMS 2 slot 2']),
    (True, ' petg_cf ', []), (False, 'PLA', []),
    (True, None, ['load PETG-CF in AMS 2 slot 2']),
])
def test_live_plan_uses_exact_material_and_physical_slot(held, loaded, expected):
    from types import SimpleNamespace
    from modules.jobs.ams_mapping import planned_loads
    job = SimpleNamespace(hold=held, ams_mapping=[-1, -1, 5])
    facts = {'required': [{'slot': 3, 'material': 'PETG_CF'}],
             'available': [{'slot': 6, 'material': loaded}]}
    assert planned_loads(job, facts) == expected


def test_held_schedule_still_checks_printer_ownership(mapping_client, review_db):
    client, auth = mapping_client
    review_db.execute(text("INSERT INTO printers(id,name,org_id,api_type,is_active,shared) VALUES(99,'Foreign synthetic',2,'bambu',1,0)"))
    review_db.commit()
    response = client.post('/api/print-files/501/schedule?printer_id=99', headers=auth(),
                           json={'hold': True, 'ams_mapping': MAPPING})
    assert response.status_code == 404
    assert review_db.execute(text('SELECT count(*) FROM jobs WHERE print_file_id=501')).scalar() == 0


@pytest.mark.parametrize('held', [True, False])
@pytest.mark.parametrize('material', ['PETG', 'PLA', None, 'empty'])
def test_repeat_mapping_checks_live_trays_or_stays_held(mapping_client, review_db, held, material):
    client, auth = mapping_client
    original = schedule(client, auth, hold=held).json()['job_id']
    if not held:
        review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"), {'id': original})
        review_db.commit()
    load(review_db, material)
    response = client.post(f'/api/jobs/{original}/repeat', headers=auth())
    assert response.status_code == 200, response.text
    planned = held or material != 'PETG'
    assert response.json()['held_for_planned_load'] is planned
    repeated = response.json()['new_job_id']
    detail = client.get(f'/api/jobs/{repeated}', headers=auth()).json()
    assert detail['hold'] is planned
    assert detail['status'] == 'pending' and detail['ams_mapping'] == [3]
    assert detail['ams_preview'].get('planned_loads', []) == (
        ['load PETG in AMS 1 slot 4'] if material != 'PETG' else [])
    fields = 'model_id,print_file_id,payload_sha256,plate_index,printer_id,target_type,target_filter'
    assert review_db.execute(text(f'SELECT {fields} FROM jobs WHERE id=:id'), {'id': repeated}).one() == \
        review_db.execute(text(f'SELECT {fields} FROM jobs WHERE id=:id'), {'id': original}).one()
    events = client.get(f'/api/jobs/{repeated}/timeline', headers=auth()).json()['events']
    mappings = [event for event in events if event['kind'] == 'ams_mapping_changed']
    expected = {'ams_mapping': [3], 'previous_ams_mapping': None}
    if planned:
        expected['message'] = 'mapping planned, not checked against the loaded trays'
    assert [event['detail'] for event in mappings] == [expected]
    if planned and material != 'PETG':
        refused = client.patch(f'/api/jobs/{repeated}', headers=auth(), json={'hold': False})
        assert refused.status_code == 400, refused.text
        assert review_db.execute(text('SELECT hold FROM jobs WHERE id=:id'), {'id': repeated}).scalar()
        load(review_db, 'PETG')
        assert client.patch(f'/api/jobs/{repeated}', headers=auth(), json={'hold': False}).status_code == 200


def repeat_state(db):
    """A refused repeat must not charge quota, create a job, or stage side effects."""
    return {table: db.execute(text(f'SELECT * FROM {table} ORDER BY 1')).all()
            for table in ('jobs', 'quota_usage', 'audit_logs', 'alerts', 'cost_source_lifetimes')}


@pytest.mark.parametrize('held', [True, False])
@pytest.mark.parametrize('change', ['missing-tray', 'source', 'protocol', 'later-invalid-tray'])
def test_repeat_mapping_still_validates_structure(mapping_client, review_db, held, change):
    client, auth = mapping_client
    original = schedule(client, auth, hold=held).json()['job_id']
    statements = {
        'missing-tray': 'DELETE FROM filament_slots WHERE printer_id=9 AND slot_number=4',
        'source': "UPDATE print_files SET filaments_json='[]' WHERE id=501",
        'protocol': "UPDATE printers SET api_type='moonraker' WHERE id=9",
        'later-invalid-tray': "UPDATE jobs SET ams_mapping='[3,16]' WHERE id=:id",
    }
    review_db.execute(text(statements[change]), {'id': original})
    if change == 'later-invalid-tray':
        review_db.execute(text('UPDATE print_files SET filaments_json=:f WHERE id=501'),
                          {'f': json.dumps([{'slot': 1, 'type': 'PETG'}, {'slot': 2, 'type': 'PLA'}])})
        load(review_db, 'PLA')
    review_db.commit()
    before = repeat_state(review_db)
    response = client.post(f'/api/jobs/{original}/repeat', headers=auth())
    assert response.status_code == 400, response.text
    assert repeat_state(review_db) == before


@pytest.mark.parametrize('held', [True, False])
def test_repeat_without_mapping_keeps_existing_unheld_behavior(mapping_client, review_db, held):
    client, auth = mapping_client
    original = schedule(client, auth, hold=held, mapping=None).json()['job_id']
    response = client.post(f'/api/jobs/{original}/repeat', headers=auth())
    assert response.status_code == 200, response.text
    assert response.json()['held_for_planned_load'] is False
    repeated = response.json()['new_job_id']
    detail = client.get(f'/api/jobs/{repeated}', headers=auth()).json()
    assert detail['hold'] is False and detail['ams_mapping'] is None
    events = client.get(f'/api/jobs/{repeated}/timeline', headers=auth()).json()['events']
    assert not any(event['kind'] == 'ams_mapping_changed' for event in events)
