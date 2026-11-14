"""Outcome-only contract and regressions for the eleven-point review."""
import json

import pytest
from sqlalchemy import text

from tests.test_contracts.test_mark_print_failed import (
    client, correction_client, token_client, review_db, mark, undo, tables, BODY, URL,
)
from tests.test_contracts.test_job_outcome_correction import snapshot


@pytest.mark.parametrize('field,value', [('filament_used_g', 0), ('filament_used_g', 20),
                                        ('filament_used_g', None), ('no_material_used', True)])
def test_removed_material_fields_are_named_in_422(client, review_db, field, value):
    before = tables(review_db)
    response = mark(client, dict(BODY, **{field: value}))
    assert response.status_code == 422, response.text
    assert any(error['loc'] == ['body', field] and error['type'] == 'extra_forbidden'
               for error in response.json()['detail'])
    assert tables(review_db) == before


def material_bytes(db):
    """Serialize exact stored values, including unnormalized JSON and ledger rows."""
    values = snapshot(db)
    for table in ('print_jobs', 'print_archives'):
        rows = db.execute(text('SELECT * FROM ' + table + ' ORDER BY id')).mappings().all()
        values[table] = [{k: v for k, v in row.items() if k != 'status'} for row in rows]
    values['spool_usage'] = list(db.execute(text('SELECT * FROM spool_usage ORDER BY id')))
    return repr(values).encode()


@pytest.mark.parametrize('shape', ['own_deduction', 'own_usage_without_marker', 'prior_deduction', 'several_filaments',
                                  'disagreeing_grams', 'unknown_grams', 'malformed_snapshot'])
def test_material_cost_ledger_spool_byte_identical_through_mark_and_undo(client, review_db, shape):
    if shape in ('own_deduction', 'prior_deduction'):
        from core.filament_ledger import record_job_filament
        from core.print_attempts import sqlalchemy_runner
        assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive',
                                   print_job_id=902 if shape == 'own_deduction' else None,
                                   grams_by_spool={901: 40}).deducted
    if shape == 'own_usage_without_marker':
        from core.filament_ledger import prior_attempt_usage, PRINT_USAGE
        from core.print_attempts import sqlalchemy_runner
        review_db.execute(text('INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g) VALUES(901,901,902,40)'))
        # Finding 1 assumes this is falsy, but the helper explicitly detects own usage.
        assert prior_attempt_usage(sqlalchemy_runner(review_db), 901, 902) == PRINT_USAGE
    if shape == 'several_filaments':
        review_db.execute(text('UPDATE print_archives SET consumption_json=:v WHERE id=903'),
                          {'v': '[ {"spool_id": null, "grams_used": 20}, {"spool_id": null, "grams_used": 30} ]'})
    if shape == 'disagreeing_grams':
        review_db.execute(text('UPDATE print_jobs SET filament_used_g=0 WHERE id=902'))
    if shape == 'unknown_grams':
        review_db.execute(text('UPDATE print_jobs SET filament_used_g=NULL WHERE id=902'))
        review_db.execute(text('UPDATE print_archives SET filament_used_grams=NULL WHERE id=903'))
    if shape == 'malformed_snapshot':
        review_db.execute(text("UPDATE print_jobs SET spool_snapshot='not JSON' WHERE id=902"))
    review_db.commit()
    before = material_bytes(review_db)
    assert mark(client).status_code == 200
    assert material_bytes(review_db) == before
    assert undo(client).status_code == 200
    assert material_bytes(review_db) == before


@pytest.mark.parametrize('change,record_name,status', [
    ("UPDATE print_jobs SET status='completed' WHERE id=902", 'Print 902', 'completed'),
    ("UPDATE print_archives SET status='completed' WHERE id=903", 'Archive 903', 'cancelled'),
    ("INSERT INTO print_archives(id,print_job_id,printer_id,status,ownership_state,org_id) VALUES(904,902,9,'failed','known',1)", 'Archive 904', 'cancelled'),
    ("UPDATE jobs SET status='completed' WHERE id=901", 'Job 901', 'cancelled'),
])
def test_undo_preserves_and_names_later_outcomes_and_new_archives(client, review_db, change, record_name, status):
    assert mark(client).status_code == 200
    review_db.execute(text(change)); review_db.commit()
    response = undo(client)
    assert response.status_code == 200, response.text
    assert record_name in ' '.join(response.json()['warnings'])
    assert response.json()['status'] == status
    assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == status
    if record_name == 'Archive 903':
        assert review_db.execute(text('SELECT status FROM print_archives WHERE id=903')).scalar_one() == 'completed'
    elif record_name == 'Archive 904':
        assert review_db.execute(text('SELECT status FROM print_archives WHERE id=904')).scalar_one() == 'failed'
        assert review_db.execute(text('SELECT status FROM print_archives WHERE id=903')).scalar_one() == 'cancelled'
    elif record_name == 'Job 901':
        assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'completed'


