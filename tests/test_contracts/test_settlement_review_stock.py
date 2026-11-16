"""Regression evidence for stock review findings A1–A6."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_filament_settlement import (
    settlement_case, archive_case, review_db, post, preview, query, write,
)


@pytest.fixture(autouse=True)
def no_external_delivery(monkeypatch):
    from modules.inventory import services
    monkeypatch.setattr(services, 'push_consumption_to_spoolman', lambda *_: [])


def test_settled_unlinked_print_then_link_and_complete(settlement_case):
    from modules.jobs.routes import jobs_lifecycle
    case = settlement_case
    case[0].app.include_router(jobs_lifecycle.router)
    write(case, 'UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=7099')
    write(case, "UPDATE spools SET status='active',location_printer_id=9 WHERE id=7099")
    assert post(case, entries=[dict(spool_id=7099, grams=40)]).status_code == 200
    assert query(case, 'SELECT job_id FROM print_archives') == [(7099,)]
    assert query(case, 'SELECT job_id FROM spool_usage') == [(None,)]
    linked = case[0].post('/jobs/7099/link-print?print_job_id=7099')
    assert linked.status_code == 200, linked.text
    write(case, "UPDATE jobs SET status='failed',quantity=1,hold=0 WHERE id=7099")
    completed = case[0].post('/jobs/7099/complete')
    assert completed.status_code == 200, completed.text
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(960,)]
    assert query(case, 'SELECT COUNT(*) FROM spool_usage') == [(1,)]


def test_job_deduction_survives_detach(settlement_case):
    from core.print_attempts import bind_print_attempt, detach_print_attempt, sqlalchemy_runner
    from modules.jobs.routes import jobs_lifecycle
    case = settlement_case
    case[0].app.include_router(jobs_lifecycle.router)
    write(case, "UPDATE spools SET status='active',location_printer_id=9 WHERE id=7099")
    write(case, "UPDATE jobs SET status='failed',quantity=1,hold=0 WHERE id=7099")
    with case[1].begin() as db:
        bind_print_attempt(sqlalchemy_runner(db), 7099, 7099)
    assert case[0].post('/jobs/7099/complete').status_code == 200
    before = query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099')
    assert before[0][0] < 1000
    with case[1].begin() as db:
        detach_print_attempt(sqlalchemy_runner(db), 7099, 7099)
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=7099'))
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 409, response.text
    assert preview(case).json()['prior_usage']
    assert post(case, no_deduction='accept_existing').status_code == 200
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == before


def test_weigh_marker_is_json(settlement_case):
    from core.filament_ledger import record_weighed_spool
    from core.print_attempts import sqlalchemy_runner
    case = settlement_case
    with case[1].begin() as db:
        record_weighed_spool(sqlalchemy_runner(db), 7099, 1001)
    value = query(case, "SELECT value FROM system_config WHERE key='filament_balance_weighed:7099'")[0][0]
    assert isinstance(json.loads(value), str)
    assert preview(case).json()['balance_set_after_print']


@pytest.mark.parametrize('kind', ['patch', 'unknown_end', 'mid_print_measurement'])
def test_absolute_balance_evidence(settlement_case, kind):
    case = settlement_case
    if kind == 'patch':
        from modules.inventory.routes.spools import router
        case[0].app.include_router(router)
        response = case[0].patch('/spools/7099', json={'remaining_weight_g': 900})
        assert response.status_code == 200, response.text
    elif kind == 'unknown_end':
        write(case, 'UPDATE print_archives SET completed_at=NULL')
    else:
        write(case, "UPDATE spools SET remaining_measured_at='2026-10-11 12:30:00' WHERE id=7099")
    assert any(r['spool_id'] == 7099 for r in preview(case).json()['balance_set_after_print'])
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 409, response.text
    assert post(case, entries=[dict(spool_id=7099, grams=40)], deduct_despite_later_balance=[7099]).status_code == 200
    audit = json.loads(query(case, "SELECT details FROM audit_logs WHERE action='archive.filament_settled'")[0][0])
    assert audit['request']['deduct_despite_later_balance'] == [7099]


def test_second_invalid_spool_rolls_back_all_stock(settlement_case):
    case = settlement_case
    write(case, 'UPDATE spools SET remaining_weight_g=:value WHERE id=7100', {'value': float('inf')})
    response = post(case, entries=[dict(spool_id=7099, grams=40), dict(spool_id=7100, grams=40)])
    assert response.status_code == 409, response.text
    assert 'balance' in response.json()['detail']['message']
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(1000,)]
    assert not query(case, 'SELECT * FROM spool_usage')
    assert not query(case, "SELECT key FROM system_config WHERE key LIKE 'quota_attempt:filament:%' OR key LIKE 'filament_settlement:print:%'")
    assert query(case, 'SELECT accounting_state FROM print_archives') == [('unresolved',)]


@pytest.mark.parametrize('identity', ['equivalent_time', 'malformed'])
def test_physical_marker_identity_fails_closed(settlement_case, identity):
    from core.filament_ledger import prior_attempt_usage, _settlement_print_identity
    from core.print_attempts import sqlalchemy_runner
    case = settlement_case
    with case[1].begin() as db:
        run = sqlalchemy_runner(db)
        physical = _settlement_print_identity(run, 7099)
        physical[2] = '2026-10-11T14:00:00+02:00'
        marker = {'print_identity': physical} if identity == 'equivalent_time' else ['unreadable']
        db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),
                   {'key': 'filament_settlement:print:7099', 'value': json.dumps(marker)})
        assert prior_attempt_usage(run, None, 7099) == 'marker'


def test_job_usage_is_costed_on_only_one_archive(settlement_case):
    from modules.jobs.routes import jobs_lifecycle
    from modules.archives.archive import create_print_archive
    case = settlement_case
    case[0].app.include_router(jobs_lifecycle.router)
    write(case, "UPDATE jobs SET status='failed',quantity=1,hold=0 WHERE id=7099")
    assert case[0].post('/jobs/7099/complete').status_code == 200
    assert query(case, 'SELECT COUNT(*) FROM spool_usage')[0][0] > 0
    write(case, "INSERT INTO print_jobs(id,printer_id,scheduled_job_id,started_at,ended_at,status) VALUES(7102,9,7099,'2026-10-11 14:00:00','2026-10-11 15:00:00','failed')")
    second = create_print_archive(7102, 9, False)
    assert post(case, no_deduction='accept_existing').status_code == 200
    response = case[0].post(f'/archives/{second}/settle-filament', json={'no_deduction': 'accept_existing'})
    assert response.status_code == 200, response.text
    entries = [entry for (value,) in query(case, 'SELECT consumption_json FROM print_archives') for entry in json.loads(value)]
    assert sum(entry.get('grams_used') or 0 for entry in entries) == 80
    assert any(entry.get('reason') == 'usage_listed_on_another_archive' for entry in entries)


# Reuse the production outside-binding HTTP fixture, including recipe qualification.
from tests.test_contracts.test_outside_print_binding import outside, batch, shop, bind  # noqa: E402,F401


def test_complete_then_outside_bind_then_settle_never_deducts_twice(outside):
    from core.print_attempts import bind_print_attempt, sqlalchemy_runner
    from modules.jobs.routes import jobs_lifecycle
    from modules.archives.routes import router
    batch_case, pid, _ = outside
    client, user, _, db, *_ = batch_case
    client.app.include_router(jobs_lifecycle.router)
    client.app.include_router(router)
    db.execute(text('UPDATE printers SET is_active=TRUE WHERE id=301'))
    db.execute(text('UPDATE dispatch_attempt_receipts SET observed_print_job_id=NULL WHERE job_id=302'))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE scheduled_job_id=302'))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=302 WHERE id=:id'), {'id': pid})
    db.execute(text("UPDATE jobs SET status='failed',hold=FALSE,ams_mapping='[0]' WHERE id=302"))
    db.execute(text("INSERT INTO print_files(id,filename,model_id,org_id,filaments_json) VALUES(301,'fixture.3mf',301,1,:materials)"), {'materials': json.dumps([{'slot': 0, 'used_grams': 40}])})
    db.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(991240,'Fixture','PLA')"))
    db.execute(text("INSERT INTO spools(id,filament_id,remaining_weight_g,status,org_id) VALUES(991240,991240,1000,'active',1)"))
    db.execute(text('DELETE FROM filament_slots WHERE printer_id=301'))
    db.execute(text('INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id) VALUES(301,1,991240)'))
    bind_print_attempt(sqlalchemy_runner(db), pid, 302)
    db.commit()
    response = client.post('/jobs/302/complete')
    assert response.status_code == 200, response.text
    assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=991240')).scalar_one() == 960
    db.execute(text('UPDATE jobs SET hold=TRUE WHERE id=302'))
    aid = db.execute(text("INSERT INTO print_archives(print_job_id,job_id,printer_id,org_id,ownership_state,accounting_state,accounting_reason,completed_at) VALUES(:pid,302,301,1,'known','unresolved','no_bound_spool',CURRENT_TIMESTAMP) RETURNING id"), {'pid': pid}).scalar_one()
    db.commit()
    response = bind(outside)
    assert response.status_code == 200, response.text
    assert db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'), {'id': pid}).scalar_one() is None
    response = client.post(f'/archives/{aid}/settle-filament', json={'entries': [{'spool_id': 991240, 'grams': 40}]})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'already_deducted'
    assert client.post(f'/archives/{aid}/settle-filament', json={'no_deduction': 'accept_existing'}).status_code == 200
    assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=991240')).scalar_one() == 960


def test_invalid_balance_preview_refuses_in_plain_words(settlement_case):
    case = settlement_case
    write(case, 'UPDATE spools SET remaining_weight_g=:value WHERE id=7100', {'value': float('inf')})
    response = preview(case)
    assert response.status_code == 200, response.text
    assert not response.json()['settleable']
    assert 'invalid balance' in response.json()['not_settleable']
