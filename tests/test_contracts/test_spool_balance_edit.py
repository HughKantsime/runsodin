"""Absolute spool edits share the ledger and never claim a print's filament."""
import json
import pytest
from sqlalchemy import text

from tests.test_contracts.test_inventory_claim_acceptance import inventory_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_archive_filament_settlement import (
    settlement_case, archive_case, post, preview, query, write,  # noqa: F401
)


def create(client):
    response = client.post('/api/spools', json={'filament_id': 990, 'initial_weight_g': 1000})
    assert response.status_code == 200, response.text
    return response.json()['id']


def audit(db, sid):
    return [json.loads(row[0]) for row in db.execute(text(
        "SELECT details FROM audit_logs WHERE action='spool.balance_edited' AND entity_id=:id"), {'id': sid})]


def test_patch_balance_decrease_writes_manual_usage_and_audit(inventory_client, review_db):
    sid = create(inventory_client)
    response = inventory_client.patch(f'/api/spools/{sid}', json={'remaining_weight_g': 900, 'notes': 'Corrected'})
    assert response.status_code == 200, response.text
    assert inventory_client.get(f'/api/spools/{sid}').json()['remaining_weight_g'] == 900
    assert review_db.execute(text('SELECT weight_used_g,notes,job_id,print_job_id FROM spool_usage WHERE spool_id=:id'),
                             {'id': sid}).all() == [(100, 'manual: balance edit', None, None)]
    assert audit(review_db, sid) == [{'before_g': 1000, 'after_g': 900}]
    marker = review_db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                               {'key': f'filament_balance_edited:{sid}'}).scalar_one()
    assert isinstance(json.loads(marker), str)
    assert inventory_client.get(f'/api/spools/{sid}').json()['notes'] == 'Corrected'


@pytest.mark.parametrize('before,after', [(900, 1000), (None, 700), (1000, 1100)])
def test_patch_balance_increase_or_first_value_writes_no_usage(inventory_client, review_db, before, after):
    sid = create(inventory_client)
    review_db.execute(text('UPDATE spools SET remaining_weight_g=:g WHERE id=:id'), {'g': before, 'id': sid})
    review_db.commit()
    response = inventory_client.patch(f'/api/spools/{sid}', json={'remaining_weight_g': after})
    assert response.status_code == 200, response.text
    assert inventory_client.get(f'/api/spools/{sid}').json()['remaining_weight_g'] == after
    assert not review_db.execute(text('SELECT id FROM spool_usage WHERE spool_id=:id'), {'id': sid}).all()
    assert audit(review_db, sid) == [{'before_g': before, 'after_g': after}]
    assert review_db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                             {'key': f'filament_balance_edited:{sid}'}).scalar_one()


@pytest.mark.parametrize('value,message', [
    (-1, 'Remaining weight must be zero or more grams.'),
    ('NaN', 'Remaining weight must be zero or more grams.'),
    ('Infinity', 'Remaining weight must be zero or more grams.'),
    ('-Infinity', 'Remaining weight must be zero or more grams.'),
    ('not a number', 'Remaining weight must be zero or more grams.'),
    (1100.1, 'Remaining weight cannot be more than the spool held when new (1000 g).'),
    (None, 'A known remaining weight cannot be cleared. Enter the grams, or weigh the spool.'),
])
def test_patch_balance_refuses_negative_nonfinite_too_large_and_clearing(inventory_client, review_db, value, message):
    sid = create(inventory_client)
    before = inventory_client.get(f'/api/spools/{sid}').json()
    audits = review_db.execute(text('SELECT * FROM audit_logs')).all()
    response = inventory_client.patch(f'/api/spools/{sid}', json={
        'remaining_weight_g': value, 'notes': 'Must not save', 'spool_weight_g': 180})
    assert response.status_code == 422, response.text
    assert response.json()['detail'] == message
    assert inventory_client.get(f'/api/spools/{sid}').json() == before
    assert not review_db.execute(text('SELECT id FROM spool_usage WHERE spool_id=:id'), {'id': sid}).all()
    assert review_db.execute(text('SELECT * FROM audit_logs')).all() == audits
    assert not review_db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                                 {'key': f'filament_balance_edited:{sid}'}).all()


