"""Whole-change review regressions, using owned local SQLite databases."""
import csv
import io
import json

import pytest
from sqlalchemy import text

from tests.test_contracts.test_mark_print_failed import (
    client, correction_client, token_client, review_db, mark, undo, tables, BODY, URL,
)


def test_unchanged_cost_on_archive_log_export_and_restore(client, review_db, monkeypatch):
    grams, cost = 50, 4.25
    from modules.archives.routes import archives_crud
    monkeypatch.setattr(archives_crud, 'require_feature', lambda *a: None)
    before = tables(review_db)
    assert mark(client).status_code == 200
    c, issue = client
    headers = issue(['read:jobs'])
    for path in ('/archives/903', '/archives/log', '/archives'):
        response = c.get(path, headers=headers)
        assert response.status_code == 200
        data = response.json()
        row = data if path == '/archives/903' else next(r for r in data['items'] if r['id'] == 903)
        assert row['filament_used_grams'] == grams
        assert row['cost_estimate'] == cost
    response = c.get('/archives/log/export', headers=headers)
    assert response.status_code == 200
    row = next(csv.DictReader(io.StringIO(response.text)))
    assert float(row['Filament (g)']) == grams and float(row['Cost']) == cost
    assert undo(client).status_code == 200
    assert tables(review_db) == before


@pytest.mark.parametrize('source', ['archive', 'ledger', 'both'])
def test_outcome_only_preserves_disagreeing_usage(client, review_db, source):
    from modules.jobs.cost_actual import load_attempts
    if source in ('ledger', 'both'):
        from core.filament_ledger import record_job_filament
        from core.print_attempts import sqlalchemy_runner
        assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', print_job_id=902, grams_by_spool={901: 40}).deducted
    if source in ('archive', 'both'):
        review_db.execute(text("UPDATE print_archives SET accounting_state='accounted',consumption_json=:entries,filament_used_grams=30 WHERE id=903"),
                          {'entries': json.dumps([{'spool_id': 901, 'grams_used': 30}])})
    review_db.execute(text('UPDATE print_jobs SET filament_used_g=0 WHERE id=902'))
    review_db.commit()
    before = tables(review_db)
    usage = load_attempts(review_db, [901])[901][0]['usage']
    assert mark(client).status_code == 200
    assert load_attempts(review_db, [901])[901][0]['usage'] == usage
    from modules.jobs.print_failure import record
    assert 'corrected_grams' not in record(review_db, 902)
    assert list(review_db.execute(text('SELECT * FROM spool_usage ORDER BY id'))) == before['spool_usage']
    assert undo(client).status_code == 200
    assert tables(review_db) == before


@pytest.mark.parametrize('change', ['accounted', 'consumption', 'ledger', 'print_grams', 'archive_grams',
                                  'archive_cost', 'archive_deleted', 'archive_added', 'late_status',
                                  'job_deleted', 'job_requeued', 'job_retry_failed', 'job_completed',
                                  'job_notes', 'job_timestamp', 'job_reason', 'open_restart', 'late_start', 'snapshot'])
