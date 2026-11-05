"""Resume safety through real HTTP auth, database state and bound audit actors."""
import json
import secrets

import pytest
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
from tests.test_contracts.test_control_audit_small_fixes import (
    audited, control_client, token_client, review_db, rows, fail_audit_insert,
)

URL = '/api/v1/printers/299/resume'
HMS = json.dumps([{'attr': 50334976, 'code': 65548}])


def seed(db, *, hms=None, detection=None, detection_printer=299):
    db.execute(text("UPDATE printers SET gcode_state='PAUSE', hms_errors=:hms WHERE id=299"), {'hms': hms})
    if detection:
        db.execute(text(
            "INSERT INTO vision_detections(printer_id,detection_type,confidence,status) "
            "VALUES(:pid,'spaghetti',0.99,:status)"
        ), {'pid': detection_printer, 'status': detection})
    db.commit()


def state(db):
    return db.execute(text('SELECT gcode_state FROM printers WHERE id=299')).scalar()


def credential(client, issue, monkeypatch, kind):
    if kind == 'global':
        key = secrets.token_urlsafe(32)
        monkeypatch.setenv('API_KEY', key)
        return {'X-API-Key': key}
    if kind == 'cookie':
        client.cookies.set('session', interactive_headers()['Authorization'][7:])
        return {}
    if kind == 'bearer':
        return interactive_headers()
    return issue([kind])


@pytest.mark.parametrize('kind', ['write:printers', 'write', 'agent:write', 'admin', 'global'])
@pytest.mark.parametrize('reason', ['printer_fault_active', 'detection_open'])
def test_noninteractive_fault_refusal_is_audited_once(audited, review_db, monkeypatch, kind, reason):
    client, issue, calls = audited
    seed(review_db, hms=HMS if reason == 'printer_fault_active' else None,
         detection='auto_paused' if reason == 'detection_open' else None)
    headers = credential(client, issue, monkeypatch, kind)
    response = client.post(URL, headers=headers)
    assert response.status_code == 409
    assert response.json()['error'] == {
        'code': reason,
        'detail': 'A person must review the printer and resume it from the ODIN web app.',
        'retriable': False,
    }
    assert not calls and state(review_db) == 'PAUSE'
    row, = rows(review_db)
    assert row[:3] == ('printer.resume_refused', 'printer', 299)
    token_id = None if kind == 'global' else review_db.execute(text('SELECT max(id) FROM api_tokens')).scalar()
    assert json.loads(row.details) == {'actor_token_id': token_id, 'reason': reason}
    assert row[4:] == (
        None if kind == 'global' else 3,
        'legacy_global_api_key' if kind == 'global' else 'user_api_token', '192.0.2.19',
    )


@pytest.mark.parametrize('kind', ['cookie', 'bearer'])
@pytest.mark.parametrize('fault', ['hms', 'detection', 'both'])
def test_interactive_session_preserves_success_response(audited, review_db, monkeypatch, kind, fault):
    client, issue, calls = audited
    seed(review_db, hms=HMS if fault != 'detection' else None,
         detection='auto_paused' if fault != 'hms' else None)
    response = client.post(URL, headers=credential(client, issue, monkeypatch, kind))
    assert response.status_code == 200
    assert response.json() == {
        'success': True, 'message': 'Print resumed', 'printer_id': 299, 'state': 'RUNNING',
        'next_actions': [
            {'tool': 'get_printer', 'args': {'printer_id': 299}, 'reason': 'confirm running state'},
            {'tool': 'pause_printer', 'args': {'printer_id': 299}, 'reason': 'if intervention needed'},
        ],
    }
    assert calls == [(299, 'resume_print', ())] and state(review_db) == 'RUNNING'
    row, = rows(review_db)
    assert row.action == 'printer.resume_requested'
    assert row.actor_auth_kind == ('session_jwt' if kind == 'cookie' else 'bearer_jwt')


@pytest.mark.parametrize('hms', [None, '', '[]', 'null'])
@pytest.mark.parametrize('detection', [None, 'pending', 'dismissed', 'confirmed', 'training'])
def test_no_current_fault_or_open_auto_pause_allows_resume(audited, review_db, hms, detection):
    client, issue, calls = audited
    seed(review_db, hms=hms, detection=detection)
    # Historical display text is not current fault authority.
    review_db.execute(text("UPDATE printers SET print_stage='Paused: hms=old-code' WHERE id=299"))
    review_db.commit()
    response = client.post(URL, headers=issue(['write:printers']))
    assert response.status_code == 200
    assert calls == [(299, 'resume_print', ())] and state(review_db) == 'RUNNING'
    assert [r.action for r in rows(review_db)] == ['printer.resume_requested']


