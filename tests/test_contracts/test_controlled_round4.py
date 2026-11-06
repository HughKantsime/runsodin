"""Round-four regressions: immutable history, log gates, clocks and physical repeats."""
import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import text
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_controlled_guards import marked


@pytest.mark.parametrize('path', ['single', 'bulk', 'retention', 'organization'])
def test_deleted_audit_bytes_and_member_access(marked, monkeypatch, path):
    from core.audit import log_audit
    from core.controlled_parts import audit_visibility
    from core.controlled_audit import redact_audit_rows
    from core.models import AuditLog
    from modules.jobs.routes import jobs_crud
    from modules.organizations.education_policy import assert_generic_job_mutation_allowed
    monkeypatch.setattr(jobs_crud, 'assert_generic_job_mutation_allowed', assert_generic_job_mutation_allowed)
    log_audit(marked, 'job.updated', 'job', 1, {'name': 'Retained secret'})
    marked.commit()
    before = marked.execute(text("SELECT * FROM audit_logs WHERE action='job.updated'")).one()
    if path == 'single':
        jobs_crud.delete_job(1, actor(), marked)
    elif path == 'bulk':
        asyncio.run(jobs_crud.bulk_update_jobs({'job_ids': [1], 'action': 'delete'}, actor(), marked))
    elif path == 'retention':
        from modules.system.routes_config import run_retention_cleanup
        marked.execute(text("UPDATE jobs SET status='completed',updated_at='2000-01-01' WHERE id=1"))
        marked.execute(text("INSERT INTO system_config(key,value) VALUES('data_retention',:value)"),
                       {'value': '{"completed_jobs_days":1,"audit_logs_days":0,"timelapses_days":0,"alert_history_days":0}'})
        marked.commit()
        asyncio.run(run_retention_cleanup(actor(2), marked))
    else:
        from modules.jobs.services import delete_org_jobs
        marked.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=1')); marked.commit()
        delete_org_jobs(marked, 2); marked.commit()
    assert marked.execute(text('SELECT id FROM jobs WHERE id=1')).first() is None
    # Releasing the source cannot remove the deleted job's historical audience.
    marked.execute(text('DELETE FROM controlled_files')); marked.commit()
    assert tuple(marked.execute(text("SELECT * FROM audit_logs WHERE action='job.updated'")).one()) == tuple(before)
    entry = marked.query(AuditLog).filter(AuditLog.action == 'job.updated').one()
    original = entry.details
    assert redact_audit_rows(marked, actor(2), [(entry, None, None, None)])[0][0].details == '[controlled]'
    assert redact_audit_rows(marked, actor(), [(entry, None, None, None)])[0][0].details == original
    for who, expected in ((actor(2), 0), (actor(), 1)):
        visible, values = audit_visibility(who, marked)
        assert marked.execute(text("SELECT count(*) FROM audit_logs WHERE action='job.updated' AND " + visible), values).scalar() == expected


@pytest.mark.parametrize('surface', ['diagnostics', 'report'])
@pytest.mark.parametrize('acknowledged', [False, True])
def test_log_omission_preserves_other_diagnostics(marked, monkeypatch, surface, acknowledged):
    from modules.system import diagnostics, troubleshooting
    monkeypatch.setattr(diagnostics, '_read_tail', lambda *a: ['secret log tail'])
    from core.error_buffer import error_buffer
    monkeypatch.setattr(error_buffer, 'entries', lambda: ['secret buffered error'])
    monkeypatch.setattr(troubleshooting, 'server_checks', lambda db: {'checks': ['retained']})
    if surface == 'diagnostics':
        result = asyncio.run(diagnostics.get_diagnostics(actor(2), marked, acknowledged))
        assert 'odin_version' in result
    else:
        result = troubleshooting.support_report(troubleshooting.ReportRequest(include_logs=True), actor(2), marked, acknowledged)
        assert result['checks'] == ['retained']
    assert ('secret log tail' in str(result)) == acknowledged
    assert ('controlled_part_acknowledgement_required' in str(result)) != acknowledged
    assert ('secret buffered error' in str(result)) == (acknowledged and surface == 'diagnostics')


