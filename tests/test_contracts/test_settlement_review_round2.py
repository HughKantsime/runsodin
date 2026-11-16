"""Physical-print retention and absolute-balance regressions from review round two."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_filament_settlement import (
    settlement_case, archive_case, review_db, post, preview, query, write, manual,
)
from tests.test_contracts.test_settlement_review_stock import no_external_delivery


def completion_routes(case):
    from modules.jobs.routes import jobs_lifecycle, jobs_crud
    case[0].app.include_router(jobs_lifecycle.router)
    case[0].app.include_router(jobs_crud.router)
    case[3].update(role='admin', is_superadmin=True)
    write(case, "UPDATE jobs SET status='failed',quantity=1,hold=0 WHERE id=7099")


def detach(case):
    from core.print_attempts import detach_print_attempt, sqlalchemy_runner
    with case[1].begin() as db:
        detach_print_attempt(sqlalchemy_runner(db), 7099, 7099)
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=7099'))


@pytest.mark.parametrize('reuse', [False, True])
@pytest.mark.parametrize('deducted', [False, True])
def test_deleted_job_settlement_preserves_print_and_new_job(settlement_case, reuse, deducted):
    case = settlement_case
    completion_routes(case)
    if deducted:
        response = case[0].post('/jobs/7099/complete')
        assert response.status_code == 200, response.text
    before = query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099')[0][0]
    response = case[0].delete('/jobs/7099')
    assert response.status_code == 204, response.text
    if reuse:
        write(case, "INSERT INTO jobs(id,item_name,printer_id,print_file_id,charged_to_org_id,status,quantity,hold,ams_mapping) VALUES(7099,'New job',9,7099,1,'failed',1,0,'[0,1]')")
    data = preview(case).json()
    assert bool(data['prior_usage']) == deducted
    assert data['job_id'] is None
    if deducted:
        refusal = post(case, entries=[dict(spool_id=7099, grams=40)])
        assert refusal.status_code == 409, refusal.text
        assert refusal.json()['detail']['code'] == 'already_deducted'
        assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(before,)]
    response = post(case, **({'no_deduction': 'accept_existing'} if deducted else
                            {'entries': [dict(spool_id=7099, grams=40)]}))
    assert response.status_code == 200, response.text
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(before if deducted else before - 40,)]
    assert not query(case, "SELECT key FROM system_config WHERE key LIKE 'quota_attempt:filament:job:7099:%'")
    if reuse:
        response = case[0].post('/jobs/7099/complete')
        assert response.status_code == 200, response.text
        assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(before - (40 if deducted else 80),)]


def test_legacy_dangling_job_is_absent_for_settlement(settlement_case):
    case = settlement_case
    write(case, 'UPDATE print_jobs SET scheduled_job_id=999999 WHERE id=7099')
    assert preview(case).json()['job_id'] is None
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 200, response.text
    assert query(case, 'SELECT job_id FROM spool_usage') == [(None,)]
    assert not query(case, "SELECT key FROM system_config WHERE key LIKE 'quota_attempt:filament:job:999999:%'")


@pytest.mark.parametrize('unlink', ['detach', 'delete'])
def test_manual_usage_still_needs_review_after_unlink(settlement_case, unlink):
    case = settlement_case
    row = manual(case, 40)
    # A different attempt's manual row must not be retained for this print.
    write(case, "INSERT INTO audit_logs(action,entity_type,entity_id,timestamp) VALUES('job.reset','job',7099,'2026-10-11 14:00:00')")
    write(case, "UPDATE spool_usage SET used_at='2026-10-11 12:30:00' WHERE id=:id", {'id': row})
    manual(case, 5)
    if unlink == 'detach':
        detach(case)
    else:
        completion_routes(case)
        assert case[0].delete('/jobs/7099').status_code == 204
    data = preview(case).json()
    assert data['prior_usage'] is None  # Manual evidence is not an automatic claim.
    assert [r['usage_id'] for r in data['manual_usage']] == [row]
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'manual_usage_unreviewed'
    assert post(case, entries=[dict(spool_id=7099, grams=40)], counted_usage_ids=[row]).status_code == 200
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(955,)]


def test_detach_replaces_stale_physical_marker(settlement_case):
    case = settlement_case
    completion_routes(case)
    assert case[0].post('/jobs/7099/complete').status_code == 200
    write(case, 'INSERT INTO system_config(key,value) VALUES(:key,:value)',
          dict(key='filament_settlement:print:7099', value=json.dumps(dict(print_identity=['old', 9, '2020-01-01']))))
    detach(case)
    assert preview(case).json()['prior_usage'] == 'marker'
    assert post(case, entries=[dict(spool_id=7099, grams=40)]).status_code == 409
    assert post(case, no_deduction='accept_existing').status_code == 200
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(960,)]


def test_detached_manual_review_stays_frozen_after_relink(settlement_case):
    case = settlement_case
    completion_routes(case)
    original = manual(case, 40)
    detach(case)
    manual(case, 5)
    # Another print's marker must not replace this print's retained evidence.
    write(case, "INSERT INTO system_config(key,value) VALUES('filament_settlement:print:12345',:value)",
          {'value': json.dumps({'source': 'settlement'})})
    response = case[0].post('/jobs/7099/link-print?print_job_id=7099')
    assert response.status_code == 200, response.text
    assert [r['usage_id'] for r in preview(case).json()['manual_usage']] == [original]
    response = post(case, entries=[dict(spool_id=7099, grams=40)], counted_usage_ids=[original])
    assert response.status_code == 200, response.text
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(955,)]


def test_relinked_print_cost_excludes_later_job_completion(settlement_case):
    from core.print_attempts import bind_print_attempt, sqlalchemy_runner
    case = settlement_case
    completion_routes(case)
    with case[1].begin() as db:
        bind_print_attempt(sqlalchemy_runner(db), 7099, 7099)
    assert case[0].post('/jobs/7099/complete').status_code == 200
    original = [r[0] for r in query(case, 'SELECT id FROM spool_usage ORDER BY id')]
    detach(case)
    write(case, "UPDATE jobs SET status='failed' WHERE id=7099")
    assert case[0].post('/jobs/7099/complete').status_code == 200
    response = case[0].post('/jobs/7099/link-print?print_job_id=7099')
    assert response.status_code == 200, response.text
    response = post(case, no_deduction='accept_existing')
    assert response.status_code == 200, response.text
    entries = json.loads(query(case, 'SELECT consumption_json FROM print_archives WHERE id=:id', {'id': case[2]})[0][0])
    assert [uid for entry in entries for uid in entry['usage_ids']] == original
    assert sum(entry['grams_used'] for entry in entries) == 80  # Two 40 g spools.


@pytest.mark.parametrize('usage_ids', [1, 'bad', {'id': 1}, None, [None]])
def test_bad_retained_usage_ids_refuse_without_crash(settlement_case, usage_ids):
    from core.filament_ledger import _settlement_print_identity
    from core.print_attempts import sqlalchemy_runner
    case = settlement_case
    with case[1].begin() as db:
        identity = _settlement_print_identity(sqlalchemy_runner(db), 7099)
    write(case, 'INSERT INTO system_config(key,value) VALUES(:key,:value)',
          dict(key='filament_settlement:print:7099', value=json.dumps(dict(print_identity=identity, usage_ids=usage_ids))))
    data = preview(case).json()
    assert not data['settleable']
    assert 'cannot be read' in data['not_settleable']
    for body in ({'entries': [dict(spool_id=7099, grams=40)]}, {'no_deduction': 'accept_existing'}):
        response = post(case, **body)
        assert response.status_code == 409, response.text
        assert 'cannot be read' in response.json()['detail']['message']
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(1000,)]
    assert not query(case, 'SELECT * FROM spool_usage')


@pytest.mark.parametrize('kind', ['created', 'weighed_mid_print', 'raw_weighed_mid_print', 'legacy_weigh_row', 'unreadable_weigh', 'patch_mid_print'])
def test_balance_evidence_requires_override(settlement_case, monkeypatch, kind):
    case = settlement_case
    write(case, "UPDATE spools SET created_at='2026-10-01' WHERE id=7099")
    if kind == 'created':
        write(case, "UPDATE spools SET created_at='2026-10-11 14:00:00' WHERE id=7099")
    elif kind == 'patch_mid_print':
        from datetime import datetime
        from core import filament_ledger
        from modules.inventory.routes.spools import router
        case[0].app.include_router(router)
        monkeypatch.setattr(filament_ledger, '_now', lambda: datetime(2026, 10, 11, 12, 30))
        response = case[0].patch('/spools/7099', json={'remaining_weight_g': 900})
        assert response.status_code == 200, response.text
    elif kind == 'legacy_weigh_row':
        write(case, "INSERT INTO spool_usage(spool_id,weight_used_g,used_at,notes) VALUES(7099,10,'2026-10-11 12:30:00','Manual weigh adjustment')")
    else:
        value = '2026-10-11 12:30:00' if kind == 'raw_weighed_mid_print' else json.dumps('broken' if kind == 'unreadable_weigh' else '2026-10-11 12:30:00')
        write(case, "INSERT INTO system_config(key,value) VALUES('filament_balance_weighed:7099',:v)", {'v': value})
    data = preview(case).json()
    warning = next((row for row in data['balance_set_after_print'] if row['spool_id'] == 7099), None)
    assert warning is not None
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'balance_set_after_print'
    if kind == 'created':
        assert 'This spool was added after this print ended' in response.json()['detail']['message']
    if kind == 'unreadable_weigh':
        assert 'cannot tell' in response.json()['detail']['message']
    assert post(case, entries=[dict(spool_id=7099, grams=40)], deduct_despite_later_balance=[7099]).status_code == 200


@pytest.mark.parametrize('balance', [1000, None])
def test_unchanged_patch_does_not_record_balance_set(settlement_case, balance):
    from modules.inventory.routes.spools import router
    case = settlement_case
    case[0].app.include_router(router)
    write(case, 'UPDATE spools SET remaining_weight_g=:balance WHERE id=7099', {'balance': balance})
    response = case[0].patch('/spools/7099', json={'remaining_weight_g': balance})
    assert response.status_code == 200, response.text
    assert not query(case, "SELECT value FROM system_config WHERE key='filament_balance_weighed:7099'")


@pytest.mark.parametrize('denial', ['role', 'scope'])
def test_permission_refusal_keeps_standard_detail(settlement_case, denial):
    case = settlement_case
    if denial == 'role':
        case[3]['role'] = 'viewer'
    else:
        case[3].update(_auth_kind='user_api_token', _token_scopes=['read:jobs'])
    response = post(case, no_deduction='nothing_extruded')
    assert response.status_code == 403
    assert response.json()['detail'] == ('Insufficient permissions' if denial == 'role' else
                                        "Insufficient token scope — 'write:spools' scope required")
