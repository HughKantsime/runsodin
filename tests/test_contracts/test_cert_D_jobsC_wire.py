"""cert-D jobsC: capture the actual HTTP producer shapes the Jobs/Timeline/Analytics UI consumes.

One owned SQLite scenario (real bootstrap schema, real FastAPI routers, no printers contacted)
is driven through the exact request bodies the SPA sends; every response is written to
frontend/src/test-fixtures/certD-jobsC-wire.json for the *.certDjobsC.cert.test.tsx consumers.
The fixture is regenerated on every run (some analytics values depend on the wall clock),
and the load-bearing producer values are asserted here before the file is written.
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_archive_reprint_behavior import reprint_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_recent_terminal_jobs import picture
from tests.test_contracts.cert_D_capture import publish

# Exercises Pro-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")

FIXTURE = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/certD-jobsC-wire.json'
CREATED = '2026-10-01 08:00:00'


def seed(db):
    # Raw fixture printers lack ORM defaults; give them the values the create route would.
    db.execute(text("UPDATE printers SET slot_count=COALESCE(slot_count,1),tags=COALESCE(tags,'[]'),"
                    "timelapse_enabled=COALESCE(timelapse_enabled,0),shared=COALESCE(shared,0)"))
    db.execute(text("INSERT INTO products(id,name) VALUES(901,'CertC product')"))
    db.execute(text("INSERT INTO orders(id,order_number,status) VALUES(901,'CERTC-901','pending')"))
    db.execute(text("INSERT INTO order_items(id,order_id,product_id,quantity) VALUES(901,901,901,1)"))
    db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,thumbnail_b64) "
                    "VALUES(301,101,'certc.3mf','certc.3mf','/opaque/certc.3mf',1,:image)"), {'image': picture()})
    rows = [
        # id, name, status, printer, due, sched_start, sched_end, act_start, act_end, order_item, created, duration, file
        (201, 'CertC adhoc pending A', 'pending', None, '2026-10-10 00:00:00', None, None, None, None, None, '2026-10-01 08:00:00', 2.0, None),
        (202, 'CertC adhoc pending B', 'pending', None, '2026-10-20 00:00:00', None, None, None, None, None, '2026-10-01 09:00:00', 1.5, None),
        (203, 'CertC order scheduled', 'scheduled', 10, None, '2026-10-12 02:00:00', '2026-10-12 05:00:00', None, None, 901, '2026-10-01 10:00:00', 3.0, None),
        (204, 'CertC printing', 'printing', 9, '2026-10-09 00:00:00', '2026-10-12 06:00:00', '2026-10-12 09:00:00', '2026-10-12 06:00:00', None, None, '2026-10-01 11:00:00', 3.0, None),
        (205, 'CertC completed overdue', 'completed', 9, '2026-10-09 00:00:00', '2026-10-11 10:00:00', '2026-10-11 12:00:00', '2026-10-11 10:00:00', '2026-10-11 12:30:00', None, '2026-10-01 12:00:00', 2.0, 301),
        (206, 'CertC failed clog', 'failed', 10, None, '2026-10-11 13:00:00', '2026-10-11 14:00:00', '2026-10-11 13:00:00', '2026-10-11 13:45:00', None, '2026-10-01 13:00:00', 1.0, 301),
    ]
    # Eight more terminal outcomes so the recent grid must choose the newest eight of eleven.
    for offset in range(8):
        identifier = 210 + offset
        end_hour = 1 + offset  # 2026-10-10 01:00 .. 08:00, all older than 205/206
        rows.append((identifier, f'CertC history {offset}', 'failed' if offset % 3 == 0 else 'completed', 9,
                     None, None, None, f'2026-10-10 00:00:00', f'2026-10-10 {end_hour:02d}:00:00', None,
                     '2026-10-01 14:00:00', 1.0, None))
    for r in rows:
        db.execute(text(
            "INSERT INTO jobs(id,item_name,status,printer_id,due_date,scheduled_start,scheduled_end,actual_start,actual_end,"
            "order_item_id,created_at,updated_at,duration_hours,print_file_id,model_id,charged_to_org_id,charged_to_user_id,"
            "quantity,priority,hold,is_locked,filament_type) "
            "VALUES(:id,:name,:status,:printer,:due,:ss,:se,:as_,:ae,:oi,:created,:created,:dur,:file,101,1,3,1,3,0,0,'PLA')"),
            dict(zip(['id', 'name', 'status', 'printer', 'due', 'ss', 'se', 'as_', 'ae', 'oi', 'created', 'dur', 'file'], r)))
    db.execute(text("UPDATE jobs SET fail_reason='adhesion' WHERE id IN (210,213)"))
    db.execute(text("UPDATE jobs SET created_at=:c,updated_at=:c WHERE id IN (13,101)"), {'c': CREATED})
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
    db.execute(text("UPDATE jobs SET ams_mapping='[0]' WHERE id=204"))
    db.commit()


@pytest.fixture
def wire_client(reprint_client, review_db):  # noqa: F811
    from modules.jobs.routes import router as jobs_router
    from modules.jobs import scheduler_routes
    from modules.printers import routes_crud
    from modules.reporting.routes import analytics, exports
    from modules.organizations import routes_permissions
    from core.errors import OdinError
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    base, identity, _ = reprint_client
    # Production order: the jobs package registers static paths before /jobs/{job_id}.
    app = FastAPI()
    for router in (jobs_router, scheduler_routes.router, routes_crud.router, analytics.router, exports.router,
                   routes_permissions.router):
        app.include_router(router)
    app.dependency_overrides = base.app.dependency_overrides
    # Same envelope handler core/app.py registers for agent-surface routes (cancel).
    app.add_exception_handler(OdinError, lambda request, exc: JSONResponse(status_code=exc.status, content=exc.to_envelope()))
    client = TestClient(app, raise_server_exceptions=False)
    seed(review_db)
    return client, identity, review_db


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def test_capture_jobs_queue_timeline_recent_and_failure_wire(wire_client):
    client, identity, db = wire_client
    wire = {'scope': 'Owned synthetic SQLite scenario; real routers; operator id 3 org 1; no printer contacted',
            'me_operator': {'id': 3, 'group_id': 1, 'role': 'operator', 'username': 'admin'},
            'me_viewer': {'id': 1, 'group_id': 1, 'role': 'viewer', 'username': 'student'}}
    wire['permissions'] = ok(client.get('/permissions'))
    assert wire['permissions']['action_access']['jobs.edit'] == ['admin', 'operator']
    wire['printers'] = ok(client.get('/printers', params={'active_only': 'true'}))
    assert sorted(p['id'] for p in wire['printers']) == [9, 10]

    wire['jobs_before'] = ok(client.get('/jobs'))
    queue = [j['id'] for j in wire['jobs_before'] if j['status'] in ('pending', 'scheduled')]
    assert queue == [201, 202, 203]
    # Jobs.tsx: drag B (202) onto A (201) -> splice gives [202,201,203].
    wire['reorder_request'] = {'job_ids': [202, 201, 203]}
    wire['reorder_response'] = ok(client.patch('/jobs/reorder', json=wire['reorder_request']))
    assert wire['reorder_response'] == {'reordered': 3}
    db.expire_all()
    assert db.execute(text('SELECT id,queue_position FROM jobs WHERE id IN (201,202,203) ORDER BY queue_position')).all() == [(202, 0), (201, 1), (203, 2)]
    wire['jobs_after_reorder'] = ok(client.get('/jobs'))

    # FailureReasonModal -> updateJobFailure(jobId, reason, notes) body.
    wire['failure_reasons'] = ok(client.get('/failure-reasons'))
    wire['failure_request'] = {'fail_reason': 'clog', 'fail_notes': 'Synthetic nozzle clog at layer 12'}
    wire['failure_response'] = ok(client.patch('/jobs/206/failure', json=wire['failure_request']))
    assert tuple(db.execute(text('SELECT fail_reason,fail_notes,status FROM jobs WHERE id=206')).one()) == ('clog', 'Synthetic nozzle clog at layer 12', 'failed')

    # EditJobModal.handleSubmit body for job 202 after: name, priority 1, printer 9, notes changed.
    wire['edit_request'] = {'item_name': 'CertC adhoc pending B edited', 'quantity': 2, 'priority': 1, 'duration_hours': 1.5,
                            'colors_required': None, 'filament_type': 'PLA', 'notes': 'Synthetic edited notes',
                            'due_date': '2026-10-21', 'pinned_printer_id': 9}
    wire['edit_response'] = ok(client.patch('/jobs/202', json=wire['edit_request']))
    db.execute(text("UPDATE jobs SET updated_at=:c"), {'c': CREATED}); db.commit()
    saved = db.execute(text('SELECT item_name,quantity,priority,duration_hours,filament_type,notes,due_date,target_filter,status FROM jobs WHERE id=202')).one()
    assert tuple(saved)[:6] == ('CertC adhoc pending B edited', 2, 1, 1.5, 'PLA', 'Synthetic edited notes')
    assert str(saved.due_date).startswith('2026-10-21') and saved.target_filter == '9' and saved.status == 'pending'
    # Clearing the pin from the edit modal (null = any printer) is honoured.
    cleared = ok(client.patch('/jobs/202', json={'pinned_printer_id': None}))
    assert cleared['pinned_printer_id'] is None
    ok(client.patch('/jobs/202', json={'pinned_printer_id': 9}))
    wire['jobs_after'] = ok(client.get('/jobs'))

    wire['timeline'] = ok(client.get('/timeline', params={'start_date': '2026-10-12T00:00:00', 'days': 1}))
    assert {(s['job_id'], s['printer_id']) for s in wire['timeline']['slots']} == {(203, 10), (204, 9)}
    wire['timeline_two_day'] = ok(client.get('/timeline', params={'start_date': '2026-10-11T00:00:00', 'days': 2}))
    assert {s['job_id'] for s in wire['timeline_two_day']['slots']} == {203, 204, 205, 206} - {206}  # failed jobs are not timeline statuses
    wire['job_205'] = ok(client.get('/jobs/205'))
    assert (wire['job_205']['duration_hours'], wire['job_205']['actual_start'], wire['job_205']['actual_end']) == (2.0, '2026-10-11T10:00:00', '2026-10-11T12:30:00')
    assert wire['job_205']['ams_mapping'] is None
    wire['job_204'] = ok(client.get('/jobs/204'))
    assert wire['job_204']['ams_mapping'] == [0]
    wire['job_203'] = ok(client.get('/jobs/203'))

    wire['recent_terminal'] = ok(client.get('/jobs/recent-terminal'))
    assert [r['id'] for r in wire['recent_terminal']] == [206, 205, 217, 216, 215, 214, 213, 212]
    assert wire['recent_terminal'][0]['thumbnail_data'].startswith('data:image/png;base64,')
    assert wire['recent_terminal'][1]['duration_seconds'] == 9000.0

    wire['failure_analytics'] = ok(client.get('/analytics/failures', params={'days': 365}))
    wire['analytics'] = ok(client.get('/analytics', params={'days': 365}))
    wire['time_accuracy'] = ok(client.get('/analytics/time-accuracy', params={'days': 365}))
    # Cancel refuses a printing job; Jobs.tsx 'markFailed' uses POST /jobs/{id}/fail instead.
    cancel = client.post('/jobs/204/cancel')
    assert cancel.status_code == 400
    wire['cancel_printing_status'] = cancel.status_code
    wire['cancel_printing_response'] = cancel.json()
    wire['fail_printing_response'] = ok(client.post('/jobs/204/fail'))
    assert wire['fail_printing_response']['status'] == 'failed'
    wire['repeat_response'] = ok(client.post('/jobs/206/repeat'))
    assert wire['repeat_response']['original_job_id'] == 206
    # A job pinned to printer 10, then that printer deactivated: pending with a visible reason.
    pinned = ok(client.post('/jobs', json={'item_name': 'CertC pinned blocked', 'duration_hours': 1, 'printer_id': 10}), 201)
    db.execute(text('UPDATE printers SET is_active=0 WHERE id=10')); db.commit()
    wire['pinned_blocked_job'] = ok(client.get(f"/jobs/{pinned['id']}"))
    assert (wire['pinned_blocked_job']['pinned_printer_id'], wire['pinned_blocked_job']['pinned_printer_issue']) == (10, 'pinned_printer_inactive')
    db.execute(text('UPDATE printers SET is_active=1 WHERE id=10')); db.commit()
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    publish(FIXTURE, wire, indent=1, volatile={'utilization_pct', 'thumbnail_data'})  # hours over a wall-clock window; PNG bytes vary with the zlib build


# ---------------- F04-4.3-01 drag-and-drop reorder (backend) ----------------

def test_reorder_persists_only_pending_scheduled_and_skips_foreign(wire_client):
    client, identity, db = wire_client
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,priority,queue_position) VALUES(299,'Foreign queue',"
                    "'pending',2,1,0,3,77)"))
    db.commit()
    response = ok(client.patch('/jobs/reorder', json={'job_ids': [203, 205, 299, 201]}))
    assert response == {'reordered': 3}  # foreign job 299 skipped; completed 205 counted but unchanged
    db.expire_all()
    rows = dict(db.execute(text('SELECT id,queue_position FROM jobs WHERE id IN (201,203,205,299)')).all())
    assert rows == {203: 0, 205: None, 299: 77, 201: 3}


@pytest.mark.parametrize('role,scopes', [('viewer', None), ('operator', ['read:jobs'])])
def test_reorder_denied_for_viewer_and_read_token_without_writes(wire_client, role, scopes):
    client, identity, db = wire_client
    identity['role'] = role
    if scopes is not None:
        identity.update(_auth_kind='user_api_token', _token_scopes=scopes)
    assert client.patch('/jobs/reorder', json={'job_ids': [202, 201]}).status_code == 403
    db.expire_all()
    assert db.execute(text('SELECT COUNT(*) FROM jobs WHERE queue_position IS NOT NULL')).scalar_one() == 0


def test_reorder_order_drives_next_auto_dispatch_selection(wire_client, monkeypatch):
    import core.db
    from modules.printers.dispatch import _get_next_scheduled_job
    client, identity, db = wire_client
    db.execute(text("UPDATE jobs SET status='scheduled',printer_id=9 WHERE id IN (201,202)"))
    db.commit()
    monkeypatch.setattr(core.db, 'engine', db.get_bind())
    ok(client.patch('/jobs/reorder', json={'job_ids': [201, 202]}))
    assert _get_next_scheduled_job(9)['id'] == 201
    ok(client.patch('/jobs/reorder', json={'job_ids': [202, 201]}))
    assert _get_next_scheduled_job(9)['id'] == 202


def test_reordered_queue_is_listed_in_reordered_order(wire_client):
    client, identity, db = wire_client
    ok(client.patch('/jobs/reorder', json={'job_ids': [202, 201, 203]}))
    listed = [j['id'] for j in ok(client.get('/jobs')) if j['status'] in ('pending', 'scheduled')]
    assert listed == [202, 201, 203]


def test_scheduler_honours_reordered_pending_queue(wire_client):
    from datetime import datetime, timezone
    from modules.jobs.scheduler import Scheduler
    client, identity, db = wire_client
    db.execute(text("UPDATE jobs SET status='completed' WHERE id NOT IN (201,202) AND status IN ('pending','scheduled','printing')"))
    db.execute(text("UPDATE jobs SET due_date=NULL,model_id=NULL,print_file_id=NULL WHERE id IN (201,202)"))
    db.execute(text("UPDATE printers SET is_active=0 WHERE id<>9"))
    db.commit()
    ok(client.patch('/jobs/reorder', json={'job_ids': [202, 201]}))
    Scheduler().run(db, start_date=datetime(2026, 10, 20, tzinfo=timezone.utc)); db.commit(); db.expire_all()
    starts = dict(db.execute(text('SELECT id,scheduled_start FROM jobs WHERE id IN (201,202)')).all())
    assert starts[202] is not None and starts[201] is not None and str(starts[202]) < str(starts[201])


def test_queue_order_is_priority_then_position_nulls_last_then_age(wire_client, monkeypatch):
    """GET /jobs, the scheduler and dispatch share one order on SQLite (PostgreSQL in the native probe)."""
    import core.db
    from modules.printers.dispatch import _get_next_scheduled_job
    client, identity, db = wire_client
    db.execute(text("UPDATE jobs SET status='completed' WHERE status IN ('pending','scheduled') AND id NOT IN (201,202,203)"))
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,priority,created_at) "
                    "VALUES(207,'CertC urgent late','pending',1,1,0,1,'2026-10-02 00:00:00')"))
    db.commit()
    ok(client.patch('/jobs/reorder', json={'job_ids': [207, 202]}))  # positions only set for 207 and 202
    db.execute(text('UPDATE jobs SET queue_position=NULL WHERE id=201')); db.commit()
    listed = [j['id'] for j in ok(client.get('/jobs', params={'status': 'pending'}))]
    # 207 (P1) beats every P3 job; among P3 the placed 202 precedes unplaced 201 despite 201 being older.
    assert listed == [207, 202, 201], listed
    # Dispatch (jobs 201/202 carry stored payloads): same rule for scheduled jobs on one printer.
    db.execute(text("UPDATE jobs SET status='scheduled', printer_id=9 WHERE id IN (201,202)")); db.commit()
    monkeypatch.setattr(core.db, 'engine', db.get_bind())
    assert _get_next_scheduled_job(9)['id'] == 202          # placed beats unplaced (NULLs last) at equal priority
    db.execute(text('UPDATE jobs SET queue_position=0 WHERE id=201')); db.execute(text('UPDATE jobs SET queue_position=5 WHERE id=202')); db.commit()
    assert _get_next_scheduled_job(9)['id'] == 201          # lower position first within the priority
    db.execute(text('UPDATE jobs SET priority=1 WHERE id=202')); db.commit()
    assert _get_next_scheduled_job(9)['id'] == 202          # priority beats position


@pytest.mark.parametrize('route', ['complete', 'fail', 'cancel'])
def test_terminal_transition_clears_queue_position(wire_client, route):
    client, identity, db = wire_client
    source = 'scheduled' if route == 'cancel' else 'printing'
    db.execute(text("UPDATE jobs SET status=:s, queue_position=4 WHERE id=202"), {'s': source}); db.commit()
    response = client.post(f'/jobs/202/{route}')
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.execute(text('SELECT queue_position FROM jobs WHERE id=202')).scalar_one() is None


# ---------------- F04-4.3-04 edit modal (backend) ----------------

def test_edit_denied_for_viewer_and_foreign_job_unchanged(wire_client):
    client, identity, db = wire_client
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,priority) VALUES(298,'Foreign edit','pending',2,1,0,3)"))
    db.commit()
    assert client.patch('/jobs/298', json={'item_name': 'hijack'}).status_code == 404
    identity['role'] = 'viewer'
    assert client.patch('/jobs/202', json={'item_name': 'viewer edit', 'priority': 1}).status_code == 403
    db.expire_all()
    assert db.execute(text("SELECT item_name FROM jobs WHERE id=298")).scalar_one() == 'Foreign edit'
    assert db.execute(text("SELECT item_name,priority FROM jobs WHERE id=202")).one() == ('CertC adhoc pending B', 3)


# ---------------- F04-4.4 Print Again (backend) ----------------

@pytest.mark.parametrize('source', [205, 206])
def test_repeat_completed_or_failed_creates_fresh_pending_job_same_model_printer(wire_client, source):
    client, identity, db = wire_client
    if source == 206:
        ok(client.patch('/jobs/206/failure', json={'fail_reason': 'clog', 'fail_notes': 'x'}))
    before = db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
    body = ok(client.post(f'/jobs/{source}/repeat'))
    assert body['success'] is True and body['original_job_id'] == source and body['new_job_id'] != source
    db.expire_all()
    assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one() == before + 1
    original = db.execute(text('SELECT model_id,printer_id,item_name,quantity,priority,print_file_id FROM jobs WHERE id=:id'), {'id': source}).one()
    clone = db.execute(text('SELECT model_id,printer_id,item_name,quantity,priority,print_file_id,status,actual_start,actual_end,'
                            'fail_reason,fail_notes,scheduled_start,scheduled_end,is_locked,queue_position,submitted_by,notes '
                            'FROM jobs WHERE id=:id'), {'id': body['new_job_id']}).one()
    assert tuple(clone[:6]) == tuple(original)
    assert clone.model_id == 101 and clone.printer_id == (9 if source == 205 else 10)
    assert clone.status == 'pending'
    assert (clone.actual_start, clone.actual_end, clone.fail_reason, clone.fail_notes, clone.scheduled_start, clone.scheduled_end) == (None,) * 6
    assert clone.submitted_by == 3 and clone.notes == f'Repeat of job #{source}'
    # source unchanged
    assert db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': source}).scalar_one() == ('completed' if source == 205 else 'failed')


@pytest.mark.parametrize('role,scopes', [('viewer', None), ('operator', ['read:jobs'])])
def test_repeat_denied_without_write_and_writes_nothing(wire_client, role, scopes):
    client, identity, db = wire_client
    identity['role'] = role
    if scopes is not None:
        identity.update(_auth_kind='user_api_token', _token_scopes=scopes)
    before = db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
    assert client.post('/jobs/205/repeat').status_code == 403
    assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one() == before


# ---------------- F04-4.5-01 failure logging (backend) ----------------

def test_failure_reasons_include_advertised_categories(wire_client):
    client, identity, db = wire_client
    reasons = {r['value']: r['label'] for r in ok(client.get('/failure-reasons'))}
    assert reasons['clog'] == 'Nozzle Clog' and reasons['adhesion'] == 'Bed Adhesion Failure' and reasons['filament_tangle'] == 'Filament Tangle'
    assert len(reasons) == 12 and 'other' in reasons


def test_failure_info_only_on_failed_jobs_and_operator_only(wire_client):
    client, identity, db = wire_client
    assert client.patch('/jobs/205/failure', json={'fail_reason': 'clog'}).status_code == 400
    identity['role'] = 'viewer'
    assert client.patch('/jobs/206/failure', json={'fail_reason': 'clog'}).status_code == 403
    db.expire_all()
    assert db.execute(text('SELECT fail_reason FROM jobs WHERE id IN (205,206) ORDER BY id')).scalars().all() == [None, None]


def test_printing_job_cannot_be_cancelled_into_failure_flow(wire_client):
    """Cancel refuses a printing job; the dedicated fail route (used by Jobs.tsx 'markFailed') works."""
    client, identity, db = wire_client
    response = client.post('/jobs/204/cancel')
    assert response.status_code == 400, response.text
    db.expire_all()
    assert db.execute(text('SELECT status FROM jobs WHERE id=204')).scalar_one() == 'printing'
    # The dedicated fail route works; Jobs.tsx 'Mark job failed' calls it.
    assert ok(client.post('/jobs/204/fail'))['status'] == 'failed'


def test_saved_failure_reason_is_returned_by_queue_list(wire_client):
    client, identity, db = wire_client
    ok(client.patch('/jobs/206/failure', json={'fail_reason': 'clog', 'fail_notes': 'n'}))
    row = next(j for j in ok(client.get('/jobs')) if j['id'] == 206)
    assert row.get('fail_reason') == 'clog' and row.get('fail_notes') == 'n'


# ---------------- F04-4.5-02 failure analytics (backend) ----------------

def test_failure_analytics_groups_by_printer_model_filament_and_reason(wire_client):
    client, identity, db = wire_client
    ok(client.patch('/jobs/206/failure', json={'fail_reason': 'clog', 'fail_notes': 'n'}))
    data = ok(client.get('/analytics/failures', params={'days': 365}))
    # terminal: 101 c (printer 9, no filament), 205 c, 206 f(clog, printer 10), 210..217 with 210/213/216 failed
    assert data['total_failed'] == 4 and data['total_completed'] == 7 and data['overall_success_rate'] == 63.6
    printers = {p['name']: p for p in data['by_printer']}
    assert (printers['P1S-09']['completed'], printers['P1S-09']['failed'], printers['P1S-09']['success_rate']) == (7, 3, 70.0)
    assert (printers['P1S-10']['completed'], printers['P1S-10']['failed'], printers['P1S-10']['success_rate']) == (0, 1, 0.0)
    assert printers['P1S-09']['mtbf_hours'] == 3.0  # failures end 01:00, 04:00, 07:00
    model = next(m for m in data['by_model'] if m['name'] == 'Original model')
    assert (model['completed'], model['failed'], model['success_rate']) == (7, 4, 63.6)
    assert {r['reason']: r['count'] for r in data['top_failure_reasons']} == {'adhesion': 2, 'clog': 1, 'unspecified': 1}
    assert data['top_failure_reasons'][0] == {'reason': 'adhesion', 'count': 2}
    assert sorted(data['by_filament'], key=lambda f: f['type']) == [{'type': 'PLA', 'completed': 6, 'failed': 4, 'failure_rate': 40.0}, {'type': 'unknown', 'completed': 1, 'failed': 0, 'failure_rate': 0.0}]


# ---------------- F04-4.7 time tracking (backend) ----------------

def test_start_and_complete_record_actual_interval_without_touching_estimate(wire_client):
    client, identity, db = wire_client
    db.execute(text("UPDATE jobs SET status='scheduled',printer_id=9,duration_hours=2.25 WHERE id=201")); db.commit()
    started = ok(client.post('/jobs/201/start'))
    assert started['status'] == 'printing' and started['actual_start'] is not None
    done = ok(client.post('/jobs/201/complete'))
    assert done['status'] == 'completed' and done['actual_end'] is not None
    assert done['duration_hours'] == 2.25  # slicer/estimate field retained by the HTTP lifecycle
    assert done['actual_end'] >= done['actual_start']


def test_jobs_export_carries_estimate_and_actual_start_end(wire_client):
    import csv, io
    client, identity, db = wire_client
    response = client.get('/export/jobs', params={'status': 'completed'})
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/csv')
    rows = list(csv.DictReader(io.StringIO(response.text)))
    row = next(r for r in rows if r['ID'] == '205')
    assert row['Duration (hrs)'] == '2.0'
    assert row['Actual Start'].startswith('2026-10-11T10:00:00') and row['Actual End'].startswith('2026-10-11T12:30:00')
    assert 'Actual Duration' not in rows[0]  # actual duration is derivable (start/end), not a column


# ---------------- F04-4.8 timeline (backend) ----------------

def test_timeline_places_jobs_on_printers_and_hides_foreign(wire_client):
    client, identity, db = wire_client
    db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,scheduled_start,scheduled_end,charged_to_org_id,quantity,hold,priority) "
                    "VALUES(297,'Foreign timeline','scheduled',11,'2026-10-12 01:00:00','2026-10-12 02:00:00',2,1,0,3)"))
    db.commit()
    data = ok(client.get('/timeline', params={'start_date': '2026-10-12T00:00:00', 'days': 1}))
    assert data['slot_duration_minutes'] == 30
    assert sorted(p['id'] for p in data['printers']) == [9, 10]
    slots = {s['job_id']: s for s in data['slots']}
    assert set(slots) == {203, 204}
    assert (slots[203]['printer_name'], slots[203]['start'][:19], slots[203]['end'][:19]) == ('P1S-10', '2026-10-12T02:00:00', '2026-10-12T05:00:00')
    assert (slots[204]['printer_name'], slots[204]['start'][:19], slots[204]['end'][:19]) == ('P1S-09', '2026-10-12T06:00:00', '2026-10-12T09:00:00')
    # completed 205 is on 10-11: outside a 1-day window starting 10-12
    assert 205 not in slots


def test_archive_reprint_pins_the_chosen_printer(reprint_client, review_db):  # noqa: F811
    from modules.jobs.pinning import pinned_printer_id
    client, identity, source = reprint_client
    response = client.post('/archives/101/reprint', json={'printer_id': 9})
    assert response.status_code == 200, response.text
    review_db.expire_all()
    row = review_db.execute(text('SELECT target_type,target_filter FROM jobs WHERE id=:id'), {'id': response.json()['job_id']}).one()
    assert pinned_printer_id(*row) == 9


@pytest.mark.parametrize('role', ['admin', 'operator', 'viewer'])
def test_ams_mapping_read_contract_and_rbac(wire_client, role):
    client, identity, db = wire_client
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
    db.execute(text("UPDATE jobs SET ams_mapping='[0]' WHERE id=205"))
    db.commit()
    identity['role'] = role
    assert ok(client.get('/jobs/205'))['ams_mapping'] == [0]
    # The same field never grants access to another organization's job.
    identity['group_id'] = 2
    assert client.get('/jobs/205').status_code in (403, 404)