def test_other_printer_detection_does_not_block(audited, review_db):
    client, issue, calls = audited
    review_db.execute(text("INSERT INTO printers(id,name) VALUES(300,'Other printer')"))
    seed(review_db, detection='auto_paused', detection_printer=300)
    assert client.post(URL, headers=issue(['write:printers'])).status_code == 200
    assert calls == [(299, 'resume_print', ())]


def test_current_hms_clear_allows_next_resume(audited, review_db):
    client, issue, calls = audited
    headers = issue(['write:printers'])
    seed(review_db, hms=HMS)
    assert client.post(URL, headers=headers).status_code == 409
    seed(review_db, hms='[]')
    assert client.post(URL, headers=headers).status_code == 200
    assert calls == [(299, 'resume_print', ())]


@pytest.mark.parametrize('hms', [HMS, 'invalid-json'])
def test_fault_precedence_and_unreadable_hms_fail_closed(audited, review_db, hms):
    client, issue, calls = audited
    seed(review_db, hms=hms, detection='auto_paused')
    response = client.post(URL, headers=issue(['write:printers']))
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'printer_fault_active'
    assert not calls


@pytest.mark.parametrize('kind', ['write:printers', 'write', 'agent:write', 'admin', 'global', 'cookie'])
@pytest.mark.parametrize('dry', [False, True])
def test_pause_hint_matches_scope_gate(audited, review_db, monkeypatch, kind, dry):
    client, issue, calls = audited
    seed(review_db)
    headers = credential(client, issue, monkeypatch, kind)
    if dry:
        headers['X-Dry-Run'] = 'true'
    response = client.post(URL, headers=headers)
    assert response.status_code == 200
    tools = [a['tool'] for a in response.json()['next_actions']]
    assert tools == (['get_printer'] if kind in ('write:printers', 'write') else ['get_printer', 'pause_printer'])
    assert len(calls) == (0 if dry else 1)
    assert len(rows(review_db)) == (0 if dry else 1)


@pytest.mark.parametrize('fault', ['hms', 'detection'])
def test_dry_run_refuses_without_command_state_change_or_audit(audited, review_db, fault):
    client, issue, calls = audited
    seed(review_db, hms=HMS if fault == 'hms' else None,
         detection='auto_paused' if fault == 'detection' else None)
    response = client.post(URL, headers={**issue(['write:printers']), 'X-Dry-Run': 'true'})
    assert response.status_code == 409
    assert not calls and not rows(review_db) and state(review_db) == 'PAUSE'


@pytest.mark.parametrize('case', ['foreign', 'missing', 'scope'])
def test_authorization_and_visibility_precede_fault_check(audited, review_db, case):
    client, issue, calls = audited
    seed(review_db, hms=HMS, detection='auto_paused')
    if case == 'foreign':
        review_db.execute(text('UPDATE printers SET org_id=2 WHERE id=299'))
        review_db.commit()
    headers = issue(['read:printers'] if case == 'scope' else ['write:printers'])
    response = client.post(URL.replace('299', '999') if case == 'missing' else URL, headers=headers)
    assert response.status_code == (403 if case == 'scope' else 404)
    assert not calls and not rows(review_db)


def test_failed_refusal_audit_still_sends_no_command(audited, review_db, fail_audit_insert):
    client, issue, calls = audited
    seed(review_db, hms=HMS)
    assert client.post(URL, headers=issue(['write:printers'])).status_code == 500
    assert not calls and not rows(review_db) and state(review_db) == 'PAUSE'


@pytest.mark.parametrize('endpoint', ['pause', 'stop'])
def test_fault_does_not_block_pause_or_stop(audited, review_db, endpoint):
    client, issue, calls = audited
    seed(review_db, hms=HMS, detection='auto_paused')
    response = client.post(URL.replace('resume', endpoint), headers=issue(['agent:write', 'write:printers']))
    assert response.status_code == 200
    assert calls == [(299, endpoint + '_print', ())]
