"""Regression requests for settlement access, payload and validation review."""
import json
import pytest
from tests.test_contracts.test_archive_filament_settlement import (
    settlement_case, archive_case, review_db, post, preview, query, write, manual,
)


@pytest.mark.parametrize('surface', ['detail', 'list', 'compare', 'summary'])
def test_prior_usage_cause_on_every_surface(settlement_case, surface):
    case = settlement_case
    write(case, "INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g,notes) VALUES(7099,7099,7099,40,'auto:moonraker')")
    write(case, 'UPDATE print_archives SET completed_at=CURRENT_TIMESTAMP')
    path = {'detail': f'/archives/{case[2]}', 'list': '/archives', 'compare': f'/archives/compare?a={case[2]}&b={case[2]}', 'summary': '/archives/accounting-summary'}[surface]
    result = case[0].get(path)
    assert result.status_code == 200, result.text
    assert 'Filament was already deducted another way' in result.text
    assert 'No spool was bound' not in result.text


@pytest.mark.parametrize('change', ['import', 'education_snapshot', 'records', 'missing_print', 'reconciliation', 'accounted'])
def test_unactionable_archives_not_counted_or_actionable(settlement_case, change):
    case = settlement_case
    case[3].update(role='admin', group_id=None, is_superadmin=True)
    write(case, 'UPDATE print_archives SET completed_at=CURRENT_TIMESTAMP')
    if change == 'education_snapshot':
        snapshot = json.loads(query(case, 'SELECT spool_snapshot FROM print_jobs WHERE id=7099')[0][0])
        snapshot['content_owner']['education_submission_id'] = 42
        write(case, 'UPDATE print_jobs SET spool_snapshot=:snapshot WHERE id=7099', {'snapshot': json.dumps(snapshot)})
    else:
        clause = {'import': "ownership_source='project_import'", 'records': "ownership_state='unresolved'", 'missing_print': 'print_job_id=NULL', 'reconciliation': "accounting_state='reconciliation_required'", 'accounted': "accounting_state='accounted',print_job_id=NULL"}[change]
        write(case, 'UPDATE print_archives SET ' + clause)
    assert not preview(case).json()['settleable']
    assert case[0].get('/archives/accounting-summary').json()['unresolved'] == 0
    assert case[0].get('/archives?accounting=unresolved').json()['total'] == 0
    detail = case[0].get(f'/archives/{case[2]}').json()
    assert detail['settleable'] is False
    if change == 'accounted':
        assert detail['accounting_badge'] is None
    assert 'Settle this print' not in detail['accounting_cause']['next_step']


@pytest.mark.parametrize('scopes,status', [(['read:jobs'], 403), (['read:spools'], 403), (['read:jobs','read:spools'], 200), (['write'], 403)])
def test_preview_requires_both_read_scopes(settlement_case, scopes, status):
    case = settlement_case
    case[3].update(_auth_kind='user_api_token', _token_scopes=scopes, role='viewer')
    assert preview(case).status_code == status


@pytest.mark.parametrize('snapshot', [{'content_owner': None}, {'slots': [None]}, {'slots': 'broken'}, {'material_plan': []}, {'material_plan': {'allocations': [2]}}])
def test_malformed_snapshot_refuses_without_crash(settlement_case, snapshot):
    case = settlement_case
    write(case, 'UPDATE print_jobs SET spool_snapshot=:snapshot WHERE id=7099', {'snapshot': json.dumps(snapshot)})
    response = preview(case)
    assert response.status_code == 200, response.text
    assert not response.json()['settleable']
    assert post(case, no_deduction='nothing_extruded').status_code == 409


@pytest.mark.parametrize('body', [dict(entries=[dict(spool_id=7099, grams=True)]), dict(counted_usage_ids=list(range(1,258))), dict(separate_usage_ids=list(range(1,258))), dict(deduct_despite_later_balance=list(range(1,18)))])
def test_strict_grams_and_bounded_lists(settlement_case, body):
    response = post(settlement_case, **body)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize('kind', ['manual', 'existing'])
def test_unreadable_usage_blocks_settlement(settlement_case, kind):
    case = settlement_case
    if kind == 'manual':
        manual(case)
    else:
        write(case, "INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g,notes) VALUES(7099,7099,7099,40,'auto:moonraker')")
    write(case, 'UPDATE spools SET org_id=2 WHERE id=7099')
    assert not preview(case).json()['settleable']
    response = post(case, no_deduction='accept_existing' if kind == 'existing' else 'nothing_extruded')
    assert response.status_code == 409, response.text
    assert 'Someone with access to it must settle this print' in response.json()['detail']['message']