@pytest.mark.parametrize('body', [{'notes': 'Only notes'}, {'remaining_weight_g': 1000}, {'remaining_weight_g': None}])
def test_patch_without_balance_change_makes_no_ledger_call(inventory_client, review_db, monkeypatch, body):
    from core import filament_ledger
    sid = create(inventory_client)
    if body.get('remaining_weight_g', 1000) is None:
        review_db.execute(text('UPDATE spools SET remaining_weight_g=NULL WHERE id=:id'), {'id': sid})
        review_db.commit()
    def unexpected(*args, **kwargs):
        pytest.fail('Unchanged balance must not call the ledger')
    monkeypatch.setattr(filament_ledger, 'record_balance_edit', unexpected)
    response = inventory_client.patch(f'/api/spools/{sid}', json=body)
    assert response.status_code == 200, response.text
    assert not audit(review_db, sid)
    assert not review_db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                                 {'key': f'filament_balance_edited:{sid}'}).all()


@pytest.mark.parametrize('remaining,status,expected', [(0, 'active', 'empty'), (10, 'active', 'empty'),
                                                     (10.1, 'active', 'active'), (900, 'empty', 'empty')])
def test_patch_balance_status_matches_weighing(inventory_client, review_db, remaining, status, expected):
    sid = create(inventory_client)
    review_db.execute(text('UPDATE spools SET status=:status WHERE id=:id'), {'status': status, 'id': sid})
    review_db.commit()
    assert inventory_client.patch(f'/api/spools/{sid}', json={'remaining_weight_g': remaining}).status_code == 200
    assert inventory_client.get(f'/api/spools/{sid}').json()['status'] == expected


@pytest.mark.parametrize('before,after', [(1000, 900), (900, 1000), (None, 700)])
def test_patch_balance_edit_never_blocks_or_double_counts_a_print(settlement_case, before, after):
    from core import filament_ledger
    from core.print_attempts import sqlalchemy_runner
    from modules.inventory.routes.spools import router
    case = settlement_case
    case[0].app.include_router(router)
    write(case, 'UPDATE spools SET remaining_weight_g=:g WHERE id=7099', {'g': before})
    response = case[0].patch('/spools/7099', json={'remaining_weight_g': after})
    assert response.status_code == 200, response.text
    data = preview(case).json()
    assert data['settleable'] and not data['manual_usage'] and not data['prior_usage']
    assert any(row['spool_id'] == 7099 and row['how'] == 'edited' for row in data['balance_set_after_print'])
    with case[1].begin() as db:
        for (pid,) in db.execute(text('SELECT id FROM print_jobs')):
            assert filament_ledger.manual_rows_for_print(sqlalchemy_runner(db), 7099, pid) == []
        assert db.execute(text('SELECT COUNT(*) FROM print_filament_evidence')).scalar_one() == 0
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'balance_set_after_print'
    assert 'Its balance was set during or after this print and may already include this use.' in response.json()['detail']['message']
    assert post(case, no_deduction='balance_already_updated').status_code == 200
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(after,)]
    assert len(query(case, 'SELECT id FROM spool_usage')) == (1 if before is not None and before > after else 0)


def test_balance_edit_usage_is_warning_evidence_without_marker(settlement_case):
    case = settlement_case
    write(case, "INSERT INTO spool_usage(spool_id,weight_used_g,used_at,notes) "
                "VALUES(7099,100,'2026-10-11 14:00:00','manual: balance edit')")
    assert any(row['spool_id'] == 7099 and row['how'] == 'edited'
               for row in preview(case).json()['balance_set_after_print'])


def test_scale_marker_after_edit_still_says_weighed(settlement_case):
    from modules.inventory.routes.spools import router
    case = settlement_case
    write(case, 'UPDATE spools SET spool_weight_g=250,initial_weight_g=1000 WHERE id=7099')
    case[0].app.include_router(router)
    assert case[0].patch('/spools/7099', json={'remaining_weight_g': 900}).status_code == 200
    assert case[0].post('/spools/7099/weigh', json={'gross_weight_g': 1050}).status_code == 200
    assert any(row['spool_id'] == 7099 and row['how'] == 'weighed'
               for row in preview(case).json()['balance_set_after_print'])


def test_patch_balance_audit_failure_rolls_back_every_change(settlement_case, monkeypatch):
    from modules.inventory.routes import spools
    case = settlement_case
    case[0].app.include_router(spools.router)
    before = query(case, 'SELECT * FROM spools WHERE id=7099')
    def refuse(*args, **kwargs):
        raise RuntimeError('Test audit failure')
    monkeypatch.setattr(spools, 'log_audit', refuse)
    with pytest.raises(RuntimeError, match='Test audit failure'):
        case[0].patch('/spools/7099', json={'remaining_weight_g': 900, 'notes': 'Must roll back'})
    assert query(case, 'SELECT * FROM spools WHERE id=7099') == before
    assert not query(case, 'SELECT * FROM spool_usage')
    assert not query(case, "SELECT value FROM system_config WHERE key='filament_balance_edited:7099'")
