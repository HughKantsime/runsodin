"""The real existing report runner with fake transport; no SMTP connections."""
import json
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_program_reports import program, filters
from tests.test_contracts.test_education_classes import setup
from tests.test_contracts.test_education_review_workflow import review_db, _principal
from modules.organizations.education_program_schedules import validate_filters, generate_email
from modules.organizations.education_program_records import export_program_records, erase_program_records


@pytest.fixture
def scheduled(program):
    db, cls, _ = program
    db.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(6,'platform','platform@example.test','fixture','admin',TRUE)"))
    db.commit()
    conf = validate_filters(db, {'org_id':1, 'cost_center_id':7, 'days':90}, 6)
    db.execute(text("INSERT INTO report_schedules(id,name,report_type,frequency,recipients,filters,created_by,is_active,next_run_at) "
        "VALUES(98,'Funding','program_report','weekly','[\"manager@example.test\"]',:filters,6,TRUE,'2000-01-01T00:00:00+00:00')"), {'filters':json.dumps(conf)})
    db.commit()
    return db, cls, conf


def test_generated_email_scoped_and_rechecks_creator(scheduled):
    db, cls, config = scheduled
    html = generate_email(db, config)
    assert 'Engineering' in html and 'student@example.test' not in html
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='program.email_generated'")).scalar() == 1
    db.execute(text("UPDATE users SET role='viewer' WHERE id=6")); db.commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        generate_email(db, config)


@pytest.mark.parametrize('change', [{'org_id':2,'class_id':'missing'}, {'org_id':1,'cost_center_id':999}, {'org_id':1,'days':999}, {}, {'org_id':1,'days':0}])
def test_invalid_schedule_filters_refused(scheduled, change):
    from fastapi import HTTPException
    db, cls, _ = scheduled
    with pytest.raises(HTTPException):
        validate_filters(db, change, 6)


def test_system_admin_required_even_when_claimed_role_is_admin(scheduled):
    from fastapi import HTTPException
    db, cls, _ = scheduled
    for user in (1, 2, 3, 5, 999):
        with pytest.raises(HTTPException):
            validate_filters(db, {'org_id':1}, user)


@pytest.mark.parametrize('mode', ['poll', 'now'])
@pytest.mark.parametrize('licensed', [True, False])
def test_runner_gate_and_delivery(scheduled, monkeypatch, tier_license, mode, licensed):
    from modules.reporting import report_runner as runner
    db, cls, _ = scheduled
    tier_license('education' if licensed else 'pro')
    monkeypatch.setattr(runner, 'SessionLocal', lambda: Session(db.get_bind()))
    monkeypatch.setattr(runner, 'get_smtp_config', lambda session: {'fake':True})
    sent = []
    monkeypatch.setattr(runner, 'send_report_email', lambda *args: sent.append(args) or True)
    if mode == 'poll':
        runner.process_due_reports(db)
    else:
        row = dict(db.execute(text('SELECT * FROM report_schedules WHERE id=98')).mappings().one())
        if licensed:
            runner.run_report(row)
        else:
            with pytest.raises(RuntimeError, match='licensed'):
                runner.run_report(row)
    assert len(sent) == int(licensed)
    if sent:
        assert 'student@example.test' not in sent[0][3]


def test_schedule_privacy_deletes_creator_config_and_removes_erased_recipient(scheduled):
    db, _, config = scheduled
    db.execute(text('UPDATE report_schedules SET recipients=:r WHERE id=98'), {'r':json.dumps(['student@example.test','manager@example.test'])}); db.commit()
    assert 'manager@example.test' not in str(export_program_records(db, 6))
    own = export_program_records(db, 1)
    assert own['recipient_schedules'] == [{'id':98, 'frequency':'weekly', 'is_active':1}]
    assert 'manager@example.test' not in str(own)
    erase_program_records(db, 1, {'student', 'student@example.test'}); db.commit()
    assert json.loads(db.execute(text('SELECT recipients FROM report_schedules')).scalar()) == ['manager@example.test']
    erase_program_records(db, 6, {'platform', 'platform@example.test'}); db.commit()
    assert not db.execute(text('SELECT is_active FROM report_schedules')).scalar()


