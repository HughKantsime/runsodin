"""Executable guard contracts. ops/check_controlled_mutations.py removes each guard."""
import asyncio
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from starlette.requests import Request
from tests.test_contracts.test_controlled_parts import database, actor, mark
from modules.organizations.routes_controlled_parts import change_mark


@pytest.fixture
def marked(database, tier_license):
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    return database


def request(method='GET', path='/api/jobs/1', params=None):
    return Request({'type': 'http', 'method': method, 'path': path, 'headers': [], 'path_params': params or {'job_id': 1}})


def test_path_guard(marked):
    from core.controlled_request import authorize_request
    with pytest.raises(HTTPException) as exc:
        authorize_request(marked, request(), actor(2))
    assert exc.value.detail['code'] == 'controlled_part_group_required'
    authorize_request(marked, request(params={'printer_id': 1}), actor(2))


@pytest.mark.parametrize('value', ['Application/JSON; charset=utf-8', 'application/problem+json', None])
def test_fastapi_json_types(value):
    from core.controlled_request import json_content
    assert json_content(value)
    assert not json_content('text/plain')


@pytest.mark.parametrize("identifier", [1, "1"])
def test_nested_body_guard(marked, identifier):
    from core.controlled_request import authorize_references
    with pytest.raises(HTTPException):
        authorize_references(marked, actor(2), {'nested': {'components': [{'model_id': identifier}]}})


def test_list_guard(marked):
    from modules.jobs.routes.jobs_crud import list_jobs
    # The hidden first row must be excluded before LIMIT, not dropped later.
    rows = list_jobs(status=None, printer_id=None, current_user=actor(2), db=marked, limit=1)
    assert [r.id for r in rows] == [2]


def test_dispatch_guard(marked):
    from core.controlled_parts import dispatch_refusal
    assert dispatch_refusal(marked, 1, 2) == 'controlled_part_printer_group_required'
    assert dispatch_refusal(marked, 1, 1) is None


def test_mjpeg_guard(marked):
    from modules.printers.controlled_camera import frame_check
    marked.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1")); marked.commit()
    frame_check(marked.get_bind(), 1, {'user_id': 1})
    with pytest.raises(HTTPException):
        frame_check(marked.get_bind(), 1, {'user_id': 2})


def test_webrtc_guard(marked, monkeypatch):
    from modules.printers import camera_routes
    marked.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1"))
    marked.execute(text("UPDATE printers SET camera_enabled=1,is_active=1 WHERE id=1")); marked.commit()
    # A reachable transport makes removal fail by returning video, not by an
    # unrelated network error. Both admission and post-signaling guards matter.
    monkeypatch.setattr(camera_routes, 'audit_view', lambda *a, **k: None)
    monkeypatch.setattr(camera_routes, 'get_camera_url', lambda p: 'rtsp://fixture/camera')
    signaling_calls = []
    class Reply:
        status_code = 200
        content = b'sdp'
        headers = {'content-type': 'application/sdp'}
        def json(self): return {'printer_1': {}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs): return Reply()
        async def post(self, *args, **kwargs):
            signaling_calls.append(args)
            return Reply()
    monkeypatch.setattr(camera_routes.httpx, 'AsyncClient', Client)
    req = request('POST', '/api/cameras/1/webrtc', {'printer_id': 1})
    req._body = b'sdp'
    with pytest.raises(HTTPException) as exc:
        asyncio.run(camera_routes.camera_webrtc(1, req, marked, actor()))
    assert exc.value.detail['code'] == 'controlled_part_use_authenticated_mjpeg'

    assert signaling_calls == [], "Controlled work must be refused before signaling"


def test_websocket_guard(marked):
    from core.controlled_events import event_allowed
    marked.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) VALUES(50,1,1,'secret.3mf','completed',CURRENT_TIMESTAMP)")); marked.commit()
    message = {'type': 'job_completed', 'data': {'printer_id': 1, 'print_job_id': 50, 'job_name': 'secret.3mf'}}
    assert not event_allowed(actor(2), message, db=marked)
    assert event_allowed(actor(), message, db=marked)
    assert not event_allowed(actor(), {'type': 'job_failed', 'data': {'printer_id': 1, 'job_name': 'unresolved'}}, db=marked)


def test_alert_recipient_guard(marked):
    from core.recipient_policy import resolve_alert_scope, education_notification_scope
    eligible, _ = resolve_alert_scope(marked, job_id=1)
    assert eligible == {1}
    assert not education_notification_scope(marked, job_id=1)


@pytest.mark.parametrize('state', ['preparing', 'adapter_entered', 'accepted', 'uncertain'])
def test_active_claim_guard(database, tier_license, state):
    from core.dispatch_claims import acquire
    tier_license('enterprise')
    database.execute(text('UPDATE jobs SET printer_id=1 WHERE id=1')); database.commit()
    claim = acquire(database.get_bind(), 1, 1, 'a'*64, 'b'*64, 'c'*64)
    assert claim.status == 'acquired'
    database.execute(text('UPDATE printer_dispatch_claims SET state=:state'), {'state': state}); database.commit()
    with pytest.raises(HTTPException) as exc:
        change_mark(database, 'files', 1, mark(), actor())
    assert exc.value.detail['code'] == 'controlled_part_work_active'


@pytest.mark.parametrize('days', [0, 3651])
def test_retention_bounds_guard(days):
    with pytest.raises(ValueError):
        mark(retention_days=days)