def test_idle_camera_and_alerts_open_but_names_hidden(marked, monkeypatch):
    from tests.test_contracts.test_controlled_round3 import finished_print
    from core.controlled_parts import allowed
    from core.controlled_redaction import printer_status
    from core.recipient_policy import resolve_alert_scope
    from modules.printers import camera_routes
    finished_print(marked)
    marked.execute(text("UPDATE printers SET camera_url='rtsp://fixture/camera',camera_enabled=TRUE,is_active=TRUE WHERE id=1")); marked.commit()
    assert allowed(marked, 'printer', 1, actor(2))
    assert 2 in resolve_alert_scope(marked, printer_id=1)[0]
    monkeypatch.setattr(camera_routes, 'sync_go2rtc_config', lambda db: None)
    cameras = camera_routes.list_cameras(current_user=actor(2), db=marked)
    assert any(c['id'] == 1 for c in cameras)
    assert camera_routes.get_camera_stream(1, marked, actor(2))['transport'] == 'webrtc'
    from modules.printers.routes_overlay import router
    from core.controlled_overlay import protect_overlay
    from fastapi import APIRouter
    overlay = APIRouter(); overlay.include_router(router); protect_overlay(overlay)
    assert overlay.routes[0].endpoint(printer_id=1, db=marked)['camera_url'] is not None
    assert printer_status(marked, 1, actor(2), {'job_name':'secret'})['job_name'] == '[controlled]'
    marked.execute(text("UPDATE print_jobs SET status='running' WHERE id=50")); marked.commit()
    assert not allowed(marked, 'printer', 1, actor(2))
    assert 2 not in resolve_alert_scope(marked, printer_id=1)[0]


@pytest.mark.parametrize('protocol,observed', [('bambu','subtask'), ('bambu','gcode'), ('moonraker','gcode')])
def test_receipted_reprint_binds_at_observed_start(marked, protocol, observed):
    from core import auth
    from core.dispatch_receipts import filename_fingerprint
    from core.attempt_start_observation import observe_start
    from core.controlled_parts import allowed
    from core.controlled_redaction import redact_export, printer_status
    token = 'a' * 32
    filename = 'odin-dispatch-' + token + '.3mf'
    marked.execute(text("UPDATE jobs SET status='completed',printer_id=1 WHERE id=1"))
    marked.execute(text('INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key) VALUES(:token,1,1,:key,:key,:name)'),
                   {'token':token, 'key':'b'*64, 'name':filename_fingerprint(auth.SECRET_KEY.encode(), token, filename)})
    marked.commit()
    name = filename.removesuffix('.3mf') if observed == 'subtask' else 'Unknown'
    marked.begin()
    result = observe_start(marked.connection(), printer_id=1, protocol=protocol, filename=filename if observed == 'gcode' else '',
                           job_name=name, physical_values={'started_at':datetime.now(timezone.utc)}, source='round4-reprint')
    marked.commit()
    assert result.linked_job_id == 1
    assert marked.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'), {'id':result.id}).scalar() == 1
    assert marked.execute(text('SELECT status FROM jobs WHERE id=1')).scalar() == 'completed'
    assert not allowed(marked, 'print_job', result.id, actor(2))
    assert not allowed(marked, 'printer', 1, actor(2))
    assert printer_status(marked, 1, actor(2), {'gcode_file':filename})['gcode_file'] == '[controlled]'
    data = {'print_jobs':[{'id':result.id, 'job_name':filename}], 'jobs':[{'id':1, 'item_name':'Part'}]}
    assert redact_export(marked, actor(2), data)['print_jobs'][0]['job_name'] == '[controlled]'
    assert redact_export(marked, actor(2), data)['jobs'][0]['item_name'] == '[controlled]'