def test_http_create_edit_run_pause_license_checks(scheduled, monkeypatch, tier_license):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.app_errors import register_error_handlers
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.reporting.routes.reports import router
    from modules.reporting import report_runner as runner
    db, cls, _ = scheduled
    app = FastAPI(); register_error_handlers(app); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _principal(6, 'admin', None)
    with TestClient(app) as http:
        payload = {'name':'Program','report_type':'program_report','recipients':['manager@example.test'],'filters':{'org_id':1,'class_id':cls['id']}}
        assert http.post('/report-schedules', json={**payload, 'filters':{'org_id':1}}).status_code == 422
        assert http.post('/report-schedules', json={**payload, 'recipients':['student@example.test']}).status_code == 422
        result = http.post('/report-schedules', json=payload)
        assert result.status_code == 200, result.text
        sid = result.json()['id']
        assert http.patch(f'/report-schedules/{sid}', json={'filters':{'org_id':1}}).status_code == 422
        assert http.patch(f'/report-schedules/{sid}', json={'recipients':['foreign@example.test']}).status_code == 422
        assert http.patch(f'/report-schedules/{sid}', json={'filters':{'org_id':2,'class_id':cls['id']}}).status_code == 404
        monkeypatch.setattr(runner, 'run_report', lambda _: None)
        assert http.post(f'/report-schedules/{sid}/run').status_code == 200
        tier_license('pro')
        assert http.post('/report-schedules', json=payload).status_code == 403
        assert http.post(f'/report-schedules/{sid}/run').status_code == 403
        assert http.patch(f'/report-schedules/{sid}', json={'is_active':True}).status_code == 403
        assert http.patch(f'/report-schedules/{sid}', json={'is_active':False}).status_code == 200
        assert http.delete(f'/report-schedules/{sid}').status_code == 200


@pytest.mark.parametrize('address', ['student@example.test','foreign@example.test','missing@example.test'])
def test_recipients_require_active_school_manager_or_admin(scheduled, address):
    from modules.organizations.services import validate_program_schedule
    from fastapi import HTTPException
    db, _, _ = scheduled
    with pytest.raises(HTTPException):
        validate_program_schedule(db, {'org_id':1}, 6, [address])
    validate_program_schedule(db, {'org_id':1, 'cost_center_id':7}, 6, ['manager@example.test','admin@example.test'])


@pytest.mark.parametrize('mode', ['poll', 'now'])
@pytest.mark.parametrize('change', ['recipient', 'owner', 'editor'])
def test_delivery_rechecks_recipients_owner_and_editor(scheduled, monkeypatch, mode, change):
    from modules.reporting import report_runner as runner
    db, _, config = scheduled
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(8,'editor','fixture','admin',TRUE)"))
    config['_program_actor'] = 8
    db.execute(text('UPDATE report_schedules SET filters=:f WHERE id=98'), {'f':json.dumps(config)})
    if change == 'recipient':
        db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"))
    else:
        db.execute(text("UPDATE users SET role='viewer' WHERE id=:u"), {'u':6 if change == 'owner' else 8})
    db.commit()
    monkeypatch.setattr(runner, 'SessionLocal', lambda: Session(db.get_bind()))
    monkeypatch.setattr(runner, 'get_smtp_config', lambda session: {'fake':True})
    sent = []
    monkeypatch.setattr(runner, 'send_report_email', lambda *args: sent.append(args) or True)
    if mode == 'poll':
        runner.process_due_reports(db)
    else:
        row = dict(db.execute(text('SELECT * FROM report_schedules WHERE id=98')).mappings().one())
        with pytest.raises(Exception):
            runner.run_report(row)
    assert sent == []
    if change != 'recipient':
        db.expire_all()
        assert not db.execute(text('SELECT is_active FROM report_schedules WHERE id=98')).scalar()


def test_erasure_deactivates_last_editor_schedule(scheduled):
    db, _, config = scheduled
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(8,'editor','fixture','admin',TRUE)"))
    config['_program_actor'] = 8
    db.execute(text('UPDATE report_schedules SET filters=:f WHERE id=98'), {'f':json.dumps(config)})
    erase_program_records(db, 8); db.commit()
    assert not db.execute(text('SELECT is_active FROM report_schedules WHERE id=98')).scalar()


@pytest.mark.parametrize('mode', ['poll', 'now'])
def test_recipient_checked_again_after_render_before_each_send(scheduled, monkeypatch, mode):
    from modules.reporting import report_runner as runner
    db, _, _ = scheduled
    monkeypatch.setattr(runner, 'SessionLocal', lambda: Session(db.get_bind()))
    monkeypatch.setattr(runner, 'get_smtp_config', lambda session: {'fake':True})
    def generate(session, filters):
        session.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"))
        session.commit()
        return '<p>rendered</p>'
    monkeypatch.setitem(runner.GENERATORS, 'program_report', generate)
    sent = []
    monkeypatch.setattr(runner, 'send_report_email', lambda *args: sent.append(args) or True)
    if mode == 'poll':
        runner.process_due_reports(db)
    else:
        with pytest.raises(Exception):
            runner.run_report(dict(db.execute(text('SELECT * FROM report_schedules WHERE id=98')).mappings().one()))
    assert sent == []


def test_user_demotion_deactivates_owned_schedule_immediately(scheduled):
    import asyncio
    from modules.organizations.routes_users import update_user, UserUpdateRequest
    db, _, _ = scheduled
    asyncio.run(update_user(6, UserUpdateRequest(role='viewer'), _principal(6, 'admin', None), db=db))
    assert not db.execute(text('SELECT is_active FROM report_schedules WHERE id=98')).scalar()