def test_audience_is_independent_and_gdpr_membership(marked):
    from core.controlled_parts import allowed
    marked.execute(text('UPDATE users SET group_id=2 WHERE id=1')); marked.commit()
    assert allowed(marked, 'file', 1, actor())
    marked.execute(text('UPDATE users SET group_id=1 WHERE id=2')); marked.commit()
    assert not allowed(marked, 'file', 1, actor(2))


def test_aggregate_redaction_and_printer_control(marked):
    from core.controlled_redaction import redact_export, printer_status
    marked.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1")); marked.commit()
    payload = {'items': [{'job_id': 1, 'name': 'Arbitrary title', 'notes': 'Secret notes'}], 'breakdown': {'Part': 3}}
    value = redact_export(marked, actor(2), payload)
    assert value['items'][0]['name'] == '[controlled]'
    assert value['items'][0]['notes'] == '[controlled]'
    assert printer_status(marked, 1, actor(2), {'status': 'printing', 'job_name': 'secret'}) == {'status': 'printing', 'job_name': '[controlled]'}


def test_external_copy_guard(marked):
    from core.controlled_events import external_payload
    safe = external_payload(marked, {'job_id': 1, 'title': 'secret', 'message': 'secret', 'metadata': {'file': 'secret'}})
    assert 'secret' not in str(safe)
    assert safe['title'] == 'Controlled work'


def test_marked_job_delete_but_file_only_job_delete_allowed(marked):
    from core.controlled_request import guard_source_changes
    req = request('DELETE')
    guard_source_changes(marked, req, actor())
    change_mark(marked, 'jobs', 1, mark(), actor())
    with pytest.raises(HTTPException):
        guard_source_changes(marked, req, actor())


def test_acknowledged_backup_guard(marked):
    from core.controlled_exports import acknowledge_export
    with pytest.raises(HTTPException) as exc:
        acknowledge_export(marked, actor(2), False, 'backup')
    assert exc.value.detail['code'] == 'controlled_part_acknowledgement_required'
    assert acknowledge_export(marked, actor(2), True, 'backup')


def test_marking_preview_reads_do_not_audit(marked):
    from modules.organizations.routes_controlled_parts import get_mark
    before = marked.execute(text('SELECT count(*) FROM audit_logs')).scalar()
    get_mark('files', 1, actor(), marked)
    get_mark('files', 1, actor(), marked)
    assert marked.execute(text('SELECT count(*) FROM audit_logs')).scalar() == before


def test_retention_preview_keeps_organization_fence(marked):
    from modules.organizations.routes_controlled_retention import preview
    assert preview(actor(), marked)['files'] == [1]
    # Membership remains explicit and valid while the user's tenant changes.
    assert preview({**actor(), 'group_id': 2}, marked)['files'] == []


@pytest.mark.parametrize('event_type', ['printer.state_changed', 'printer_status', 'printer_telemetry'])
def test_websocket_printer_status_redacts_nested_names_but_preserves_control(marked, monkeypatch, event_type):
    import core.db
    from core.controlled_events import audience_messages
    marked.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1")); marked.commit()
    monkeypatch.setattr(core.db, 'engine', marked.get_bind())
    message = {'type': event_type, 'data': {'printer_id': 1, 'new_state': 'printing', 'telemetry': {'job_name': 'secret', 'progress': 5}}}
    member, outsider = audience_messages([actor(), actor(2)], message)
    assert member == message
    assert outsider['data']['new_state'] == 'printing'
    assert outsider['data']['telemetry'] == {'job_name': '[controlled]', 'progress': 5}


def test_upload_duplicate_has_no_controlled_oracle(marked):
    from modules.models_library.upload_storage import _visible_upload_duplicate
    marked.execute(text("UPDATE print_files SET file_hash='owned-hash' WHERE id=1")); marked.commit()
    assert _visible_upload_duplicate(marked, 'owned-hash', actor(2)) is None
    assert _visible_upload_duplicate(marked, 'owned-hash', actor())['existing_file_id'] == 1


def test_education_work_cannot_be_marked(database, tier_license):
    tier_license('enterprise')
    database.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) VALUES(1,1,'class','class','Class','CLASS','active',1,1)")); database.commit()
    with pytest.raises(HTTPException) as exc:
        change_mark(database, 'files', 1, mark(), actor())
    assert exc.value.detail['code'] == 'controlled_part_education_owned'
    assert not database.execute(text('SELECT 1 FROM controlled_files')).first()


def test_unmarked_frame_and_event_filters_open_no_orm_session(database, monkeypatch):
    import core.db
    import sqlalchemy.orm
    from modules.printers import controlled_camera
    from core.controlled_events import audience_messages
    def unexpected(*a, **k):
        raise AssertionError('Unmarked work opened an ORM session')
    monkeypatch.setattr(core.db, 'engine', database.get_bind())
    monkeypatch.setattr(controlled_camera, 'Session', unexpected)
    monkeypatch.setattr(sqlalchemy.orm, 'Session', unexpected)
    # Connection existence queries are allowed; no Session may be created.
    from sqlalchemy import event
    queries = []
    event.listen(database.get_bind(), 'before_cursor_execute', lambda *args: queries.append(args[2]))
    guard = controlled_camera.frame_guard(database.get_bind(), 1)
    guard({'user_id': 2}); guard({'user_id': 2})
    assert len(queries) == 1
    message = {'type': 'printer_status', 'data': {'printer_id': 1}}
    assert audience_messages([actor(2)], message) == [message]
