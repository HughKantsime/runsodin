"""Real bound request actors and transactions; printer transport is synthetic."""
import json
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import text, event
from sqlalchemy.orm import Session
from tests.test_contracts.test_printer_control_behavior import control_client, token_client, review_db
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
from tests.test_contracts.test_monitor_command_channel import setup_resume


@pytest.fixture
def audited(control_client, review_db):
    from core.db import get_db
    from core.audit_actor import register_request_session, clear_session
    from core.middleware.audit_actor import AuditActorMiddleware
    from modules.vision.routes import detections
    client, issue, calls = control_client
    client.app.add_middleware(AuditActorMiddleware)
    from core.middleware.dry_run import dry_run_middleware
    from modules.printers import routes_controls
    client.app.middleware('http')(dry_run_middleware)
    client.app.include_router(routes_controls.router, prefix='/api/v1')
    client.app.include_router(detections.router)
    def database(request: Request):
        with Session(review_db.get_bind()) as db:
            register_request_session(db, request)
            try: yield db
            finally: clear_session(db)
    client.app.dependency_overrides[get_db] = database
    return TestClient(client.app, client=('192.0.2.19', 42000), raise_server_exceptions=False), issue, calls


def rows(db):
    return db.execute(text("SELECT action,entity_type,entity_id,details,actor_user_id,actor_auth_kind,ip_address FROM audit_logs WHERE action LIKE 'printer.%' ORDER BY id")).all()


@pytest.mark.parametrize('endpoint', ['pause', 'stop'])
@pytest.mark.parametrize('accepted', [True, False])
@pytest.mark.parametrize('credential', ['token', 'jwt'])
def test_controls_record_bound_actor_and_outcome(audited, review_db, monkeypatch, endpoint, accepted, credential):
    from modules.printers import routes_controls
    client, issue, calls = audited
    monkeypatch.setattr(routes_controls, '_send_printer_command', lambda printer, command: calls.append(command) or accepted)
    headers = issue(['agent:write', 'write:printers']) if credential == 'token' else interactive_headers()
    response = client.post('/printers/299/' + endpoint, headers=headers)
    assert response.status_code == (200 if accepted else 503)
    assert len(calls) == 1
    row, = rows(review_db)
    assert row[:3] == (f'printer.{endpoint}_' + ('requested' if accepted else 'failed'), 'printer', 299)
    expected_token = review_db.execute(text('SELECT max(id) FROM api_tokens')).scalar() if credential == 'token' else None
    assert json.loads(row.details) == {'actor_token_id': expected_token}
    assert row[4:] == (3, 'user_api_token' if credential == 'token' else 'bearer_jwt', '192.0.2.19')
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=299')).scalar() == (
        ('PAUSED' if endpoint == 'pause' else 'IDLE') if accepted else 'RUNNING')


@pytest.mark.parametrize('case', ['dry_run', 'foreign', 'scope'])
def test_pause_refusals_and_preview_send_and_record_nothing(audited, review_db, case):
    client, issue, calls = audited
    headers = issue(['read:printers'] if case == 'scope' else ['agent:write'])
    if case == 'dry_run': headers['X-Dry-Run'] = 'true'
    if case == 'foreign':
        review_db.execute(text('UPDATE printers SET org_id=2 WHERE id=299')); review_db.commit()
    response = client.post('/api/v1/printers/299/pause', headers=headers)
    assert response.status_code == {'dry_run': 200, 'foreign': 404, 'scope': 403}[case]
    assert not calls and not rows(review_db)


@pytest.fixture
def fail_audit_insert(review_db):
    def fail(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith('INSERT INTO AUDIT_LOGS'):
            raise RuntimeError('Synthetic audit persistence failure')
    engine = review_db.get_bind()
    event.listen(engine, 'before_cursor_execute', fail)
    try: yield
    finally: event.remove(engine, 'before_cursor_execute', fail)


@pytest.mark.parametrize('endpoint', ['pause', 'stop'])
@pytest.mark.parametrize('accepted', [True, False])
def test_control_audit_failure_never_commits_state_or_reports_success(audited, review_db, monkeypatch, fail_audit_insert, endpoint, accepted):
    from modules.printers import routes_controls
    client, issue, calls = audited
    monkeypatch.setattr(routes_controls, '_send_printer_command', lambda printer, command: calls.append(command) or accepted)
    response = client.post('/printers/299/' + endpoint, headers=issue(['agent:write', 'write:printers']))
    assert response.status_code == 500
    assert len(calls) == 1  # Same send-before-audit semantics as the resume route.
    assert not rows(review_db)
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=299')).scalar() == 'RUNNING'


@pytest.mark.parametrize('state,outcome', [('PAUSE', 'accepted'), ('PAUSED', 'unknown'), ('PAUSE', 'refused'), ('RUNNING', 'not_needed')])
def test_vigil_resume_audits_transport_and_preserves_failed_detection(audited, review_db, monkeypatch, state, outcome):
    from modules.printers import monitor_commands
    client, _, calls = audited
    setup_resume(review_db, state)
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3')); review_db.commit()
    monkeypatch.setattr(monitor_commands, 'request_command', lambda *a, **kw: calls.append('resume') or outcome)
    response = client.post('/vision/detections/701/dismiss-and-resume', headers=interactive_headers())
    assert response.status_code == {'accepted': 200, 'not_needed': 200, 'unknown': 503, 'refused': 409}[outcome]
    assert len(calls) == (0 if state == 'RUNNING' else 1)
    logged = rows(review_db)
    if state == 'RUNNING': assert not logged
    else:
        row, = logged
        assert row[:3] == ('printer.resume_' + ('requested' if outcome == 'accepted' else 'failed'), 'printer', 9)
        assert json.loads(row.details) == {'via': 'vision_dismissal', 'detection_id': 701}
        assert row[4:] == (3, 'bearer_jwt', '192.0.2.19')
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar() == (
        'dismissed' if outcome in ('accepted', 'not_needed') else 'confirmed')


def test_vigil_audit_failure_rolls_back_dismissal(audited, review_db, monkeypatch, fail_audit_insert):
    from modules.printers import monitor_commands
    client, _, calls = audited
    setup_resume(review_db, 'PAUSE')
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3')); review_db.commit()
    monkeypatch.setattr(monitor_commands, 'request_command', lambda *a, **kw: calls.append('resume') or 'accepted')
    assert client.post('/vision/detections/701/dismiss-and-resume', headers=interactive_headers()).status_code == 503
    assert calls == ['resume'] and not rows(review_db)
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar() == 'confirmed'
