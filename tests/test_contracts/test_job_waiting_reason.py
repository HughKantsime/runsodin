"""GET /jobs: a queued job says what it is waiting for (spec first-run-followups item 4).

waiting_reason is computed on read from what the database already holds. Each test builds the
condition, reads the reason, fixes the condition and reads again: the reason must be gone.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Response
from sqlalchemy import event, text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401

OPERATOR = {'id': 3, 'role': 'operator', 'group_id': 1, 'is_active': True}
VIEWER = {'id': 4, 'role': 'viewer', 'group_id': 1, 'is_active': True}
OTHER_ORG = {'id': 5, 'role': 'viewer', 'group_id': 2, 'is_active': True}
# A scoped token that may read jobs but not printers.
JOBS_ONLY = {**VIEWER, '_token_scopes': ['read:jobs']}


def stamp(moment):
    return moment.strftime('%Y-%m-%d %H:%M:%S')


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def add_job(db, identifier, status, **columns):
    values = {**dict(id=identifier, item_name=f'Waiting {identifier}', status=status, charged_to_org_id=1, quantity=1,
                     hold=0, is_locked=0, priority=3, created_at=stamp(utcnow()), updated_at=stamp(utcnow())), **columns}
    db.execute(text(f"INSERT INTO jobs({','.join(values)}) VALUES({','.join(':' + key for key in values)})"), values)
    db.commit()


def run(db, sql, **params):
    db.execute(text(sql), params)
    db.commit()


def online(db, printer_id, state='IDLE'):
    run(db, 'UPDATE printers SET gcode_state=:state,last_seen=:seen WHERE id=:id', state=state, seen=stamp(utcnow()), id=printer_id)


def refuse(db, job_id, code, *, kind='gate_refused', minutes_ago=5, printer_id=9, at=None):
    run(db, "INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,code,detail,occurred_at) "
            "VALUES(:id,:job,:printer,1,:kind,:code,'{}',:at)", id=f'{job_id}-{kind}-{minutes_ago}', job=job_id, printer=printer_id,
        kind=kind, code=code, at=at or (utcnow() - timedelta(minutes=minutes_ago)).isoformat())


def reasons(db, user=OPERATOR):
    """{job id: waiting_reason} exactly as GET /jobs computes it for this caller."""
    from modules.jobs.routes.jobs_crud import list_jobs
    db.expire_all()
    rows = list_jobs(status=None, printer_id=None, finished_since=None, order='queue', org_id=None, limit=500, offset=0,
                     q=None, held=None, unassigned=False, response=Response(), current_user=user, _agent_scope={}, db=db)
    return {row.id: row.waiting_reason for row in rows if row.id >= 900}  # the fixture database has jobs of its own


@pytest.fixture
def db(review_db, monkeypatch):  # noqa: F811
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    run(review_db, "UPDATE printers SET shared=0,tags='[]',slot_count=4")
    run(review_db, "DELETE FROM system_config WHERE key='scheduler_blackout'")
    run(review_db, "INSERT INTO system_config(key,value) VALUES('scheduler_blackout',:value)",
        value=json.dumps({'start': '00:00', 'end': '00:00'}))  # quiet hours off unless a test turns them on
    online(review_db, 9)
    online(review_db, 10)
    return review_db


# ---- each reason, and that it clears ----

def test_approval_and_hold_come_from_the_job_itself(db):
    add_job(db, 901, 'submitted')
    add_job(db, 902, 'pending', hold=1)
    add_job(db, 903, 'scheduled', hold=1, printer_id=9)
    found = reasons(db)
    assert found[901] == {'code': 'awaiting_approval'}
    assert found[902] == {'code': 'on_hold'} and found[903] == {'code': 'on_hold'}
    run(db, "UPDATE jobs SET status='pending' WHERE id=901")
    run(db, 'UPDATE jobs SET hold=0 WHERE id IN (902,903)')
    assert reasons(db) == {901: None, 902: None, 903: None}


@pytest.mark.parametrize('status', ['printing', 'paused', 'completed', 'failed', 'cancelled', 'rejected'])
def test_printing_and_finished_jobs_have_no_reason(db, status):
    # Even with everything that would make a queued job wait: held, refused, on an offline printer.
    run(db, 'UPDATE printers SET last_seen=NULL WHERE id=9')
    add_job(db, 905, status, hold=1, printer_id=9)
    refuse(db, 905, 'MATERIAL_MISMATCH')
    assert reasons(db)[905] is None


def test_a_job_that_is_simply_next_has_no_reason(db):
    add_job(db, 906, 'pending')
    add_job(db, 907, 'scheduled', printer_id=9)
    assert reasons(db) == {906: None, 907: None}


def test_needs_filament_mapping_until_a_mapping_is_saved(db):
    add_job(db, 910, 'scheduled', printer_id=9)
    refuse(db, 910, 'AMS_MAPPING_REQUIRED')
    assert reasons(db)[910] == {'code': 'needs_filament_mapping'}
    run(db, "UPDATE jobs SET ams_mapping='[0]' WHERE id=910")
    assert reasons(db)[910] is None


def test_printer_offline_then_back(db):
    add_job(db, 920, 'scheduled', printer_id=9)
    run(db, 'UPDATE printers SET last_seen=:seen WHERE id=9', seen=stamp(utcnow() - timedelta(minutes=10)))
    assert reasons(db)[920] == {'code': 'printer_offline', 'printer_id': 9}
    online(db, 9)
    assert reasons(db)[920] is None


def test_printer_busy_carries_the_running_prints_estimated_end(db):
    add_job(db, 930, 'scheduled', printer_id=9)
    online(db, 9, 'RUNNING')
    assert reasons(db)[930] == {'code': 'printer_busy', 'printer_id': 9}  # no estimate known: no end time
    run(db, "INSERT INTO print_jobs(printer_id,job_name,status,started_at,remaining_minutes) "
            "VALUES(9,'other.gcode','running',:start,45)", start=stamp(utcnow() - timedelta(minutes=20)))
    reason = reasons(db)[930]
    assert reason['code'] == 'printer_busy' and reason['printer_id'] == 9
    assert abs((reason['until'] - (utcnow() + timedelta(minutes=45))).total_seconds()) < 60
    online(db, 9, 'PAUSE')  # a paused print has no trustworthy end
    assert reasons(db)[930] == {'code': 'printer_busy', 'printer_id': 9}
    online(db, 9, 'FINISH')
    assert reasons(db)[930] is None


def test_last_dispatch_refusal_stays_until_the_next_attempt(db):
    add_job(db, 940, 'scheduled', printer_id=9)
    refuse(db, 940, 'MATERIAL_MISMATCH')
    from modules.printers.integration_floor import refusal_words
    assert reasons(db)[940] == {'code': 'dispatch_refused', 'detail': refusal_words('MATERIAL_MISMATCH')}
    refuse(db, 940, None, kind='dispatch_uploading', minutes_ago=1)
    assert reasons(db)[940] is None


def test_a_live_printer_state_outranks_an_old_refusal(db):
    add_job(db, 941, 'scheduled', printer_id=9)
    refuse(db, 941, 'PRINTER_NOT_IDLE_OR_ERROR')
    online(db, 9, 'RUNNING')
    assert reasons(db)[941]['code'] == 'printer_busy'


def test_quiet_hours_until_they_end(db):
    now = utcnow()
    window = {'start': (now - timedelta(hours=1)).strftime('%H:%M'), 'end': (now + timedelta(hours=2)).strftime('%H:%M')}
    run(db, "UPDATE system_config SET value=:value WHERE key='scheduler_blackout'", value=json.dumps(window))
    add_job(db, 950, 'scheduled', printer_id=9, scheduled_start=stamp(now + timedelta(hours=3)))
    add_job(db, 951, 'scheduled', printer_id=10, scheduled_start=stamp(now + timedelta(minutes=5)))  # planned inside: not waiting on them
    found = reasons(db)
    assert found[950]['code'] == 'quiet_hours' and set(found[950]) == {'code', 'until'}
    assert found[950]['until'].strftime('%H:%M') == window['end'] and now < found[950]['until'] <= now + timedelta(hours=2)
    assert found[951] is None
    run(db, "UPDATE system_config SET value=:value WHERE key='scheduler_blackout'", value=json.dumps({'start': '00:00', 'end': '00:00'}))
    assert reasons(db)[950] is None


def test_no_printer_has_the_material_until_one_is_loaded(db):
    add_job(db, 960, 'pending', filament_type='PETG')
    assert reasons(db)[960] == {'code': 'no_printer_can_run_it', 'detail': 'no printer has PETG loaded'}
    run(db, "UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=10")
    assert reasons(db)[960] is None


def test_material_reason_follows_the_schedulers_material_mode(db):
    add_job(db, 961, 'pending', filament_type='PETG')
    run(db, "INSERT INTO system_config(key,value) VALUES('scheduler_material_mode',:value)", value=json.dumps({'mode': 'off'}))
    assert reasons(db)[961] is None


def test_pinned_job_names_the_material_but_never_the_printer(db):
    add_job(db, 962, 'pending', filament_type='PETG', target_type='specific', target_filter='9')
    run(db, "UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=10")  # another printer has it: the pin still blocks
    reason = reasons(db)[962]
    assert reason == {'code': 'no_printer_can_run_it', 'detail': 'printer pinned to this job does not have PETG loaded'}
    assert 'P1S-09' not in json.dumps(reason)
    run(db, "UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=9")
    assert reasons(db)[962] is None


def test_no_printer_has_the_tag_until_one_does(db):
    add_job(db, 963, 'pending', required_tags='["enclosed"]')
    assert reasons(db)[963] == {'code': 'no_printer_can_run_it', 'detail': 'no printer has the tag "enclosed"'}
    run(db, "UPDATE printers SET tags='[\"enclosed\"]' WHERE id=10")
    assert reasons(db)[963] is None


def test_no_active_printer_at_all(db):
    add_job(db, 964, 'pending')
    run(db, 'UPDATE printers SET is_active=0')
    assert reasons(db)[964] == {'code': 'no_printer_can_run_it'}
    run(db, 'UPDATE printers SET is_active=1 WHERE id=10')
    assert reasons(db)[964] is None


def test_an_unusable_pin_is_left_to_pinned_printer_issue(db):
    add_job(db, 965, 'pending', target_type='specific', target_filter='9')
    run(db, 'UPDATE printers SET is_active=0 WHERE id=9')
    assert reasons(db)[965] is None  # the row already says the pinned printer is inactive


# ---- visibility ----

def test_a_caller_gets_reasons_only_for_jobs_they_may_see(db):
    add_job(db, 970, 'submitted')                       # organization 1
    add_job(db, 971, 'submitted', charged_to_org_id=2)  # organization 2
    assert reasons(db, VIEWER) == {970: {'code': 'awaiting_approval'}}
    assert reasons(db, OTHER_ORG) == {971: {'code': 'awaiting_approval'}}


def test_a_printer_the_caller_may_not_read_is_not_identified(db):
    add_job(db, 972, 'scheduled', printer_id=9)
    online(db, 9, 'RUNNING')
    run(db, "INSERT INTO print_jobs(printer_id,job_name,status,started_at,remaining_minutes) "
            "VALUES(9,'other.gcode','running',:start,45)", start=stamp(utcnow()))
    assert set(reasons(db, VIEWER)[972]) == {'code', 'printer_id', 'until'}
    assert reasons(db, JOBS_ONLY)[972] == {'code': 'printer_busy'}  # no printer id, no end time
    # Another organization's private printer: the job still says it is waiting, the printer stays anonymous.
    run(db, "INSERT INTO printers(id,name,api_type,is_active,org_id,shared,gcode_state,last_seen,slot_count,tags) "
            "VALUES(11,'Their printer','bambu',1,2,0,'RUNNING',:seen,4,'[]')", seen=stamp(utcnow()))
    add_job(db, 975, 'scheduled', printer_id=11)
    assert reasons(db, VIEWER)[975] == {'code': 'printer_busy'}
    run(db, 'UPDATE printers SET shared=1 WHERE id=11')
    assert reasons(db, VIEWER)[975] == {'code': 'printer_busy', 'printer_id': 11}


def test_material_and_tag_detail_is_withheld_without_printer_read(db):
    add_job(db, 973, 'pending', filament_type='PETG')
    add_job(db, 974, 'pending', required_tags='["enclosed"]')
    assert reasons(db, JOBS_ONLY) == {973: {'code': 'no_printer_can_run_it'}, 974: {'code': 'no_printer_can_run_it'}}


@pytest.mark.parametrize('status', ['pending', 'scheduled'])
def test_dispatch_refusal_detail_requires_printer_access(db, status):
    run(db, "INSERT INTO printers(id,name,api_type,is_active,org_id,shared,gcode_state,last_seen) "
            "VALUES(11,'Refused printer','bambu',1,1,0,'IDLE',:seen)", seen=stamp(utcnow()))
    add_job(db, 976, status, printer_id=11)
    refuse(db, 976, 'PRINTER_MAINTENANCE_HOLD', printer_id=11)
    assert reasons(db, VIEWER)[976] == {
        'code': 'dispatch_refused', 'detail': 'Printer is on a maintenance hold'}
    assert reasons(db, JOBS_ONLY)[976] == {'code': 'dispatch_refused'}
    run(db, 'UPDATE printers SET org_id=2 WHERE id=11')
    assert reasons(db, VIEWER)[976] == {'code': 'dispatch_refused'}
    run(db, 'UPDATE printers SET shared=1 WHERE id=11')
    assert reasons(db, VIEWER)[976]['detail'] == 'Printer is on a maintenance hold'


@pytest.mark.parametrize('assigned_printer', [None, 10])
@pytest.mark.parametrize('refusal_printer', [9, 11, None])
def test_refusal_detail_uses_the_attempted_printer_after_reassignment(db, assigned_printer, refusal_printer):
    run(db, "INSERT INTO printers(id,name,api_type,is_active,org_id,shared) "
            "VALUES(11,'Hidden printer','bambu',1,2,0)")
    add_job(db, 982, 'pending', printer_id=assigned_printer)
    refuse(db, 982, 'PRINTER_MAINTENANCE_HOLD', printer_id=refusal_printer)
    expected = {'code': 'dispatch_refused'}
    if refusal_printer == 9:
        expected['detail'] = 'Printer is on a maintenance hold'
    assert reasons(db, VIEWER)[982] == expected
    assert reasons(db, JOBS_ONLY)[982] == {'code': 'dispatch_refused'}
    # A later attempt clears the old refusal even when it uses another printer.
    refuse(db, 982, None, kind='dispatch_uploading', minutes_ago=1, printer_id=10)
    assert reasons(db, VIEWER)[982] is None


@pytest.mark.parametrize('retry_kind', ['dispatch_uploading', 'dispatch_starting', 'dispatch_dispatched', 'dispatch_failed'])
@pytest.mark.parametrize('reverse_rows', [False, True])
def test_a_dispatch_event_tied_with_a_refusal_clears_it(db, retry_kind, reverse_rows):
    add_job(db, 984, 'scheduled', printer_id=9)
    refuse(db, 984, 'MATERIAL_MISMATCH', at='2026-11-14 12:00:00')
    refuse(db, 984, None, kind=retry_kind, minutes_ago=1, at='2026-11-14 12:00:00')
    run(db, f'PRAGMA reverse_unordered_selects={int(reverse_rows)}')
    assert reasons(db, VIEWER)[984] is None


@pytest.mark.parametrize('other_printer', ['hidden', 'missing', 'visible', 'shared'])
@pytest.mark.parametrize('reverse_rows', [False, True])
def test_tied_refusals_require_access_to_every_attempted_printer(db, other_printer, reverse_rows):
    add_job(db, 985, 'scheduled', printer_id=9)
    run(db, "INSERT INTO printers(id,name,api_type,is_active,org_id,shared) "
            "VALUES(11,'Other printer','bambu',1,:org,:shared)",
        org=2 if other_printer in ('hidden', 'shared') else 1, shared=int(other_printer == 'shared'))
    # IDs choose stable wording, never authority: even an earlier-ID hidden row
    # must withhold the later-ID visible row's detail.
    db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,kind,code,occurred_at) "
                    "VALUES(:id,985,:printer,'gate_refused',:code,'2026-11-14 12:00:00')"), [
        {'id': 'tie-a', 'printer': None if other_printer == 'missing' else 11, 'code': 'MATERIAL_MISMATCH'},
        {'id': 'tie-z', 'printer': 9, 'code': 'PRINTER_MAINTENANCE_HOLD'},
    ])
    db.commit()
    run(db, f'PRAGMA reverse_unordered_selects={int(reverse_rows)}')
    expected = {'code': 'dispatch_refused'}
    if other_printer in ('visible', 'shared'):
        expected['detail'] = 'Printer is on a maintenance hold'
    assert reasons(db, VIEWER)[985] == expected
    assert reasons(db, JOBS_ONLY)[985] == {'code': 'dispatch_refused'}


def test_null_org_refusal_printer_follows_unscoped_resource_visibility(db):
    run(db, "INSERT INTO printers(id,name,api_type,is_active,org_id,shared) "
            "VALUES(11,'Unscoped printer','bambu',1,NULL,0)")
    add_job(db, 986, 'scheduled', printer_id=9)
    refuse(db, 986, 'PRINTER_MAINTENANCE_HOLD', printer_id=11)
    assert reasons(db, VIEWER)[986] == {
        'code': 'dispatch_refused', 'detail': 'Printer is on a maintenance hold'}
    assert reasons(db, JOBS_ONLY)[986] == {'code': 'dispatch_refused'}


def test_jobs_and_print_floor_share_dispatch_event_classification(db, monkeypatch):
    from core import job_timeline
    from modules.printers.integration_floor import _refusals
    add_job(db, 987, 'scheduled', printer_id=9)
    refuse(db, 987, 'MATERIAL_MISMATCH')
    # An existing valid timeline kind stands in for a newly classified attempt
    # event, without changing the schema. Both readers must use that classification.
    kinds = ('gate_refused', 'dispatch_uploading', 'dispatch_starting', 'dispatch_dispatched', 'dispatch_failed', 'started')
    monkeypatch.setattr(job_timeline, 'DISPATCH_EVENT_KINDS', kinds, raising=False)
    assert reasons(db, VIEWER)[987]['code'] == 'dispatch_refused'
    assert 987 in _refusals(db, [987])
    refuse(db, 987, None, kind='started', minutes_ago=1)
    assert reasons(db, VIEWER)[987] is None
    assert _refusals(db, [987]) == {}


@pytest.mark.parametrize('role', ['admin', 'viewer'])
def test_busy_estimate_with_an_unscoped_actor(db, role):
    from modules.printers.models import Printer
    from modules.printers.services import printer_wait_states
    add_job(db, 983, 'printing', printer_id=9, charged_to_org_id=None)
    online(db, 9, 'RUNNING')
    run(db, "INSERT INTO print_jobs(printer_id,scheduled_job_id,job_name,status,started_at,remaining_minutes) "
            "VALUES(9,983,'current.gcode','running',:start,45)", start=stamp(utcnow()))
    now = utcnow()
    actor = {**VIEWER, 'role': role, 'group_id': None}
    printer = db.get(Printer, 9)
    assert printer_wait_states(db, [printer], now, actor)[9] == ('busy', now + timedelta(minutes=45))


def test_busy_estimate_requires_access_to_the_latest_running_job(db):
    add_job(db, 977, 'scheduled', printer_id=9)
    add_job(db, 978, 'printing', printer_id=9, charged_to_org_id=2)
    online(db, 9, 'RUNNING')
    # An older visible print must not provide a fallback estimate for the hidden current print.
    run(db, "INSERT INTO print_jobs(printer_id,job_name,status,started_at,remaining_minutes) "
            "VALUES(9,'older.gcode','running',:start,10)", start=stamp(utcnow() - timedelta(hours=1)))
    run(db, "INSERT INTO print_jobs(printer_id,scheduled_job_id,job_name,status,started_at,remaining_minutes) "
            "VALUES(9,978,'current.gcode','running',:start,45)", start=stamp(utcnow()))
    assert 978 not in reasons(db, VIEWER)
    assert reasons(db, VIEWER)[977] == {'code': 'printer_busy', 'printer_id': 9}
    run(db, 'UPDATE jobs SET charged_to_org_id=1 WHERE id=978')
    assert abs((reasons(db, VIEWER)[977]['until'] - (utcnow() + timedelta(minutes=45))).total_seconds()) < 60


# ---- cost and wire shape ----

@pytest.mark.parametrize('backlog_size', [1, 1000])
def test_detail_material_query_only_loads_the_requested_pending_job(db, monkeypatch, backlog_size):
    from modules.jobs.routes.jobs_crud import get_job
    from modules.jobs import scheduler_materials
    add_job(db, 979, 'pending', filament_type='PETG')
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,hold,filament_type) "
                    "VALUES(:id,'Backlog','pending',:org,0,'PLA')"),
               [{'id': 2000 + i, 'org': 1 if i % 2 else 2} for i in range(backlog_size)])
    db.commit()
    parsed, queried = [], []
    original = scheduler_materials.needed_materials

    def counted(*args):
        parsed.append(args)
        return original(*args)

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if '/* scheduler material plan */' in statement:
            queried.append((statement, parameters))

    monkeypatch.setattr(scheduler_materials, 'needed_materials', counted)
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        job = get_job(979, current_user=VIEWER, _agent_scope={}, db=db)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert job['waiting_reason']['code'] == 'no_printer_can_run_it'
    assert job['waiting_reason']['detail'] == 'no printer has PETG loaded'
    assert len(parsed) == 1
    assert len(queried) == 1
    statement, parameters = queried[0]
    # Constrain rows in SQL, not after fetching and parsing the whole backlog.
    assert 'j.id IN' in statement and tuple(parameters) == (979,)


def test_reasons_cost_a_fixed_number_of_queries_however_many_jobs(db, monkeypatch):
    """Counted around the waiting-reason step alone (the name lookup before it is not under test)."""
    from modules.jobs import waiting_reason
    engine, original, counts = db.get_bind(), waiting_reason.add_waiting_reasons, []

    def counted(*args, **kwargs):
        statements = []
        listener = lambda *a, **k: statements.append(1)  # noqa: E731
        event.listen(engine, 'before_cursor_execute', listener)
        try:
            return original(*args, **kwargs)
        finally:
            event.remove(engine, 'before_cursor_execute', listener)
            counts.append(len(statements))

    monkeypatch.setattr(waiting_reason, 'add_waiting_reasons', counted)

    def seed(start):  # one job per kind of read: material, busy printer, refusal, quiet hours, tag
        add_job(db, start, 'pending', filament_type='PETG')
        add_job(db, start + 1, 'scheduled', printer_id=9, scheduled_start=stamp(utcnow() + timedelta(hours=1)))
        add_job(db, start + 2, 'scheduled', printer_id=10, scheduled_start=stamp(utcnow() + timedelta(hours=1)))
        add_job(db, start + 3, 'scheduled', printer_id=10, scheduled_start=stamp(utcnow() + timedelta(hours=1)))
        add_job(db, start + 4, 'pending', required_tags='["enclosed"]')
        refuse(db, start + 2, 'MATERIAL_MISMATCH', printer_id=10)

    def measure():
        found = reasons(db)
        assert {found[i]['code'] for i in found if i % 10 != 3} == {'no_printer_can_run_it', 'printer_busy', 'dispatch_refused'}
        return counts[-1]

    online(db, 9, 'RUNNING')
    seed(1000)
    small = measure()
    for index in range(1, 12):
        seed(1000 + index * 10)
    assert len(reasons(db)) == 60
    assert measure() == small
    assert small <= 7  # refusals, running prints, quiet hours, printers, material mode, loaded slots, needed materials


def test_wire_shape_on_list_and_detail(bom_client, db):  # noqa: F811
    from modules.jobs.routes import jobs_crud
    bom_client.app.include_router(jobs_crud.router)
    add_job(db, 980, 'scheduled', printer_id=9)
    add_job(db, 981, 'printing', printer_id=10)
    online(db, 9, 'RUNNING')
    run(db, "INSERT INTO print_jobs(printer_id,job_name,status,started_at,remaining_minutes) "
            "VALUES(9,'other.gcode','running',:start,45)", start=stamp(utcnow()))
    listed = {job['id']: job['waiting_reason'] for job in bom_client.get('/jobs').json()}
    assert listed[981] is None
    assert listed[980]['code'] == 'printer_busy' and listed[980]['printer_id'] == 9 and listed[980]['detail'] is None
    assert datetime.fromisoformat(listed[980]['until'].replace('Z', '+00:00')).tzinfo is not None  # an instant, not a bare local time
    assert bom_client.get('/jobs/980').json()['waiting_reason']['code'] == 'printer_busy'


def test_quiet_hours_end_follows_the_saved_time_zone(review_db):  # noqa: F811
    """Merged with the quiet-hours time zone work: the end is on the window's own clock."""
    from datetime import datetime
    from core.scheduler_settings import set_blackout
    from modules.jobs.waiting_reason import quiet_hours_end
    set_blackout(review_db, "22:30", "05:30", "America/New_York")
    review_db.commit()
    # 03:00 UTC on Jan 16 is 22:00 the evening before in New York: not yet quiet hours.
    assert quiet_hours_end(review_db, datetime(2026, 1, 16, 3, 0)) is None
    # 04:00 UTC is 23:00 in New York: quiet until 05:30 local = 10:30 UTC.
    assert quiet_hours_end(review_db, datetime(2026, 1, 16, 4, 0)) == datetime(2026, 1, 16, 10, 30)
    # July: 05:30 local is 09:30 UTC (daylight time).
    assert quiet_hours_end(review_db, datetime(2026, 7, 16, 4, 0)) == datetime(2026, 7, 16, 9, 30)
