"""Whole-change review regressions, using owned local SQLite databases."""
import csv
import io
import json

import pytest
from sqlalchemy import text

from tests.test_contracts.test_mark_print_failed import (
    client, correction_client, token_client, review_db, mark, undo, tables, BODY, URL,
)


@pytest.mark.parametrize('grams,cost', [(0, 0), (20, 1.7)])
def test_corrected_cost_on_archive_log_export_and_restore(client, review_db, monkeypatch, grams, cost):
    from modules.archives.routes import archives_crud
    monkeypatch.setattr(archives_crud, 'require_feature', lambda *a: None)
    before = tables(review_db)
    assert mark(client, dict(BODY, filament_used_g=grams)).status_code == 200
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
    assert record(review_db, 902)['corrected_grams'] == (40 if source == 'ledger' else 30)
    assert list(review_db.execute(text('SELECT * FROM spool_usage ORDER BY id'))) == before['spool_usage']
    assert undo(client).status_code == 200
    assert tables(review_db) == before


def test_archive_is_gram_source_when_print_disagrees(client, review_db):
    review_db.execute(text('UPDATE print_jobs SET filament_used_g=0 WHERE id=902'))
    review_db.commit()
    before = tables(review_db)
    response = mark(client, dict(BODY, filament_used_g=20))
    assert response.status_code == 200, response.text
    from modules.jobs.print_failure import record
    assert record(review_db, 902)['original_grams'] == 50
    assert undo(client).status_code == 200
    assert tables(review_db) == before


@pytest.mark.parametrize('change', ['accounted', 'consumption', 'ledger', 'print_grams', 'archive_grams',
                                  'archive_cost', 'archive_deleted', 'archive_added', 'late_status',
                                  'job_deleted', 'job_requeued', 'job_retry_failed', 'job_completed',
                                  'job_notes', 'job_timestamp', 'job_reason', 'open_restart', 'late_start', 'snapshot'])
def test_undo_has_a_way_back_after_later_changes(client, review_db, change):
    assert mark(client, dict(BODY, no_material_used=True)).status_code == 200
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
    assert response.status_code == 200, response.text
    assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == 'cancelled'
    assert all(s == 'cancelled' for s in review_db.execute(text('SELECT status FROM print_archives WHERE print_job_id=902')).scalars())
    after = tables(review_db)
    assert after['spools'] == before['spools'] and after['spool_usage'] == before['spool_usage']
    if change in ('job_deleted', 'job_requeued', 'job_retry_failed', 'job_completed'):
        assert after['jobs'] == before['jobs']
    else:
        assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'cancelled'
    if change in ('job_notes', 'job_timestamp', 'job_reason'):
        field = {'job_notes': 'fail_notes', 'job_timestamp': 'updated_at', 'job_reason': 'fail_reason'}[change]
        assert review_db.execute(text('SELECT ' + field + ' FROM jobs WHERE id=901')).scalar_one() == job_before[field]
    if change not in ('late_status', 'late_start', 'open_restart'):
        assert response.json()['warnings'], 'Partial undo must explain preserved later work'
    if change in ('accounted', 'consumption', 'ledger', 'print_grams', 'archive_grams', 'archive_cost', 'archive_added'):
        # A material change is preserved as a unit; no new deductions or guesses.
        assert review_db.execute(text('SELECT filament_used_g FROM print_jobs WHERE id=902')).scalar_one() == print_before
        for archive_before, archive_after in zip(before['print_archives'], after['print_archives']):
            index = list(review_db.execute(text('SELECT * FROM print_archives')).keys()).index('status')
            assert archive_before[:index] + archive_before[index + 1:] == archive_after[:index] + archive_after[index + 1:]
    assert undo(client).status_code == 409


@pytest.mark.parametrize('grams,expected', [(0, [0, 0]), (25, [10, 15]), (12.5, [5, 7.5])])
def test_multiple_unattributed_filaments_spread_and_restore(client, review_db, grams, expected):
    entries = [{'spool_id': None, 'grams_used': 20}, {'spool_id': None, 'grams_used': 30}]
    review_db.execute(text('UPDATE print_archives SET consumption_json=:entries WHERE id=903'), {'entries': json.dumps(entries, indent=2)})
    review_db.commit()
    before = tables(review_db)
    response = mark(client, dict(BODY, filament_used_g=grams))
    assert response.status_code == 200, response.text
    actual = json.loads(review_db.execute(text('SELECT consumption_json FROM print_archives WHERE id=903')).scalar_one())
    assert [e['grams_used'] for e in actual] == expected
    assert all(e['quantity_provenance'] == 'operator_corrected' for e in actual)
    assert undo(client).status_code == 200
    assert tables(review_db) == before


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


@pytest.mark.parametrize('entries', [[{'grams_used': None}, {'grams_used': 30}],
                                    [{'grams_used': -1}, {'grams_used': 30}],
                                    [{'grams_used': 0}, {'grams_used': 0}]])