def test_actor_ids_private_and_legacy_auth_attributed(settlement_case):
    case = settlement_case
    case[3].update(_auth_kind='legacy_global_api_key', _token_id=None)
    assert post(case, no_deduction='nothing_extruded').status_code == 200
    raw = json.loads(query(case, 'SELECT consumption_json FROM print_archives')[0][0])[0]
    assert raw['settled_auth_kind'] == 'legacy_global_api_key'
    detail = case[0].get(f'/archives/{case[2]}').json()
    assert 'settled_by_user_id' not in json.dumps(detail)
    assert 'settled_by_token_id' not in json.dumps(detail)
    assert detail['accounting_settled']['by'] == 'Global API key'
    audit = json.loads(query(case, "SELECT details FROM audit_logs WHERE action='archive.filament_settled'")[0][0])
    assert audit['actor_auth_kind'] == 'legacy_global_api_key'


def test_prior_usage_allows_truthful_counted_manual_review(settlement_case):
    case = settlement_case
    row = manual(case)
    write(case, "INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g,notes) VALUES(7099,7099,7099,40,'auto:moonraker')")
    response = post(case, no_deduction='accept_existing', counted_usage_ids=[row])
    assert response.status_code == 200, response.text
    assert query(case, 'SELECT COUNT(*) FROM spool_usage') == [(2,)]
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(990,)]


@pytest.mark.parametrize('body,phrase', [
    ({'no_deduction': 'nothing_extruded', 'entries': [{'spool_id': 7099, 'grams': 1}]}, 'Remove the spool entries'),
    ({'no_deduction': 'nothing_extruded', 'separate_usage_ids': [999]}, 'no longer available'),
    ({'entries': [{'spool_id': 7101, 'grams': 1}]}, 'Choose a spool'),
    ({'entries': [{'spool_id': 7099, 'grams': True}]}, 'Grams must be numbers'),
])
def test_refusals_have_one_shape_and_action(settlement_case, body, phrase):
    response = post(settlement_case, **body)
    assert response.status_code in (404, 409, 422), response.text
    assert response.json()['detail']['code']
    assert phrase in response.json()['detail']['message']


def test_known_education_staff_cannot_settle_or_count(settlement_case):
    case = settlement_case
    case[3].update(role='admin')
    write(case, "UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'")
    write(case, 'UPDATE print_archives SET job_id=13,print_file_id=11,education_submission_id=14,completed_at=CURRENT_TIMESTAMP')
    detail = case[0].get(f'/archives/{case[2]}')
    assert detail.status_code == 200, detail.text
    assert detail.json()['ownership_state'] == 'known'
    assert detail.json()['accounting_badge'] is None
    assert not preview(case).json()['settleable']
    assert 'Education submission' in detail.json()['accounting_cause']['title']
    assert case[0].get('/archives/accounting-summary').json()['unresolved'] == 0
    assert post(case, no_deduction='nothing_extruded').status_code == 409


def test_shared_archive_uses_printer_organisation_spool(settlement_case):
    write(settlement_case, 'UPDATE print_archives SET org_id=NULL')
    assert post(settlement_case, entries=[dict(spool_id=7099, grams=40)]).status_code == 200


def test_actor_name_hidden_across_organisations(settlement_case):
    case = settlement_case
    case[3].update(role='admin', group_id=None, is_superadmin=True, id=5)
    assert post(case, no_deduction='nothing_extruded').status_code == 200
    case[3].update(role='viewer', group_id=1, is_superadmin=False, id=3)
    detail = case[0].get(f'/archives/{case[2]}').json()
    assert detail['accounting_settled']['by'] is None
    assert 'settled_by_user_id' not in json.dumps(detail)


def test_batch_causes_match_ledger_without_per_archive_queries(settlement_case):
    from sqlalchemy import event, text
    from sqlalchemy.orm import Session
    from modules.archives.settlement_data import archive_statuses
    case = settlement_case
    calls = []
    with Session(case[1]) as db:
        row = dict(db.execute(text('SELECT * FROM print_archives')).mappings().one())
        def capture(*args): calls.append(args[2])
        event.listen(case[1], 'before_cursor_execute', capture)
        try:
            archive_statuses(db, [row])
            single = len(calls)
            calls.clear()
            archive_statuses(db, [dict(row, id=i) for i in range(50)])
            assert len(calls) == single
        finally:
            event.remove(case[1], 'before_cursor_execute', capture)