def test_undo_has_a_way_back_after_later_changes(client, review_db, change):
    assert mark(client).status_code == 200
    statements = {
        'accounted': "UPDATE print_archives SET accounting_state='accounted' WHERE id=903",
        'consumption': "UPDATE print_archives SET consumption_json='[{\"spool_id\": 901,\"grams_used\": 30}]' WHERE id=903",
        'print_grams': 'UPDATE print_jobs SET filament_used_g=30 WHERE id=902',
        'archive_grams': 'UPDATE print_archives SET filament_used_grams=30 WHERE id=903',
        'archive_cost': 'UPDATE print_archives SET cost_estimate=10 WHERE id=903',
        'archive_deleted': 'DELETE FROM print_archives WHERE id=903',
        'archive_added': "INSERT INTO print_archives(id,print_job_id,printer_id,status,filament_used_grams,ownership_state,org_id) VALUES(904,902,9,'failed',30,'known',1)",
        'late_status': "UPDATE print_jobs SET status='completed',ended_at='2026-11-14 11:00:03' WHERE id=902",
        'job_deleted': 'DELETE FROM jobs WHERE id=901',
        'job_requeued': "UPDATE jobs SET status='pending' WHERE id=901",
        'job_retry_failed': "UPDATE jobs SET status='failed' WHERE id=901",
        'job_completed': "UPDATE jobs SET status='completed' WHERE id=901",
        'job_notes': "UPDATE jobs SET fail_notes='Later operator note' WHERE id=901",
        'job_timestamp': "UPDATE jobs SET updated_at='2026-11-15 12:00:00' WHERE id=901",
        'job_reason': "UPDATE jobs SET fail_reason='Later reason' WHERE id=901",
        'open_restart': "INSERT INTO system_config(key,value) VALUES('restart_question:9','{\"state\":\"pending\"}')",
        'late_start': "UPDATE print_jobs SET started_at='2026-11-14 10:00:03' WHERE id=902",
        'snapshot': "UPDATE print_jobs SET spool_snapshot='{\"slots\":[{\"spool_id\": 901}]}' WHERE id=902",
    }
    if change == 'ledger':
        from core.filament_ledger import record_job_filament
        from core.print_attempts import sqlalchemy_runner
        assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', print_job_id=902, grams_by_spool={901: 30}).deducted
    else:
        review_db.execute(text(statements[change]))
    if change == 'job_retry_failed':
        from core.print_attempts import bump_print_attempt, sqlalchemy_runner
        bump_print_attempt(sqlalchemy_runner(review_db), 901)
    review_db.commit()
    before = tables(review_db)
    job_before = review_db.execute(text('SELECT * FROM jobs WHERE id=901')).mappings().first()
    print_before = review_db.execute(text('SELECT filament_used_g FROM print_jobs WHERE id=902')).scalar_one()
    response = undo(client)
    if change == 'late_start':
        assert response.status_code == 409
        assert tables(review_db) == before
        return
    assert response.status_code == 200, response.text
    assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == ('completed' if change == 'late_status' else 'cancelled')
    assert dict(review_db.execute(text('SELECT id,status FROM print_archives WHERE print_job_id=902')).all()) == ({} if change == 'archive_deleted' else {903: 'cancelled', 904: 'failed'} if change == 'archive_added' else {903: 'cancelled'})
    after = tables(review_db)
    assert after['spools'] == before['spools'] and after['spool_usage'] == before['spool_usage']
    if change in ('job_deleted', 'job_requeued', 'job_retry_failed', 'job_completed'):
        assert after['jobs'] == before['jobs']
    else:
        assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'cancelled'
    if change in ('job_notes', 'job_timestamp', 'job_reason'):
        field = {'job_notes': 'fail_notes', 'job_timestamp': 'updated_at', 'job_reason': 'fail_reason'}[change]
        assert review_db.execute(text('SELECT ' + field + ' FROM jobs WHERE id=901')).scalar_one() == job_before[field]
    if change in ('late_status', 'archive_deleted', 'archive_added', 'job_deleted', 'job_requeued', 'job_retry_failed', 'job_completed', 'job_notes', 'job_timestamp', 'job_reason'):
        assert response.json()['warnings'], 'Partial undo must explain preserved later work'
    if change in ('accounted', 'consumption', 'ledger', 'print_grams', 'archive_grams', 'archive_cost', 'archive_added'):
        # A material change is preserved as a unit; no new deductions or guesses.
        assert review_db.execute(text('SELECT filament_used_g FROM print_jobs WHERE id=902')).scalar_one() == print_before
        for archive_before, archive_after in zip(before['print_archives'], after['print_archives']):
            index = list(review_db.execute(text('SELECT * FROM print_archives')).keys()).index('status')
            assert archive_before[:index] + archive_before[index + 1:] == archive_after[:index] + archive_after[index + 1:]
    assert undo(client).status_code == 409


@pytest.mark.parametrize('predicate', ['NULL', 'a.org_id = NULL'])
def test_null_archive_audience_cannot_disclose_note(client, review_db, monkeypatch, predicate):
    from modules.archives import services
    from modules.jobs.print_failure import annotate
    assert mark(client).status_code == 200
    monkeypatch.setattr(services, 'archive_visibility', lambda user: (predicate, {}))
    row = dict(review_db.execute(text('SELECT * FROM print_jobs WHERE id=902')).mappings().one())
    assert annotate(review_db, [row], user={'id': 3, 'role': 'admin', 'group_id': 1})[0]['failure_correction'] is None


def test_null_education_predicate_refuses_mutation(client, monkeypatch):
    from core.interfaces import education_policy
    monkeypatch.setattr(education_policy, 'generic_archive_audience_sql', lambda: 'NULL')
    assert mark(client).status_code == 409


def test_crlf_plain_note_normalized(client, review_db):
    assert mark(client, dict(BODY, note='First line\r\nSecond line')).status_code == 200
    from modules.jobs.print_failure import record
    assert record(review_db, 902)['note'] == 'First line\nSecond line'


@pytest.mark.parametrize('role,org,expected', [('admin', None, 200), ('custom', 1, 403)])
def test_superadmin_and_custom_jobs_edit_role(client, review_db, role, org, expected):
    review_db.execute(text('UPDATE users SET role=:role,group_id=:org WHERE id=3'), {'role': role, 'org': org})
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('rbac_permissions',:value)"),
                      {'value': json.dumps({'action_access': {'jobs.edit': ['custom']}})})
    review_db.commit()
    assert mark(client).status_code == expected


def test_distinct_earlier_print_deduction_does_not_block_correction(client, review_db):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,ended_at) VALUES(900,9,901,'failed','2026-11-13 10:00:00','2026-11-13 11:00:00')"))
    assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', print_job_id=900, grams_by_spool={901: 30}).deducted
    review_db.commit()
    before = tables(review_db)
    assert mark(client).status_code == 200
    assert undo(client).status_code == 200
    assert tables(review_db) == before