@pytest.mark.parametrize('change', ['filename', 'job_name', 'archive', 'job'])
def test_education_at_undo_time_refuses_exactly_as_mark(client, review_db, change):
    # A platform admin can read Education archives; generic mutation must still refuse.
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3')); review_db.commit()
    assert mark(client).status_code == 200
    if change in ('filename', 'job_name'):
        review_db.execute(text('UPDATE print_jobs SET ' + change + '=:v WHERE id=902'), {'v': 'odin-' + 'a' * 32 + '.3mf'})
    elif change == 'archive':
        review_db.execute(text('UPDATE print_archives SET education_submission_id=(SELECT id FROM education_submissions LIMIT 1) WHERE id=903'))
    else:
        review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=13 WHERE id=902'))
    review_db.commit()
    before = tables(review_db)
    correction = mark(client)
    reversal = undo(client)
    assert correction.status_code in (403, 409)
    assert reversal.status_code == correction.status_code, reversal.text
    assert reversal.json() == correction.json()
    assert tables(review_db) == before


def test_controlled_access_is_rechecked_at_undo(client, review_db):
    from tests.test_contracts.test_home_assistant_status import _mark_controlled
    _mark_controlled(review_db, 901, member=True); review_db.commit()
    assert mark(client).status_code == 200
    review_db.execute(text('DELETE FROM controlled_audience_members WHERE user_id=3')); review_db.commit()
    before = tables(review_db)
    assert undo(client).status_code == 404
    assert tables(review_db) == before


@pytest.mark.parametrize('role,org,scopes,matrix,expected', [
    ('operator', 1, ['read:jobs', 'write:jobs'], ['operator'], 200),
    ('admin', 1, ['read:jobs', 'write:jobs'], [], 403),
    ('operator', 1, ['read:jobs', 'write:jobs'], [], 403),
    ('viewer', 1, ['read:jobs', 'write:jobs'], ['viewer'], 403),
    ('admin', None, ['read:jobs', 'write:jobs'], [], 200),
    ('admin', None, ['read:jobs'], ['admin'], 403),
    ('operator', 1, ['read:jobs'], ['operator'], 403),
    ('operator', 2, ['read:jobs', 'write:jobs'], ['operator'], 404),
])
def test_button_permission_matches_endpoint(client, review_db, role, org, scopes, matrix, expected):
    c, issue = client
    review_db.execute(text('UPDATE users SET role=:role,group_id=:org WHERE id=3'), {'role': role, 'org': org})
    review_db.execute(text("UPDATE printers SET shared=1 WHERE id=9"))
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('rbac_permissions',:value)"),
                      {'value': json.dumps({'action_access': {'jobs.edit': matrix}})})
    review_db.commit()
    headers = issue(scopes)
    read = c.get(URL, headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()['failure_correction_allowed'] is (expected == 200)
    result = c.post(URL + '/mark-failed', headers=headers, json=BODY)
    assert result.status_code == expected, result.text


def test_restarted_row_with_same_nonce_cannot_inherit_correction(client, review_db):
    from modules.jobs.print_failure import record
    assert mark(client).status_code == 200
    nonce = review_db.execute(text('SELECT economic_birth_nonce FROM print_jobs WHERE id=902')).scalar_one()
    review_db.execute(text("UPDATE print_jobs SET started_at='2026-11-15 10:00:00',status='cancelled' WHERE id=902"))
    review_db.commit()
    assert review_db.execute(text('SELECT economic_birth_nonce FROM print_jobs WHERE id=902')).scalar_one() == nonce
    assert record(review_db, 902) is None
    c, issue = client
    assert c.get(URL, headers=issue(['read:jobs'])).json()['failure_correction'] is None
    before = tables(review_db)
    assert undo(client).status_code == 409
    assert tables(review_db) == before


def test_the_released_outcome_endpoint_still_reclassifies_in_both_directions(client, review_db):
    """POST /jobs/{id}/outcome shipped in 2.2.0 with cancelled -> failed; API clients keep it.

    The screens send cancelled prints to Mark as failed (reason, undo); this endpoint records no reason.
    While a Mark-as-failed correction stands it refuses, so the two paths cannot fight over one print.
    """
    c, issue = client
    response = c.post('/jobs/901/outcome', headers=issue(['write:jobs']),
                      json={'from_status': 'cancelled', 'status': 'failed', 'print_id': 902})
    assert response.status_code == 200, response.text
    back = c.post('/jobs/901/outcome', headers=issue(['write:jobs']),
                  json={'from_status': 'failed', 'status': 'cancelled', 'print_id': 902})
    assert back.status_code == 200, back.text
    assert mark(client).status_code == 200
    blocked = c.post('/jobs/901/outcome', headers=issue(['write:jobs']),
                     json={'from_status': 'failed', 'status': 'cancelled', 'print_id': 902})
    assert blocked.status_code == 409 and 'Back to cancelled' in blocked.json()['detail']


def test_archive_change_between_read_and_undo_write_is_preserved(client, review_db, monkeypatch):
    from modules.jobs import print_failure
    assert mark(client).status_code == 200
    write = print_failure._write_archive
    def late_outcome(db, identifier, values, **kwargs):
        # Inject the later outcome after undo has read its archive snapshot.
        db.execute(text("UPDATE print_archives SET status='completed' WHERE id=:id"), {'id': identifier})
        return write(db, identifier, values, **kwargs)
    monkeypatch.setattr(print_failure, '_write_archive', late_outcome)
    response = undo(client)
    assert response.status_code == 200
    assert review_db.execute(text('SELECT status FROM print_archives WHERE id=903')).scalar_one() == 'completed'
    assert 'Archive 903' in ' '.join(response.json()['warnings'])
