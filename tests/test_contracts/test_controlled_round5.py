"""Round-five behavior contracts, including the five surviving mutation guards."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_controlled_guards import marked


def test_timelapse_completion_controls_retention(database, tier_license, monkeypatch):
    from core import controlled_policy_clock as clock
    from core.controlled_retention import expired
    from modules.organizations.routes_controlled_parts import change_mark
    tier_license('enterprise')
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, 'datetime', SimpleNamespace(now=lambda *a: origin))
    change_mark(database, 'files', 1, mark(retention_days=30), actor())
    completed = origin + timedelta(days=40)
    database.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,completed_at,created_at) VALUES(1,1,1,'late.mp4','ready',:time,:time)"), {'time': completed})
    database.commit()
    assert not expired(database, 1, now=completed + timedelta(minutes=10))
    assert not expired(database, 1, now=completed + timedelta(days=29))
    assert expired(database, 1, now=completed + timedelta(days=30))


def test_shortening_grace_cycle_and_new_media(database, tier_license, monkeypatch):
    from core import controlled_policy_clock as clock
    from core.controlled_parts import policies
    from modules.organizations.routes_controlled_parts import change_mark
    tier_license('enterprise')
    now = origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, 'datetime', SimpleNamespace(now=lambda *a: now))
    def change(days):
        change_mark(database, 'files', 1, mark(retention_days=days), actor())
        return policies(database, 'file', 1)[0]
    change(30)
    now += timedelta(days=25)
    for days in (29, 30, 29, 30):
        rule = change(days)
        assert clock.media_deadline(rule, origin) == origin + timedelta(days=30)
    now += timedelta(days=3)
    rule = change(1)
    # Grace cannot postpone an existing deadline.
    assert clock.media_deadline(rule, origin) == origin + timedelta(days=30)
    # Nor can cycling erase an earlier shortening for already completed media.
    now += timedelta(days=1)
    rule = change(30)
    assert clock.media_deadline(rule, origin) == origin + timedelta(days=30)
    # A new print gets the current full policy window.
    completed = origin + timedelta(days=40)
    assert clock.media_deadline(rule, completed) == completed + timedelta(days=30)


def test_shortening_uses_seven_day_grace(database, tier_license, monkeypatch):
    from core import controlled_policy_clock as clock
    from core.controlled_parts import policies
    from modules.organizations.routes_controlled_parts import change_mark
    tier_license('enterprise')
    now = origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, 'datetime', SimpleNamespace(now=lambda *a: now))
    change_mark(database, 'files', 1, mark(retention_days=30), actor())
    now += timedelta(days=10)
    change_mark(database, 'files', 1, mark(retention_days=1), actor())
    rule = policies(database, 'file', 1)[0]
    assert clock.media_deadline(rule, origin) == origin + timedelta(days=17)
    now += timedelta(days=1)
    change_mark(database, 'files', 1, mark(retention_days=30), actor())
    change_mark(database, 'files', 1, mark(retention_days=1), actor())
    assert clock.media_deadline(policies(database, 'file', 1)[0], origin) == origin + timedelta(days=17)


def delete_controlled(db):
    from core.audit import log_audit
    from core.controlled_deletion import redact_deleted_job_history
    log_audit(db, 'job.updated', 'job', 1, {'name': 'Secret history'})
    db.commit()
    old_id = db.execute(text('SELECT max(id) FROM audit_logs')).scalar_one()
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) VALUES(50,1,1,'Secret physical','completed',CURRENT_TIMESTAMP)"))
    redact_deleted_job_history(db, 1)
    db.execute(text('DELETE FROM jobs WHERE id=1'))
    db.execute(text('DELETE FROM controlled_files'))
    db.commit()
    return old_id


def test_tombstone_linked_print_and_denied_union_without_live_marks(marked):
    from core.controlled_parts import any_marks, allowed
    from core.controlled_redaction import redact_export
    delete_controlled(marked)
    marked.info.update(_controlled_request=True)
    assert not any_marks(marked)
    from core.controlled_events import external_payload, event_allowed
    public = {'job_name': 'Public unrelated work'}
    assert external_payload(marked, public) == public
    assert event_allowed(actor(2), {'data': public}, db=marked)
    assert not allowed(marked, 'print_job', 50, actor(2))
    assert allowed(marked, 'print_job', 50, actor())
    payload = {'print_jobs': [{'id': 50, 'job_name': 'Secret physical'}]}
    assert redact_export(marked, actor(2), payload)['print_jobs'][0]['job_name'] == '[controlled]'
    assert redact_export(marked, actor(), payload) == payload


@pytest.mark.parametrize('legacy_shared_id', [False, True])
def test_tombstone_audits_follow_durable_job_identity(marked, legacy_shared_id):
    from core.audit import log_audit
    from core.models import AuditLog
    from core.controlled_audit import redact_audit_rows
    from core.controlled_redaction import redact_export
    from core.controlled_parts import audit_visibility
    old_id = delete_controlled(marked)
    if legacy_shared_id:
        # Reproduce pre-upgrade shared IDs; these histories remain merged.
        marked.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(1,'Public new work','pending')"))
        identifier = 1
    else:
        from tests.test_contracts.test_controlled_round8 import new_job
        identifier = new_job(marked)
        assert identifier > 1
    log_audit(marked, 'job.updated', 'job', identifier, {'name': 'Public new work'})
    marked.commit()
    rows = marked.query(AuditLog).filter(AuditLog.action == 'job.updated').order_by(AuditLog.id).all()
    result = redact_audit_rows(marked, actor(2), [(r,None,None,None) for r in rows])
    assert result[0][0].details == '[controlled]'
    expected = '[controlled]' if legacy_shared_id else rows[1].details
    assert result[1][0].details == expected
    condition, values = audit_visibility(actor(2), marked)
    assert marked.execute(text("SELECT id FROM audit_logs WHERE action='job.updated' AND " + condition), values).scalars().all() == ([] if legacy_shared_id else [rows[1].id])
    payload = [{'id': r.id, 'entity_type': 'job', 'entity_id': r.entity_id, 'details': r.details} for r in rows]
    exported = redact_export(marked, actor(2), payload)
    assert exported[0]['details'] == '[controlled]'
    assert exported[1]['details'] == expected
    assert redact_export(marked, actor(), payload) == payload
    assert marked.execute(text('SELECT audit_max_id FROM controlled_job_tombstones')).scalar_one() > old_id


def test_audience_delete_conflict_and_fk(marked):
    from modules.organizations.routes_controlled_audiences import delete_audience
    from sqlalchemy import inspect
    delete_controlled(marked)
    with pytest.raises(HTTPException) as exc:
        delete_audience(1, actor(2), marked)
    assert exc.value.status_code == 409
    assert exc.value.detail['code'] == 'controlled_part_audience_in_use'
    foreign = inspect(marked.get_bind()).get_foreign_keys('controlled_job_tombstones')
    assert foreign[0]['referred_table'] == 'controlled_audiences'
    assert foreign[0]['options']['ondelete'] == 'RESTRICT'


def test_idle_and_stale_name_printer_cards(marked):
    from core.display_names import name_printers
    from tests.test_contracts.test_controlled_round3 import finished_print
    finished_print(marked)
    printer = SimpleNamespace(id=1)
    assert name_printers(marked, [printer], actor(2))[0].current_print_display_name is None
    # A stale, unresolved running row can still supply a card name after the
    # latest controlled print finished. Only the names policy covers this case.
    marked.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(49,1,'secret.3mf','running','2000-01-01')"))
    marked.commit()
    assert name_printers(marked, [printer], actor())[0].current_print_display_name == 'secret'
    assert name_printers(marked, [printer], actor(2))[0].current_print_display_name == 'Controlled work'


def test_overlay_camera_uses_active_work(marked):
    from fastapi import APIRouter
    from core.controlled_overlay import protect_overlay
    from tests.test_contracts.test_controlled_round3 import finished_print
    finished_print(marked)
    router = APIRouter()
    @router.get('/overlay/{printer_id}')
    def overlay(printer_id: int, db=None):
        return {'camera_url': '/camera', 'job_name': 'Secret'}
    protect_overlay(router)
    read = router.routes[0].endpoint
    assert read(printer_id=1, db=marked) == {'camera_url':'/camera', 'job_name':'[controlled]'}
    marked.execute(text("UPDATE print_jobs SET status='running' WHERE id=50")); marked.commit()
    assert read(printer_id=1, db=marked)['camera_url'] is None


def test_public_receipt_does_not_bind_reprint(marked):
    from core.controlled_reprint import bind_reprint
    from core.dispatch_observation_executor import TransactionView
    from core.dispatch_receipts import filename_fingerprint
    token, key = 'a'*32, b'x'*32
    filename = 'odin-dispatch-' + token + '.3mf'
    marked.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(90,1,'running',CURRENT_TIMESTAMP)"))
    marked.execute(text('INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key) VALUES(:token,1,2,:key,:key,:name)'),
                   {'token':token, 'key':'b'*64, 'name':filename_fingerprint(key,token,filename)})
    assert bind_reprint(TransactionView(marked), 90, 1, 'bambu', [filename], key) is None
    assert marked.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=90')).scalar() is None


def test_log_refusals_hourly_and_stream_rechecks_do_not_audit(marked):
    from core.controlled_logs import check_log_access, stream_log_access
    for _ in range(5):
        with pytest.raises(HTTPException): check_log_access(marked, actor(2), False)
    assert marked.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.refused'")).scalar() == 1
    marked.execute(text("UPDATE audit_logs SET timestamp=:old WHERE action='controlled_part.refused'"),
                   {'old': datetime.now(timezone.utc)-timedelta(hours=2)})
    marked.commit()
    with pytest.raises(HTTPException): check_log_access(marked, actor(2), False)
    assert marked.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.refused'")).scalar() == 2
    before = marked.execute(text('SELECT count(*) FROM audit_logs')).scalar()
    for _ in range(20): stream_log_access(marked.get_bind(), actor(2), True)
    assert marked.execute(text('SELECT count(*) FROM audit_logs')).scalar() == before


def test_stream_open_and_revocation_audited_once(marked, tmp_path, monkeypatch):
    from modules.system import routes_admin
    from core.controlled_logs import issue_stream_token
    path = tmp_path/'stream.log'; path.write_text('')
    monkeypatch.setattr(routes_admin, '_LOG_FILES', {'backend':str(path)})
    class Request:
        calls = 0
        async def is_disconnected(self):
            self.calls += 1
            if self.calls == 4:
                marked.execute(text("UPDATE users SET role='viewer' WHERE id=2")); marked.commit()
            with path.open('a') as f: f.write('line\n')
            return False
    async def consume():
        response = await routes_admin.stream_logs(Request(), source='backend', level=None, search=None,
            user=actor(2), db=marked, token=issue_stream_token(actor(2), True))
        for _ in range(3): assert 'line' in await anext(response.body_iterator)
        with pytest.raises(HTTPException): await anext(response.body_iterator)
    asyncio.run(consume())
    actions = marked.execute(text("SELECT action FROM audit_logs WHERE action LIKE 'controlled_part.log_stream_%' ORDER BY id")).scalars().all()
    assert actions == ['controlled_part.log_stream_opened', 'controlled_part.log_stream_ended']


def test_mqtt_redaction_failure_is_rate_limited_and_closed(marked, monkeypatch, caplog):
    from modules.notifications import mqtt_republish as mqtt
    from core import controlled_mqtt, controlled_warnings
    sent = []
    monkeypatch.setattr(mqtt, '_get_client', lambda: SimpleNamespace(publish=lambda *a,**k:sent.append(a)))
    monkeypatch.setattr(mqtt, '_get_config', lambda: {'topic_prefix':'test'})
    def fail(*a): raise RuntimeError('private failure contents')
    monkeypatch.setattr(controlled_mqtt, 'redact_payload', fail)
    monkeypatch.setattr(controlled_warnings, '_last', {})
    for _ in range(10): mqtt._publish('test', {'job_name':'Secret'})
    assert sent == []
    warnings = [r for r in caplog.records if r.name == 'odin.controlled']
    assert len(warnings) == 1 and 'RuntimeError' in warnings[0].message
    assert 'private failure' not in warnings[0].message and 'Secret' not in warnings[0].message


@pytest.mark.parametrize('surface', ['logs', 'diagnostics', 'report'])
def test_http_ack_is_post_header_not_query(marked, monkeypatch, tmp_path, surface):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import routes_admin, diagnostics, troubleshooting
    path = tmp_path/'logs'; path.write_text('Secret tail\n')
    monkeypatch.setattr(routes_admin, '_LOG_FILES', {'backend':str(path)})
    monkeypatch.setattr(diagnostics, '_read_tail', lambda *a:['Secret tail'])
    monkeypatch.setattr(troubleshooting, 'server_checks', lambda db:{'checks':[]})
    app = FastAPI()
    for router in (routes_admin.router, diagnostics.router, troubleshooting.router):
        app.include_router(router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: marked
    app.dependency_overrides[get_current_user] = lambda: actor(2)
    url = {'logs':'/api/admin/logs','diagnostics':'/api/system/diagnostics',
           'report':'/api/system/troubleshooting/report'}[surface]
    with TestClient(app) as client:
        body = {'include_logs':True} if surface == 'report' else {}
        legacy = client.get(url+'?acknowledge_controlled=true')
        assert legacy.status_code == (405 if surface == 'report' else 200)
        assert 'Secret tail' not in legacy.text
        if surface != 'report':
            assert 'controlled_part_acknowledgement_required' in legacy.text
        response = client.post(url+'?acknowledge_controlled=true', json=body)
        assert response.status_code == (403 if surface == 'logs' else 200)
        assert 'Secret tail' not in response.text
        assert 'controlled_part_acknowledgement_required' in response.text
        headers = {'X-ODIN-Controlled-Ack':'true', 'Origin':'http://foreign.example'}
        assert client.post(url, json=body, headers=headers).status_code == 403
        headers['Origin'] = 'http://testserver'
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == 200 and 'Secret tail' in response.text


def test_stream_token_requires_post_ack_and_binds_actor(marked):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.controlled_logs import stream_ack
    from modules.system import routes_admin
    app = FastAPI(); app.include_router(routes_admin.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: marked
    app.dependency_overrides[get_current_user] = lambda: actor(2)
    with TestClient(app) as client:
        assert client.post('/api/admin/logs/stream-token?acknowledge_controlled=true').status_code == 403
        assert client.get('/api/admin/logs/stream?acknowledge_controlled=true').status_code == 403
        reply = client.post('/api/admin/logs/stream-token', headers={'X-ODIN-Controlled-Ack':'true'})
        assert reply.status_code == 200
        token = reply.json()['token']
    assert stream_ack(token, actor(2))
    with pytest.raises(HTTPException): stream_ack(token, actor())
    with pytest.raises(HTTPException): stream_ack(token+'tampered', actor(2))


def test_deletion_audit_itself_is_inside_tombstone_boundary(marked, monkeypatch):
    from modules.jobs.routes import jobs_crud
    from modules.organizations.education_policy import assert_generic_job_mutation_allowed
    from core.models import AuditLog
    from core.controlled_audit import redact_audit_rows
    monkeypatch.setattr(jobs_crud, 'assert_generic_job_mutation_allowed', assert_generic_job_mutation_allowed)
    jobs_crud.delete_job(1, actor(), marked)
    row = marked.query(AuditLog).filter(AuditLog.action == 'job.deleted').one()
    assert row.id <= marked.execute(text('SELECT audit_max_id FROM controlled_job_tombstones')).scalar_one()
    assert redact_audit_rows(marked, actor(2), [(row,None,None,None)])[0][0].details == '[controlled]'
    assert redact_audit_rows(marked, actor(), [(row,None,None,None)])[0][0].details == row.details