def test_unspreadable_filament_weights_refused_with_reason(client, review_db, entries):
    review_db.execute(text('UPDATE print_archives SET consumption_json=:entries WHERE id=903'), {'entries': json.dumps(entries)})
    review_db.commit()
    before = tables(review_db)
    response = mark(client, dict(BODY, filament_used_g=25))
    assert response.status_code == 409 and 'proportion' in response.json()['detail']
    assert tables(review_db) == before


@pytest.mark.parametrize('weights', [[None, 30], [0, 0]])
def test_zero_total_needs_no_per_filament_ratio(client, review_db, weights):
    entries = [{'spool_id': None, 'grams_used': weight} for weight in weights]
    review_db.execute(text('UPDATE print_archives SET consumption_json=:entries WHERE id=903'), {'entries': json.dumps(entries)})
    review_db.commit()
    before = tables(review_db)
    response = mark(client, dict(BODY, no_material_used=True))
    assert response.status_code == 200, response.text
    assert [e['grams_used'] for e in json.loads(review_db.execute(text('SELECT consumption_json FROM print_archives WHERE id=903')).scalar_one())] == [0, 0]
    assert undo(client).status_code == 200
    assert tables(review_db) == before


def test_positive_total_without_per_filament_amounts_explains_refusal(client, review_db):
    snapshot = {'material_plan': {'allocations': [{'spool_id': None}, {'spool_id': None}]}}
    review_db.execute(text('UPDATE print_jobs SET spool_snapshot=:value WHERE id=902'), {'value': json.dumps(snapshot)})
    review_db.commit()
    before = tables(review_db)
    response = mark(client, dict(BODY, filament_used_g=25))
    assert response.status_code == 409 and 'per-filament recorded amounts are missing' in response.json()['detail']
    assert tables(review_db) == before


def test_job_level_deduction_message_does_not_claim_this_print_consumed_it(client, review_db):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', grams_by_spool={901: 30}).deducted
    review_db.commit()
    before = tables(review_db)
    response = mark(client, dict(BODY, no_material_used=True))
    assert response.status_code == 409
    assert 'attempt-level' in response.json()['detail'] and 'cannot be attributed' in response.json()['detail']
    assert tables(review_db) == before


def test_distinct_earlier_print_deduction_does_not_block_correction(client, review_db):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,ended_at) VALUES(900,9,901,'failed','2026-11-13 10:00:00','2026-11-13 11:00:00')"))
    assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', print_job_id=900, grams_by_spool={901: 30}).deducted
    review_db.commit()
    before = tables(review_db)
    assert mark(client, dict(BODY, no_material_used=True)).status_code == 200
    assert undo(client).status_code == 200
    assert tables(review_db) == before


def test_nonzero_corrected_grams_reach_cost_breakdown_without_inventing_price(client, review_db):
    from modules.jobs.cost_actual import cost_actual, load_attempts
    from tests.test_contracts.test_cost_actual import PRICING
    assert mark(client, dict(BODY, filament_used_g=20)).status_code == 200
    result = cost_actual(load_attempts(review_db, [901])[901], PRICING)
    assert result['material']['spools'][0]['grams'] == 20
    assert result['material']['complete'] is False  # no purchase price exists for unattributed use


def test_late_accounting_takes_precedence_over_zero_correction(client, review_db):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    from modules.jobs.cost_actual import load_attempts
    assert mark(client, dict(BODY, no_material_used=True)).status_code == 200
    assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', print_job_id=902, grams_by_spool={901: 30}).deducted
    review_db.commit()
    assert load_attempts(review_db, [901])[901][0]['usage'][0]['grams'] == 30
    response = undo(client)
    assert response.status_code == 200 and response.json()['warnings']
    assert load_attempts(review_db, [901])[901][0]['usage'][0]['grams'] == 30


def test_new_job_workflow_reports_preserved_outcome_without_stranding_print(client, review_db, monkeypatch):
    from fastapi import HTTPException
    from modules.jobs import print_failure
    assert mark(client).status_code == 200
    def refuse(*args):
        raise HTTPException(409, 'The job now has an Education workflow')
    monkeypatch.setattr(print_failure, 'assert_generic_job_mutation_allowed', refuse)
    response = undo(client)
    assert response.status_code == 200
    assert 'workflow' in response.json()['warnings'][0]
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'failed'
    assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == 'cancelled'


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


def test_explicit_zero_clears_stale_cost_when_grams_already_zero(client, review_db):
    review_db.execute(text('UPDATE print_jobs SET filament_used_g=0 WHERE id=902'))
    review_db.execute(text('UPDATE print_archives SET filament_used_grams=0 WHERE id=903'))
    review_db.commit()
    before = tables(review_db)
    assert mark(client, dict(BODY, no_material_used=True)).status_code == 200
    assert review_db.execute(text('SELECT cost_estimate FROM print_archives WHERE id=903')).scalar_one() == 0
    assert undo(client).status_code == 200
    assert tables(review_db) == before


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