def test_generic_write_token_and_viewer_preview(settlement_case):
    case = settlement_case
    case[3].update(role='viewer')
    assert preview(case).status_code == 200
    assert post(case, no_deduction='nothing_extruded').status_code == 403
    case[3].update(role='operator', _auth_kind='user_api_token', _token_id=77, _token_scopes=['write'])
    assert preview(case).status_code == 403
    assert post(case, no_deduction='nothing_extruded').status_code == 200


def test_controlled_archive_filtered_from_summary_preview_and_write(settlement_case, tier_license):
    from sqlalchemy.orm import Session
    from modules.organizations.routes_controlled_parts import change_mark
    from tests.test_contracts.test_controlled_parts import mark
    case = settlement_case
    tier_license('enterprise')
    write(case, "INSERT INTO controlled_audiences(id,name) VALUES(1,'Settlement secure')")
    write(case, 'INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)')
    case[3].update(role='admin', group_id=2)
    write(case, 'UPDATE print_files SET org_id=2 WHERE id=7099')
    write(case, 'UPDATE jobs SET charged_to_org_id=2 WHERE id=7099')
    write(case, 'UPDATE print_archives SET org_id=2')
    with Session(case[1]) as db:
        change_mark(db, 'files', 7099, mark(), case[3])
    write(case, 'UPDATE print_archives SET completed_at=CURRENT_TIMESTAMP')
    case[3].update(id=4, role='operator')
    assert case[0].get('/archives/accounting-summary').json()['unresolved'] == 0
    assert case[0].get('/archives?accounting=unresolved').json()['total'] == 0
    assert preview(case).status_code == 404
    assert post(case, no_deduction='nothing_extruded').status_code == 404


def test_real_legacy_key_auth_records_auth_kind(settlement_case, monkeypatch):
    from core.dependencies import get_current_user
    case = settlement_case
    monkeypatch.setenv('API_KEY', 'settlement-test-global-key')
    del case[0].app.dependency_overrides[get_current_user]
    response = case[0].post(f'/archives/{case[2]}/settle-filament', headers={'X-API-Key': 'settlement-test-global-key'}, json={'no_deduction': 'nothing_extruded'})
    assert response.status_code == 200, response.text
    entry = json.loads(query(case, 'SELECT consumption_json FROM print_archives')[0][0])[0]
    assert entry['settled_by_user_id'] == 3
    assert entry['settled_auth_kind'] == 'legacy_global_api_key'
    audit = json.loads(query(case, "SELECT details FROM audit_logs WHERE action='archive.filament_settled'")[0][0])
    assert audit['actor_auth_kind'] == 'legacy_global_api_key'


@pytest.mark.parametrize('kind', ['marker', 'print_usage', 'job_usage', 'legacy_job_usage'])
def test_batch_prior_matches_live_ledger(settlement_case, kind):
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    from core.filament_read_evidence import batch_runner
    from core.filament_ledger import prior_attempt_usage
    from core.print_attempts import sqlalchemy_runner
    case = settlement_case
    if kind in ('marker', 'job_usage'):
        key = 'quota_attempt:filament:job:7099:print:7099' if kind == 'marker' else 'quota_attempt:filament:job:7099:0'
        write(case, 'INSERT INTO system_config(key,value) VALUES(:key,:value)', {'key': key, 'value': json.dumps({'print_job_id': None})})
    else:
        write(case, "INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g,used_at,notes) VALUES(7099,7099,:pid,40,'2026-10-11 13:00:00','Auto-deducted on job #7099')", {'pid': 7099 if kind == 'print_usage' else None})
    with Session(case[1]) as db:
        assert prior_attempt_usage(batch_runner(db, [7099]), 7099, 7099) == prior_attempt_usage(sqlalchemy_runner(db), 7099, 7099) == kind


@pytest.mark.parametrize('kind,phrase', [('missing_print', 'physical print record'), ('reconciliation', 'Weigh the spool')])
def test_non_settleable_refusal_explains_the_actual_problem(settlement_case, kind, phrase):
    case = settlement_case
    clause = 'print_job_id=NULL' if kind == 'missing_print' else "accounting_state='reconciliation_required',accounting_reason='inventory_shortfall'"
    write(case, 'UPDATE print_archives SET ' + clause)
    response = post(case, no_deduction='nothing_extruded')
    assert response.status_code == 409, response.text
    assert phrase in response.json()['detail']['message']
    assert 'already settled' not in response.json()['detail']['message']
    assert 'Imported history' not in response.json()['detail']['message']
