"""Local SQLite HTTP and accounting regression tests for mark-print-failed.md."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from tests.test_contracts.test_job_outcome_correction import correction_client, snapshot
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db

URL = '/print-jobs/902'
BODY = {'reason': 'no_filament', 'note': 'Nothing fed to the nozzle.'}


@pytest.fixture
def client(correction_client, review_db):
    from modules.jobs.routes.print_failure import router
    from modules.jobs.routes.presets import router as reads
    from modules.archives.routes.archives_crud import router as archives
    app, issue = correction_client
    app.app.include_router(router)
    app.app.include_router(reads)
    app.app.include_router(archives)
    review_db.execute(text("UPDATE jobs SET status='cancelled',printer_id=9,actual_start='2026-11-14 10:00:00',actual_end='2026-11-14 11:00:00' WHERE id=901"))
    review_db.execute(text("UPDATE print_jobs SET status='cancelled',started_at='2026-11-14 10:00:00',ended_at='2026-11-14 11:00:00',filament_used_g=50,print_duration_seconds=3600 WHERE id=902"))
    review_db.execute(text("UPDATE print_archives SET status='cancelled',completed_at='2026-11-14 11:00:00',accounting_state='unresolved',accounting_reason='missing_or_invalid_start_snapshot' WHERE id=903"))
    review_db.commit()
    return app, issue


def tables(db):
    return {t: [tuple(r) for r in db.execute(text('SELECT * FROM ' + t + ' ORDER BY id'))]
            for t in ('jobs', 'print_jobs', 'print_archives', 'spools', 'spool_usage')}


def mark(client, body=None):
    c, issue = client
    return c.post(URL + '/mark-failed', headers=issue(['write:jobs']), json=BODY if body is None else body)


def undo(client):
    c, issue = client
    return c.post(URL + '/undo-failure', headers=issue(['write:jobs']))


def test_exact_restore_and_audit(client, review_db):
    before = tables(review_db)
    effects = snapshot(review_db)
    response = mark(client)
    assert response.status_code == 200, response.text
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'failed'
    assert tuple(review_db.execute(text('SELECT status,filament_used_g FROM print_jobs WHERE id=902')).one()) == ('failed', 50)
    assert tuple(review_db.execute(text('SELECT status,filament_used_grams,cost_estimate FROM print_archives WHERE id=903')).one()) == ('failed', 50, 4.25)
    assert snapshot(review_db) == effects  # no notifications, inventory or timeline delivery
    assert undo(client).status_code == 200
    assert tables(review_db) == before
    assert snapshot(review_db) == effects
    audits = review_db.execute(text("SELECT action,details FROM audit_logs WHERE entity_type='print_job' AND entity_id=902 ORDER BY id")).all()
    assert [a[0] for a in audits] == ['print.marked_failed', 'print.failure_undone']
    correction, reversal = [json.loads(a[1]) if isinstance(a[1], str) else a[1] for a in audits]
    assert correction['user_id'] == 3 and correction['by'] == 'admin' and correction['at']
    assert correction['reason'] == BODY['reason'] and correction['note'] == BODY['note']
    assert correction['before']['print']['status'] == 'cancelled'
    assert correction['after']['print']['status'] == 'failed'
    assert reversal['undone_by_id'] == 3 and reversal['undone_at']
    assert correction['before'] == reversal['before']


def test_correction_read_surfaces(client, review_db):
    c, issue = client
    assert mark(client).status_code == 200
    headers = issue(['read:jobs'])
    for path in (URL, '/archives/903'):
        response = c.get(path, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['failure_correction']['reason'] == BODY['reason']
        assert 'corrected_grams' not in response.json()['failure_correction']
    for path in ('/print-jobs', '/archives', '/archives/log'):
        response = c.get(path, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        rows = body if isinstance(body, list) else body['items']
        row = next(r for r in rows if r['id'] in (902, 903))
        assert row['failure_correction']['reason_label'] == 'No filament fed / printed air'
    assert undo(client).status_code == 200
    assert c.get(URL, headers=headers).json()['failure_correction'] is None


@pytest.mark.parametrize('body', [
    {}, {'reason': 'unknown'}, dict(BODY, note='a' * 1001), dict(BODY, note='<b>bad</b>'),
    dict(BODY, note='bad\x00'), dict(BODY, filament_used_g=-1), dict(BODY, filament_used_g=True),
    dict(BODY, filament_used_g='12'), dict(BODY, filament_used_g='NaN'),
    dict(BODY, no_material_used=True, filament_used_g=10), dict(BODY, extra='wrong'),
])
def test_invalid_input(client, review_db, body):
    before = tables(review_db)
    assert mark(client, body).status_code == 422
    assert tables(review_db) == before


@pytest.mark.parametrize('sql', [
    "UPDATE print_jobs SET status='completed' WHERE id=902",
    "UPDATE print_jobs SET status='running' WHERE id=902",
    "UPDATE print_jobs SET status='failed' WHERE id=902",
    "UPDATE print_jobs SET ended_at=NULL WHERE id=902",
    "UPDATE jobs SET status='printing' WHERE id=901",
    "UPDATE print_archives SET status='completed' WHERE id=903",
    "DELETE FROM print_archives WHERE id=903",
])
def test_ineligible_outcomes(client, review_db, sql):
    review_db.execute(text(sql)); review_db.commit()
    before = tables(review_db)
    assert mark(client).status_code == 409
    assert tables(review_db) == before


@pytest.mark.parametrize('state', ['pending', 'investigation'])
def test_open_restart_refused(client, review_db, state):
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('restart_question:9',:value)"), {'value': json.dumps({'state': state})})
    review_db.commit()
    assert mark(client).status_code == 409


def test_open_dispatch_refused(client, review_db):
    review_db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,owner_org_id,state,token,physical_key,configuration_key,payload_sha256) VALUES(9,901,1,'uncertain',:token,:key,:key,:key)"), {'token': 'a' * 32, 'key': 'b' * 64})
    review_db.commit()
    response = mark(client)
    assert response.status_code == 409, response.text


@pytest.mark.parametrize('role,org,scope,expected', [
    ('operator', 1, 'write:jobs', 200), ('admin', 1, 'write:jobs', 200),
    ('viewer', 1, 'write:jobs', 403), ('operator', 1, 'read:jobs', 403),
    ('operator', 2, 'write:jobs', 404), ('admin', 2, 'write:jobs', 404),
])
def test_permissions(client, review_db, role, org, scope, expected):
    c, issue = client
    review_db.execute(text('UPDATE users SET role=:role,group_id=:org WHERE id=3'), {'role': role, 'org': org}); review_db.commit()
    assert c.post(URL + '/mark-failed', headers=issue([scope]), json=BODY).status_code == expected


def test_archive_tenant_checked(client, review_db):
    review_db.execute(text('UPDATE print_archives SET org_id=2 WHERE id=903')); review_db.commit()
    assert mark(client).status_code == 404


def test_education_job_refused(client, review_db):
    # The base fixture's job 13 belongs to an Education submission.
    review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=13 WHERE id=902')); review_db.commit()
    assert mark(client).status_code in (403, 409)


def test_reserved_education_print_refused(client, review_db):
    review_db.execute(text("UPDATE print_jobs SET filename='odin-" + 'a' * 32 + ".3mf' WHERE id=902")); review_db.commit()
    assert mark(client).status_code == 409


def test_deduction_cannot_be_refunded_or_rededucted(client, review_db):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    result = record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', print_job_id=902, grams_by_spool={901: 50})
    assert result.deducted
    review_db.commit()
    before = tables(review_db)
    assert mark(client).status_code == 200
    assert undo(client).status_code == 200
    assert tables(review_db) == before


def test_multiple_filaments_outcome_only_and_exact_undo(client, review_db):
    consumption = json.dumps([{'spool_id': None, 'grams_used': 20}, {'spool_id': None, 'grams_used': 30}], indent=2)
    review_db.execute(text('UPDATE print_archives SET consumption_json=:value WHERE id=903'), {'value': consumption}); review_db.commit()
    before = tables(review_db)
    assert mark(client).status_code == 200
    assert review_db.execute(text('SELECT consumption_json FROM print_archives WHERE id=903')).scalar_one() == consumption
    assert undo(client).status_code == 200
    assert tables(review_db) == before
    assert mark(client).status_code == 200
    assert undo(client).status_code == 200
    assert tables(review_db) == before


def test_unknown_grams_preserved(client, review_db):
    review_db.execute(text('UPDATE print_jobs SET filament_used_g=NULL WHERE id=902'))
    review_db.execute(text('UPDATE print_archives SET filament_used_grams=NULL WHERE id=903')); review_db.commit()
    assert mark(client).status_code == 200
    assert undo(client).status_code == 200
    assert review_db.execute(text('SELECT filament_used_g FROM print_jobs WHERE id=902')).scalar_one() is None


def test_second_correction_undo_and_legacy_bypass(client, review_db):
    c, issue = client
    assert undo(client).status_code == 409
    assert mark(client).status_code == 200
    assert mark(client).status_code == 409
    response = c.post('/jobs/901/outcome', headers=issue(['write:jobs']), json={'from_status': 'failed', 'status': 'cancelled'})
    assert response.status_code == 409
    assert undo(client).status_code == 200
    assert undo(client).status_code == 409
    assert mark(client).status_code == 200


def test_concurrent_correction_has_one_winner(client, review_db):
    c, issue = client
    headers = issue(['write:jobs'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: c.post(URL + '/mark-failed', headers=headers, json=BODY), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='print.marked_failed'")).scalar_one() == 1


@pytest.mark.parametrize('sql', [
    'UPDATE print_jobs SET filament_used_g=49 WHERE id=902',
    'UPDATE print_archives SET filament_used_grams=49 WHERE id=903',
    "UPDATE jobs SET status='completed' WHERE id=901",
])
def test_undo_will_not_overwrite_changed_history(client, review_db, sql):
    assert mark(client).status_code == 200
    review_db.execute(text(sql)); review_db.commit()
    before = tables(review_db)
    if sql.startswith('UPDATE jobs'):
        # A job that moved on is preserved, while this print/archive can undo.
        assert undo(client).status_code == 200
        after = tables(review_db)
        assert after['jobs'] == before['jobs']
        assert after['spools'] == before['spools'] and after['spool_usage'] == before['spool_usage']
        assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == 'cancelled'
        assert review_db.execute(text('SELECT status FROM print_archives WHERE id=903')).scalar_one() == 'cancelled'
    else:
        response = undo(client)
        assert response.status_code == 200
        after = tables(review_db)
        # Only the outcomes and this correction's job metadata are restored;
        # neither table's subsequently edited material fields may be overwritten.
        for table in ('print_jobs', 'print_archives'):
            status_index = list(review_db.execute(text('SELECT * FROM ' + table)).keys()).index('status')
            assert [r[:status_index] + r[status_index + 1:] for r in after[table]] == [r[:status_index] + r[status_index + 1:] for r in before[table]]
        assert after['spools'] == before['spools'] and after['spool_usage'] == before['spool_usage']
        assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == 'cancelled'
        assert review_db.execute(text('SELECT status FROM print_archives WHERE id=903')).scalar_one() == 'cancelled'


def test_stats_printer_totals_day_counts_and_cost(client, review_db):
    from modules.printers.integration_floor import day_totals
    from modules.jobs.cost_actual import load_attempts
    c, issue = client
    headers = issue(['read:jobs'])
    actor = {'id': 3, 'role': 'admin', 'group_id': None}
    def totals():
        return day_totals(review_db, actor, None, False, [9], datetime(2026, 11, 14, 12, tzinfo=timezone.utc), timezone.utc)
    def stats():
        return next(row for row in c.get('/print-jobs/stats', headers=headers).json() if row['printer_id'] == 9)
    before = stats()
    printer = tuple(review_db.execute(text('SELECT total_print_count,total_print_hours FROM printers WHERE id=9')).one())
    assert totals()['plates_stopped'] == 1 and totals()['plates_failed'] == 0
    assert mark(client).status_code == 200
    assert stats()['failed_jobs'] == before['failed_jobs'] + 1
    assert stats()['total_jobs'] == before['total_jobs'] and stats()['total_hours'] == before['total_hours']
    assert tuple(review_db.execute(text('SELECT total_print_count,total_print_hours FROM printers WHERE id=9')).one()) == printer
    assert totals()['plates_stopped'] == 0 and totals()['plates_failed'] == 1
    attempts = load_attempts(review_db, [901])[901]
    assert attempts[0]['outcome'] == 'failed' and attempts[0]['usage'] is None
    assert undo(client).status_code == 200
    assert stats() == before and totals()['plates_stopped'] == 1 and totals()['plates_failed'] == 0
    assert load_attempts(review_db, [901])[901][0]['usage'] is None


@pytest.mark.parametrize('operation', [mark, undo])
def test_audit_failure_rolls_back_all_records(client, review_db, monkeypatch, operation):
    import modules.jobs.print_failure as service
    if operation is undo:
        assert mark(client).status_code == 200
    def broken(*args, **kwargs):
        raise RuntimeError('Synthetic audit failure')
    monkeypatch.setattr(service, 'log_audit', broken)
    before = tables(review_db)
    saved = service.record(review_db, 902)
    with pytest.raises(RuntimeError, match='Synthetic audit'):
        operation(client)
    assert tables(review_db) == before
    assert service.record(review_db, 902) == saved


@pytest.mark.parametrize('operation', [mark, undo])
def test_commit_failure_rolls_back_correction_provenance_and_audit(client, review_db, monkeypatch, operation):
    import modules.jobs.print_failure as service
    if operation is undo:
        assert mark(client).status_code == 200
    before = tables(review_db)
    saved = service.record(review_db, 902)
    audits = list(review_db.execute(text('SELECT id,action,details FROM audit_logs ORDER BY id')))
    def broken():
        raise RuntimeError('Synthetic commit failure')
    monkeypatch.setattr(review_db, 'commit', broken)
    with pytest.raises(RuntimeError, match='Synthetic commit'):
        operation(client)
    assert tables(review_db) == before
    assert service.record(review_db, 902) == saved
    assert list(review_db.execute(text('SELECT id,action,details FROM audit_logs ORDER BY id'))) == audits


def test_api_documents_both_operations(client):
    schema = client[0].app.openapi()
    for path in ('/print-jobs/{print_id}/mark-failed', '/print-jobs/{print_id}/undo-failure'):
        assert schema['paths'][path]['post']['description']
    body_schema = schema['components']['schemas']['MarkPrintFailed']
    assert body_schema['required'] == ['reason']
    assert set(body_schema['properties']) == {'reason', 'note'}
    assert body_schema['additionalProperties'] is False


@pytest.mark.parametrize('member,expected', [(False, 404), (True, 200)])
def test_controlled_part_audience(client, review_db, member, expected):
    from tests.test_contracts.test_home_assistant_status import _mark_controlled
    _mark_controlled(review_db, 901, member=member)
    review_db.commit()
    assert mark(client).status_code == expected


def test_shared_printer_does_not_disclose_correction_note(client, review_db):
    c, issue = client
    assert mark(client).status_code == 200
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=9'))
    review_db.execute(text('UPDATE users SET group_id=2 WHERE id=3')); review_db.commit()
    response = c.get(URL, headers=issue(['read:jobs']))
    assert response.status_code == 200  # existing shared-printer history visibility
    assert response.json()['failure_correction'] is None
    assert BODY['note'] not in response.text
    assert undo(client).status_code == 404


@pytest.mark.parametrize('value', ['broken', '[]', '{"material_plan": []}', '{"slots": [1]}'])
def test_malformed_material_record_does_not_block_outcome(client, review_db, value):
    review_db.execute(text('UPDATE print_jobs SET spool_snapshot=:value WHERE id=902'), {'value': value}); review_db.commit()
    before = tables(review_db)
    assert mark(client).status_code == 200
    assert undo(client).status_code == 200
    assert tables(review_db) == before


@pytest.mark.parametrize('linked', [True, False])
def test_success_rate_and_hours_follow_outcome(client, review_db, monkeypatch, linked):
    import asyncio
    from modules.reporting.routes import analytics
    monkeypatch.setattr(analytics, 'get_spoolman_url', lambda db: None)
    if not linked:
        review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=902'))
        review_db.execute(text('UPDATE print_archives SET job_id=NULL WHERE id=903'))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at,ended_at) VALUES(904,9,'completed','2026-11-14 08:00:00','2026-11-14 09:00:00')"))
    review_db.commit()
    def stats():
        review_db.expire_all()
        result = asyncio.run(analytics.get_stats(db=review_db, current_user={'id': 3, 'role': 'admin', 'group_id': 1}, days=365))
        return next(row for row in result['printer_stats'] if row['id'] == 9)
    before = stats()
    # Unlinking the print leaves the stopped job as a separate record in this
    # fixture. Both already count as did not complete before the correction.
    stopped = 1 if linked else 2
    assert before['completed_jobs'] == 1
    assert before['failed_jobs'] == 0
    assert before['stopped_jobs'] == stopped
    assert before['did_not_complete_jobs'] == stopped
    assert before['success_rate'] == (50 if linked else 33.3)
    assert before['total_hours'] == 1 + stopped
    assert mark(client).status_code == 200
    after = stats()
    assert after['failed_jobs'] == before['failed_jobs'] + 1
    assert after['stopped_jobs'] == before['stopped_jobs'] - 1
    assert after['completed_jobs'] == before['completed_jobs']
    assert after['did_not_complete_jobs'] == before['did_not_complete_jobs']
    assert after['success_rate'] == before['success_rate']
    assert after['total_hours'] == before['total_hours']
    assert undo(client).status_code == 200
    assert stats() == before


@pytest.mark.parametrize('measurement', ['weighed', 'ams'])
def test_measured_or_reconciled_spools_are_never_refunded(client, review_db, measurement):
    from core.filament_ledger import record_job_filament, record_weighed_spool, record_ams_measurement
    from core.print_attempts import sqlalchemy_runner
    run = sqlalchemy_runner(review_db)
    assert record_job_filament(run, 901, source='archive', print_job_id=902, grams_by_spool={901: 50}).deducted
    if measurement == 'weighed':
        record_weighed_spool(run, 901, 432.5)
    else:
        record_ams_measurement(run, 901, 420, 9)
    review_db.commit()
    before = tables(review_db)
    assert mark(client).status_code == 200
    assert undo(client).status_code == 200
    assert tables(review_db) == before


def test_cost_per_print_retains_material_and_total(client, review_db):
    from modules.jobs.cost_actual import cost_actual, load_attempts
    from tests.test_contracts.test_cost_actual import PRICING
    def cost():
        return cost_actual(load_attempts(review_db, [901])[901], PRICING)
    before = cost()
    assert before['material']['complete'] is False
    assert mark(client).status_code == 200
    after = cost()
    assert after['material'] == before['material']
    assert after['total'] == before['total']
    assert after['failed_attempts']['count'] == 1
    assert undo(client).status_code == 200
    assert cost() == before


def test_historical_attempt_does_not_change_completed_retry(client, review_db):
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=901"))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,ended_at) VALUES(904,9,901,'completed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    review_db.commit()
    before = tables(review_db)
    assert mark(client).status_code == 200
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'completed'
    assert undo(client).status_code == 200
    assert tables(review_db) == before


from tests.test_contracts.test_private_attempt_valuation_route import private_attempt, creators


def test_retained_measurement_is_untouched_by_outcome_correction(private_attempt, review_db):
    from modules.archives.measured_filament import retain_terminal
    from modules.jobs.routes.print_failure import router
    c, auth, _, identifier = private_attempt
    c.app.include_router(router)
    review_db.execute(text("UPDATE print_jobs SET status='cancelled',ended_at=CURRENT_TIMESTAMP,filament_used_g=12.5 WHERE id=:id"), {'id': identifier})
    job = review_db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'), {'id': identifier}).scalar_one()
    review_db.execute(text("UPDATE jobs SET status='cancelled' WHERE id=:id"), {'id': job})
    retain_terminal(review_db.connection(), identifier)
    row = review_db.execute(text('SELECT printer_id FROM print_jobs WHERE id=:id'), {'id': identifier}).one()
    review_db.execute(text("INSERT INTO print_archives(print_job_id,job_id,printer_id,status,ownership_state) VALUES(:id,:job,:printer,'cancelled','known')"), {'id': identifier, 'job': job, 'printer': row[0]})
    # Finish the fixture's synthetic transport before testing a metadata action.
    review_db.execute(text('DELETE FROM printer_dispatch_claims'))
    review_db.execute(text("UPDATE dispatch_sender_registrations SET state='quiesced',finished_at=CURRENT_TIMESTAMP,cleanup_evidence_key=:key"), {'key': 'a' * 64})
    review_db.commit()
    before = snapshot(review_db)
    response = c.post(f'/print-jobs/{identifier}/mark-failed', headers=auth('platform'), json=BODY)
    assert response.status_code == 200, response.text
    assert snapshot(review_db) == before
    assert c.post(f'/print-jobs/{identifier}/undo-failure', headers=auth('platform')).status_code == 200
    assert snapshot(review_db) == before


def test_home_assistant_feed_counts_the_day_the_print_ended(client, review_db, monkeypatch):
    from modules.printers import routes_integrations
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 11, 14, 12, tzinfo=timezone.utc)
            return value if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(routes_integrations, 'datetime', FrozenDatetime)
    c, issue = client
    c.app.include_router(routes_integrations.router)
    headers = issue(['read:jobs', 'read:printers'])
    def feed():
        response = c.get('/integrations/home-assistant/status', headers=headers)
        assert response.status_code == 200, response.text
        return response.json()['fleet']['today']
    before = feed()
    assert before['plates_stopped'] == 1
    assert mark(client).status_code == 200
    after = feed()
    assert after['plates_failed'] == before['plates_failed'] + 1
    assert after['plates_stopped'] == before['plates_stopped'] - 1
    assert undo(client).status_code == 200
    assert feed() == before
    # Correcting yesterday's print never increments today's counters.
    review_db.execute(text("UPDATE print_jobs SET started_at='2026-11-13 10:00:00',ended_at='2026-11-13 11:00:00' WHERE id=902"))
    review_db.commit()
    yesterday = feed()
    assert mark(client).status_code == 200
    assert feed() == yesterday

def test_maintenance_rate_keeps_stopped_print_in_total_after_correction(client, review_db):
    from modules.system.maintenance_status import failure_rates

    def counts():
        window = failure_rates(review_db, [9], datetime(2026, 11, 15, tzinfo=timezone.utc))[9]['last_30_days']
        return window['failed'], window['stopped'], window['did_not_complete']

    before = counts()
    assert mark(client).status_code == 200
    assert counts() == (before[0] + 1, before[1] - 1, before[2])
    assert undo(client).status_code == 200
    assert counts() == before