def test_reprint_requires_same_printer_and_exact_receipt(marked):
    from core.controlled_reprint import bind_reprint
    from core.dispatch_observation_executor import TransactionView
    from core.dispatch_receipts import filename_fingerprint
    key = b'x' * 32; token = 'a' * 32; filename = 'odin-dispatch-' + token + '.3mf'
    marked.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(90,2,'running',CURRENT_TIMESTAMP)"))
    marked.execute(text('INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key) VALUES(:token,1,1,:key,:key,:name)'),
                   {'token':token,'key':'b'*64,'name':filename_fingerprint(key,token,filename)})
    assert bind_reprint(TransactionView(marked), 90, 2, 'bambu', [filename], key) is None
    assert bind_reprint(TransactionView(marked), 90, 1, 'bambu', [filename], b'y'*32) is None


def test_retention_cycle_and_release_clock(database, tier_license, monkeypatch):
    from core import controlled_policy_clock as clock
    from core.controlled_policy_clock import media_deadline
    from core.controlled_parts import policies
    from modules.organizations.routes_controlled_parts import change_mark
    tier_license('enterprise')
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    class Clock:
        @staticmethod
        def now(*a): return now
    monkeypatch.setattr(clock, 'datetime', Clock)
    def change(days, **kwargs):
        change_mark(database, 'files', 1, mark(retention_days=days, **kwargs), actor())
        saved = database.execute(text('SELECT marked_at FROM controlled_retention_clocks')).scalar_one()
        rules = policies(database, 'file', 1)
        return saved, media_deadline(rules[0], origin_time) if rules else None
    origin_time = now
    origin, deadline = change(30)
    now += timedelta(days=25)
    for days in (29, 30, 29):
        marked_at, new = change(days)
        assert marked_at == origin
        assert new <= deadline
        deadline = new
    change(29, controlled=False)
    now += timedelta(days=5)
    assert change(29)[0] == origin
    change(29, controlled=False)
    now += timedelta(days=31)
    assert change(29)[0] != origin


@pytest.mark.parametrize('foreign,public_url', [(False,''), (True,''), (False,'https://farm.example')])
def test_ack_origin_behind_trusted_proxy(monkeypatch, foreign, public_url):
    from core.controlled_csrf import require_controlled_ack
    from core.remote_access import TrustedProxySchemeMiddleware
    from core.config import settings
    monkeypatch.setattr(settings, 'odin_public_url', public_url)
    app = FastAPI()
    app.add_middleware(TrustedProxySchemeMiddleware, trusted='127.0.0.1')
    @app.post('/ack', dependencies=[Depends(require_controlled_ack)])
    def ack(): return {'ok':True}
    async def request():
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1',1234)), base_url='http://internal' if public_url else 'http://farm.example') as client:
            return await client.post('/ack', headers={'X-Forwarded-Proto':'https', 'X-Forwarded-For':'192.0.2.8',
                'Origin':'https://foreign.example' if foreign else 'https://farm.example', 'X-ODIN-Controlled-Ack':'true'})
    assert asyncio.run(request()).status_code == (403 if foreign else 200)


def test_tombstone_survives_id_reuse_and_keeps_audience(marked):
    from core.controlled_deletion import redact_deleted_job_history
    from core.controlled_audit import audit_allowed
    from core.controlled_parts import audit_visibility, allowed
    from core.controlled_redaction import redact_export
    from modules.organizations.routes_controlled_audiences import delete_audience
    from fastapi import HTTPException
    redact_deleted_job_history(marked, 1)
    marked.execute(text('DELETE FROM jobs WHERE id=1'))
    marked.execute(text('DELETE FROM controlled_files'))
    marked.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(1,'Public replacement','pending')"))
    marked.commit()
    assert allowed(marked, 'job', 1, actor(2))
    assert not audit_allowed(marked, actor(2), 'job', 1)
    assert redact_export(marked, actor(2), {'entity_type':'job','entity_id':1,'details':'Old secret'})['details'] == '[controlled]'
    with pytest.raises(HTTPException) as exc:
        delete_audience(1, actor(2), marked)
    assert exc.value.detail['code'] == 'controlled_part_audience_in_use'