def test_late_accounting_is_preserved_by_undo(client, review_db):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    from modules.jobs.cost_actual import load_attempts
    assert mark(client).status_code == 200
    assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', print_job_id=902, grams_by_spool={901: 30}).deducted
    review_db.commit()
    assert load_attempts(review_db, [901])[901][0]['usage'][0]['grams'] == 30
    response = undo(client)
    assert response.status_code == 200
    assert load_attempts(review_db, [901])[901][0]['usage'][0]['grams'] == 30


def test_new_education_workflow_refuses_undo(client, review_db, monkeypatch):
    from fastapi import HTTPException
    from modules.jobs import print_failure
    assert mark(client).status_code == 200
    def refuse(*args):
        raise HTTPException(409, 'The job now has an Education workflow')
    monkeypatch.setattr(print_failure, 'assert_generic_job_mutation_allowed', refuse)
    response = undo(client)
    assert response.status_code == 409
    assert 'Education workflow' in response.json()['detail']
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'failed'
    assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == 'failed'


def test_correction_namespace_is_not_attempt_data_or_shared_diagnostics(client, review_db, monkeypatch):
    import asyncio
    import subprocess
    import zipfile
    from core.print_attempts import attempt_seq, attempt_starts, sqlalchemy_runner
    from modules.system import diagnostics, routes_admin
    assert mark(client).status_code == 200
    run = sqlalchemy_runner(review_db)
    assert attempt_seq(run, 901) == 0 and attempt_starts(run, 901) == {}
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: 'Synthetic test')
    monkeypatch.setattr(diagnostics, '_read_tail', lambda *a: [])
    response = routes_admin._download_support_bundle(user={'id': 3, 'role': 'admin', 'group_id': None}, db=review_db)
    async def body():
        return b''.join([chunk async for chunk in response.body_iterator])
    with zipfile.ZipFile(io.BytesIO(asyncio.run(body()))) as bundle:
        settings = json.loads(bundle.read('settings_safe.json'))
        assert not any(key.startswith('quota_attempt:') for key in settings)
        assert BODY['note'] not in json.dumps(settings)


def test_deleted_print_correction_does_not_attach_to_new_print(client, review_db):
    from modules.jobs.print_failure import record
    from core.economic_source_creation import _create_attempt
    assert mark(client).status_code == 200
    review_db.execute(text('DELETE FROM print_jobs WHERE id=902'))
    # Use the real print factory so the replacement gets its own lifetime.
    created = _create_attempt(review_db, {'printer_id': 9, 'scheduled_job_id': 901, 'status': 'running', 'started_at': '2026-11-14 10:00:00'})
    review_db.execute(text("UPDATE print_jobs SET status='cancelled',ended_at='2026-11-14 11:00:00' WHERE id=:id"), {'id': created.id})
    review_db.commit()
    assert record(review_db, 902) is None
    assert record(review_db, created.id) is None
    assert undo(client).status_code == 404
    c, issue = client
    assert c.post(f'/print-jobs/{created.id}/undo-failure', headers=issue(['write:jobs'])).status_code == 409


@pytest.mark.parametrize('role,org,expected', [('operator', 1, True), ('admin', 1, True),
                                            ('admin', None, True), ('custom', 1, False),
                                            ('viewer', 1, False), ('operator', 2, False)])
def test_read_payload_gives_ui_the_same_role_and_tenant_boundary(client, review_db, role, org, expected):
    from modules.jobs.print_failure import annotate
    row = dict(review_db.execute(text('SELECT * FROM print_jobs WHERE id=902')).mappings().one())
    result = annotate(review_db, [row], user={'id': 3, 'role': role, 'group_id': org})[0]
    assert result['failure_correction_allowed'] is expected


def test_actual_unassigned_reader_null_audience_fails_closed(client, review_db):
    from modules.archives.services import archive_visibility
    from modules.jobs.print_failure import annotate
    # Printer/job are globally visible, while retained archive ownership
    # remains org 1. A viewer with no org produces SQL UNKNOWN, not FALSE.
    review_db.execute(text("INSERT INTO printers(id,name) VALUES(9000,'Global synthetic printer')"))
    review_db.execute(text('UPDATE print_jobs SET printer_id=9000 WHERE id=902'))
    review_db.execute(text('UPDATE print_archives SET printer_id=9000 WHERE id=903'))
    review_db.execute(text('UPDATE jobs SET charged_to_org_id=NULL WHERE id=901'))
    review_db.commit()
    assert mark(client).status_code == 200
    user = {'id': 3, 'role': 'viewer', 'group_id': None}
    predicate, params = archive_visibility(user)
    assert review_db.execute(text('SELECT (' + predicate + ') FROM print_archives a WHERE a.id=903'), params).scalar_one() is None
    row = dict(review_db.execute(text('SELECT * FROM print_jobs WHERE id=902')).mappings().one())
    result = annotate(review_db, [row], user=user)[0]
    assert result['failure_correction'] is None and result['failure_correction_allowed'] is False
