"""cert-D jobsB: real Scheduler.run against a bootstrapped SQLite schema.

Claims: F04-4.2-01 (color-match scoring minimizes swaps), F04-4.2-02 (target type
filtering), F04-4.2-03 (availability-window search), F04-4.2-04 (proactive stale
cleanup on every scheduler run). Competing printers, fixed clock, no device I/O.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

START = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)  # outside the 22:30-05:30 blackout


class FixedClock(datetime):
    """datetime whose now() is pinned to START (the scheduler's only clock read)."""

    @classmethod
    def now(cls, tz=None):
        return START if tz is None else START.astimezone(tz)


@pytest.fixture
def fleet(review_db, monkeypatch):
    """Printers 9/10 (P1S, bambu, org 1) plus X1C/bambu 11 and moonraker 12, all idle."""
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs import scheduler as module
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    monkeypatch.setattr(module, 'datetime', FixedClock)
    review_db.execute(text(
        "INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id) VALUES "
        "(11,'X1C-11','Bambu Lab X1C','X1C',1,'bambu',1),"
        "(12,'Voron-12','Voron 2.4','Voron',1,'moonraker',1)"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES (11,1,'PLA'),(12,1,'PLA')"))
    review_db.commit()
    return review_db


def colors(db, mapping):
    for printer_id, color in mapping.items():
        db.execute(text('UPDATE filament_slots SET color=:c WHERE printer_id=:p'), {'c': color, 'p': printer_id})
    db.commit()


def only_active(db, ids):
    db.execute(text(f"UPDATE printers SET is_active=CASE WHEN id IN ({','.join(map(str, ids))}) THEN 1 ELSE 0 END"))
    db.commit()


def add_job(db, job_id, *, name=None, colors_required=None, hours=1, status='pending', printer=None,
            start=None, target_type='specific', target_filter=None, priority=3, hold=False):
    end = start + timedelta(hours=hours) if start else None
    db.execute(text(
        "INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,quantity,duration_hours,hold,"
        "priority,colors_required,printer_id,scheduled_start,scheduled_end,target_type,target_filter,created_at) "
        "VALUES(:id,:name,:status,1,1,1,:hours,:hold,:priority,:colors,:printer,:start,:end,:tt,:tf,:created)"),
        {'id': job_id, 'name': name or f'Synthetic job {job_id}', 'status': status, 'hours': hours, 'hold': hold,
         'priority': priority, 'colors': colors_required, 'printer': printer,
         'start': start.replace(tzinfo=None) if start else None, 'end': end.replace(tzinfo=None) if end else None,
         'tt': target_type, 'tf': target_filter, 'created': f'2026-10-01 00:00:{job_id % 60:02d}'})
    db.commit()


def run(db, horizon_days=2):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    return Scheduler(SchedulerConfig(horizon_days=horizon_days)).run(db, start_date=START)


def rows(db, ids):
    db.expire_all()
    found = db.execute(text(
        f"SELECT id,status,printer_id,scheduled_start,match_score FROM jobs WHERE id IN ({','.join(map(str, ids))}) ORDER BY id")).all()
    return {r[0]: r[1:] for r in found}


def as_utc(value):
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


# ---------------- F04-4.2-01 color-match scoring ----------------

def test_competing_printers_job_goes_to_printer_with_matching_loaded_color(fleet):
    only_active(fleet, [9, 10])
    colors(fleet, {9: 'white', 10: 'red'})
    add_job(fleet, 901, colors_required='red')
    result = run(fleet)
    assert result.scheduled_count == 1 and result.setup_blocks == 0
    status, printer, start, score = rows(fleet, [901])[901]
    assert (status, printer, score) == ('scheduled', 10, 100)
    assert as_utc(start) == START  # no setup slot: starts immediately


def test_mixed_color_queue_is_batched_without_any_filament_swap(fleet):
    only_active(fleet, [9, 10])
    colors(fleet, {9: 'red', 10: 'blue'})
    for job_id, color in ((901, 'red'), (902, 'blue'), (903, 'red'), (904, 'blue')):
        add_job(fleet, job_id, colors_required=color)
    result = run(fleet)
    assigned = rows(fleet, [901, 902, 903, 904])
    assert result.scheduled_count == 4
    assert result.setup_blocks == 0, 'every job should land on a printer already loaded with its color'
    assert {j: v[1] for j, v in assigned.items()} == {901: 9, 902: 10, 903: 9, 904: 10}
    assert {v[3] for v in assigned.values()} == {100}


def test_new_color_swap_is_paid_once_and_followup_job_reuses_swapped_printer(fleet):
    only_active(fleet, [9, 10])
    colors(fleet, {9: 'red', 10: 'red'})
    add_job(fleet, 901, colors_required='green')
    add_job(fleet, 902, colors_required='green')
    result = run(fleet)
    assigned = rows(fleet, [901, 902])
    assert result.scheduled_count == 2
    assert result.setup_blocks == 1, 'second green job must reuse the printer already swapped to green'
    assert assigned[901][1] == assigned[902][1]
    # The first green job pays one 15-minute setup slot; the second starts right after it.
    assert as_utc(assigned[901][2]) == START + timedelta(minutes=15)
    assert as_utc(assigned[902][2]) == START + timedelta(minutes=75)
    assert (assigned[901][3], assigned[902][3]) == (0, 100)


def test_color_match_is_preference_so_unmatched_job_is_still_scheduled(fleet):
    only_active(fleet, [9])
    colors(fleet, {9: 'white'})
    add_job(fleet, 901, colors_required='purple')
    result = run(fleet)
    status, printer, start, score = rows(fleet, [901])[901]
    assert (result.scheduled_count, result.setup_blocks) == (1, 1)
    assert (status, printer, score) == ('scheduled', 9, 0)


# ---------------- F04-4.2-02 target type filtering ----------------

@pytest.mark.parametrize('target_filter', ['X1C', 'x1c', 'Bambu Lab X1 Carbon', 'BL-P001'])
def test_model_target_only_schedules_on_matching_machine_type(fleet, target_filter):
    add_job(fleet, 901, target_type='model', target_filter=target_filter)
    result = run(fleet)
    assert result.scheduled_count == 1
    assert rows(fleet, [901])[901][:2] == ('scheduled', 11)


def test_protocol_target_only_schedules_on_matching_api_type(fleet):
    add_job(fleet, 901, target_type='protocol', target_filter='moonraker')
    add_job(fleet, 902, target_type='protocol', target_filter='bambu')
    result = run(fleet)
    assigned = rows(fleet, [901, 902])
    assert result.scheduled_count == 2
    assert assigned[901][:2] == ('scheduled', 12)
    assert assigned[902][1] in {9, 10, 11}


@pytest.mark.parametrize('target_type,target_filter', [('model', 'MK4'), ('protocol', 'prusalink')])
def test_target_without_any_matching_printer_stays_pending(fleet, target_type, target_filter):
    add_job(fleet, 901, target_type=target_type, target_filter=target_filter)
    result = run(fleet)
    assert result.scheduled_count == 0 and result.skipped_count == 1
    assert rows(fleet, [901])[901][:2] == ('pending', None)


def test_model_target_competes_for_matching_printer_capacity(fleet):
    """Two 2h X1C-only jobs serialize on printer 11 even though other printers are idle."""
    add_job(fleet, 901, target_type='model', target_filter='X1C', hours=2)
    add_job(fleet, 902, target_type='model', target_filter='X1C', hours=2)
    run(fleet)
    assigned = rows(fleet, [901, 902])
    assert {v[1] for v in assigned.values()} == {11}
    assert sorted(as_utc(v[2]) for v in assigned.values()) == [START, START + timedelta(hours=2)]


def specific_client(review_db, signed_license, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.jobs.routes import jobs_crud
    from modules.notifications import alert_dispatcher
    signed_license(tier='pro', features=[])
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda **_: True)
    app = FastAPI(); app.include_router(jobs_crud.router, prefix='/api/v1')

    def request_db():
        with Session(review_db.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': 1, 'is_active': True}
    return TestClient(app)


from tests.test_contracts.test_license_user_limits import signed_license  # noqa: E402


def test_specific_target_created_through_api_schedules_only_on_chosen_printer(fleet, signed_license, monkeypatch):
    client = specific_client(fleet, signed_license, monkeypatch)
    response = client.post('/api/v1/jobs', json={'item_name': 'Synthetic specific target', 'quantity': 1,
                                                  'duration_hours': 1, 'target_type': 'specific', 'printer_id': 12})
    assert response.status_code == 201, response.text
    job_id = response.json()['id']
    run(fleet)
    assert rows(fleet, [job_id])[job_id][:2] == ('scheduled', 12)


# ---------------- F04-4.2-03 availability windows ----------------

def test_job_needs_whole_duration_window_not_just_a_free_start_slot(fleet):
    only_active(fleet, [9])
    add_job(fleet, 801, status='scheduled', printer=9, start=START, hours=1)                      # 12:00-13:00
    add_job(fleet, 802, status='scheduled', printer=9, start=START + timedelta(hours=3), hours=1)  # 15:00-16:00
    add_job(fleet, 901, hours=3)   # 3h cannot fit the 13:00-15:00 gap
    add_job(fleet, 902, hours=2)   # 2h exactly fits the gap
    result = run(fleet)
    assigned = rows(fleet, [801, 802, 901, 902])
    assert result.scheduled_count == 2
    assert as_utc(assigned[901][2]) == START + timedelta(hours=4)
    assert as_utc(assigned[902][2]) == START + timedelta(hours=1)
    assert assigned[801][2] is not None and assigned[802][2] is not None


def test_window_choice_across_competing_printers_picks_earliest_free_window(fleet):
    only_active(fleet, [9, 10])
    add_job(fleet, 801, status='scheduled', printer=9, start=START, hours=4)
    add_job(fleet, 802, status='scheduled', printer=10, start=START, hours=1)
    add_job(fleet, 901, hours=2)
    run(fleet)
    _, printer, start, _ = rows(fleet, [901])[901]
    assert (printer, as_utc(start)) == (10, START + timedelta(hours=1))


def test_job_start_never_falls_inside_blackout_window(fleet):
    only_active(fleet, [9])
    # Printer busy until 22:00; next 1h window would start 22:00..22:30 is free but 22:30+ is blackout.
    add_job(fleet, 801, status='scheduled', printer=9, start=START, hours=10.5)  # 12:00-22:30
    add_job(fleet, 901, hours=1)
    run(fleet)
    start = as_utc(rows(fleet, [901])[901][2])
    assert start == datetime(2026, 10, 13, 5, 30, tzinfo=timezone.utc)


# ---------------- F04-4.2-04 proactive fleet-wide stale cleanup ----------------

def test_every_scheduler_run_resets_stale_schedules_across_the_fleet(fleet):
    only_active(fleet, [9, 10])
    add_job(fleet, 701, status='scheduled', printer=9, start=START - timedelta(hours=3), hold=True)
    add_job(fleet, 702, status='scheduled', printer=10, start=START - timedelta(hours=2, minutes=1), hold=True)
    add_job(fleet, 703, status='scheduled', printer=11, start=START - timedelta(hours=5), hold=True)  # inactive printer
    add_job(fleet, 704, status='scheduled', printer=9, start=START - timedelta(hours=1, minutes=59))  # inside 2h window
    add_job(fleet, 705, status='scheduled', printer=10, start=START - timedelta(hours=1, minutes=30))
    add_job(fleet, 706, status='scheduled', printer=9, start=START - timedelta(hours=4))              # stale, not held
    run(fleet)
    after = rows(fleet, [701, 702, 703, 704, 705, 706])
    for stale in (701, 702, 703):
        assert after[stale] == ('pending', None, None, None)
    assert after[704][:2] == ('scheduled', 9) and as_utc(after[704][2]) == START - timedelta(hours=1, minutes=59)
    assert after[705][:2] == ('scheduled', 10) and as_utc(after[705][2]) == START - timedelta(hours=1, minutes=30)
    # The non-held stale job is reset and immediately re-placed in a future window by the same run.
    assert after[706][0] == 'scheduled' and as_utc(after[706][2]) >= START
    # A second run sweeps a schedule that became stale after the first run.
    add_job(fleet, 707, status='scheduled', printer=10, start=START - timedelta(hours=6), hold=True)
    run(fleet)
    assert rows(fleet, [707])[707] == ('pending', None, None, None)


def test_stale_cleanup_runs_even_when_no_printer_is_available(fleet):
    only_active(fleet, [])
    add_job(fleet, 701, status='scheduled', printer=9, start=START - timedelta(hours=3), hold=True)
    result = run(fleet)
    assert result.success is False
    assert rows(fleet, [701])[701] == ('pending', None, None, None)


def test_specific_printer_set_by_patch_is_honoured_by_scheduler(fleet, signed_license, monkeypatch):
    client = specific_client(fleet, signed_license, monkeypatch)
    add_job(fleet, 901, target_type='specific')
    response = client.patch('/api/v1/jobs/901', json={'printer_id': 12, 'pinned_printer_id': 12})  # explicit pin
    assert response.status_code == 200, response.text
    assert rows(fleet, [901])[901][:2] == ('pending', 12)
    run(fleet)
    assert rows(fleet, [901])[901][:2] == ('scheduled', 12)


# ---------------- F04-4.2-02 pin lives in the job's target (specific + printer id), not printer_id ----------------

def _pin_client(fleet, signed_license, monkeypatch):
    from modules.jobs.routes import jobs_lifecycle
    client = specific_client(fleet, signed_license, monkeypatch)
    client.app.include_router(jobs_lifecycle.router, prefix='/api/v1')
    return client


def _pin(db, job_id):
    from modules.jobs.pinning import pinned_printer_id
    db.expire_all()
    return pinned_printer_id(*db.execute(text('SELECT target_type,target_filter FROM jobs WHERE id=:id'), {'id': job_id}).one())


def test_pin_survives_reset_bulk_reschedule_and_stale_cleanup(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    job = client.post('/api/v1/jobs', json={'item_name': 'Synthetic pinned', 'duration_hours': 1, 'printer_id': 12}).json()['id']
    assert _pin(fleet, job) == 12
    run(fleet)
    assert rows(fleet, [job])[job][:2] == ('scheduled', 12)
    # 1. explicit reset to pending (reset is only allowed from a finished outcome)
    assert client.post(f'/api/v1/jobs/{job}/start').status_code == 200
    assert client.post(f'/api/v1/jobs/{job}/fail').status_code == 200
    assert client.post(f'/api/v1/jobs/{job}/reset').status_code == 200
    assert rows(fleet, [job])[job][:2] == ('pending', None) and _pin(fleet, job) == 12
    run(fleet)
    assert rows(fleet, [job])[job][:2] == ('scheduled', 12)
    # 2. bulk reschedule
    assert client.post('/api/v1/jobs/bulk-update', json={'job_ids': [job], 'action': 'reschedule'}).status_code == 200
    assert rows(fleet, [job])[job][:2] == ('pending', None) and _pin(fleet, job) == 12
    run(fleet)
    assert rows(fleet, [job])[job][:2] == ('scheduled', 12)
    # 3. scheduler stale cleanup (scheduled start long past)
    fleet.execute(text("UPDATE jobs SET scheduled_start='2026-10-01 00:00:00', scheduled_end='2026-10-01 01:00:00' WHERE id=:id"), {'id': job})
    fleet.commit()
    run(fleet)
    assert rows(fleet, [job])[job][:2] == ('scheduled', 12) and _pin(fleet, job) == 12


@pytest.mark.parametrize('state,issue', [('inactive', 'pinned_printer_inactive'), ('missing', 'pinned_printer_missing'),
                                         ('inaccessible', 'pinned_printer_inaccessible')])
def test_pinned_printer_unavailable_keeps_job_pending_with_visible_reason(fleet, signed_license, monkeypatch, state, issue):
    client = _pin_client(fleet, signed_license, monkeypatch)
    job = client.post('/api/v1/jobs', json={'item_name': 'Synthetic pinned blocked', 'duration_hours': 1, 'printer_id': 12}).json()['id']
    if state == 'inactive':
        fleet.execute(text('UPDATE printers SET is_active=0 WHERE id=12'))
    elif state == 'inaccessible':  # the printer moved to another org and is not shared
        fleet.execute(text('UPDATE printers SET org_id=2, shared=0 WHERE id=12'))
    else:  # legacy row removed behind the API's back
        fleet.execute(text('DELETE FROM filament_slots WHERE printer_id=12'))
        fleet.execute(text('DELETE FROM printers WHERE id=12'))
    fleet.commit()
    result = run(fleet)
    assert rows(fleet, [job])[job][:2] == ('pending', None) and _pin(fleet, job) == 12
    assert any(f'({issue})' in e for e in result.errors)
    body = client.get(f'/api/v1/jobs/{job}').json()
    assert (body['pinned_printer_id'], body['pinned_printer_issue']) == (12, issue)
    fleet.expire_all()
    alert = fleet.execute(text("SELECT metadata_json FROM alerts WHERE job_id=:id AND alert_type='queue_skipped'"), {'id': job}).scalar()
    assert alert is not None and json.loads(alert)['reason'] == issue if isinstance(alert, str) else alert['reason'] == issue


def test_only_an_explicit_choice_pins(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    # No printer chosen: default target_type 'specific' does not pin.
    free = client.post('/api/v1/jobs', json={'item_name': 'Synthetic free', 'duration_hours': 1}).json()['id']
    assert _pin(fleet, free) is None
    run(fleet)
    status, printer, *_ = rows(fleet, [free])[free]
    assert status == 'scheduled'
    # Sending a printer_id (assignment) without pinned_printer_id is not a pin.
    assert client.patch(f'/api/v1/jobs/{free}', json={'printer_id': printer, 'notes': 'edit'}).status_code == 200
    assert _pin(fleet, free) is None
    # Repeat keeps the assignment hint but never invents a pin.
    from modules.jobs.routes import jobs_crud  # noqa: F401
    repeated = client.post(f'/api/v1/jobs/{free}/repeat').json()['new_job_id']
    assert _pin(fleet, repeated) is None
    # Clearing an explicit pin.
    assert client.patch(f'/api/v1/jobs/{free}', json={'pinned_printer_id': 12}).status_code == 200
    assert _pin(fleet, free) == 12
    assert client.patch(f'/api/v1/jobs/{free}', json={'pinned_printer_id': None}).status_code == 200
    assert _pin(fleet, free) is None
    # Foreign/unknown printers cannot be pinned.
    assert client.patch(f'/api/v1/jobs/{free}', json={'pinned_printer_id': 9999}).status_code == 404



def test_model_target_with_unknown_model_fails_closed(fleet):
    add_job(fleet, 903, target_type='model', target_filter='BL-A001')  # not a Bambu Studio id
    run(fleet)
    assert rows(fleet, [903])[903][:2] == ('pending', None)


# ---------------- explicit printer choices on other create paths ----------------

def test_batch_send_pins_each_copy_to_its_printer(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    made = client.post('/api/v1/jobs/batch', json={'item_name': 'Synthetic batch', 'printer_ids': [9, 11], 'duration_hours': 1})
    assert made.status_code == 200, made.text
    assert sorted(_pin(fleet, j['id']) for j in made.json()) == [9, 11]


def test_deleting_a_printer_marks_its_pins_deleted_and_never_retargets(fleet, signed_license, monkeypatch):
    from modules.printers import routes_crud
    client = _pin_client(fleet, signed_license, monkeypatch)
    client.app.include_router(routes_crud.router, prefix='/api/v1')
    job = client.post('/api/v1/jobs', json={'item_name': 'Synthetic pinned to doomed', 'duration_hours': 1, 'printer_id': 12}).json()['id']
    fleet.execute(text('DELETE FROM filament_slots WHERE printer_id=12')); fleet.commit()
    response = client.delete('/api/v1/printers/12')
    assert response.status_code in (200, 204), response.text
    body = client.get(f'/api/v1/jobs/{job}').json()
    assert (body['pinned_printer_id'], body['pinned_printer_issue']) == (None, 'pinned_printer_deleted')
    # SQLite may hand the id out again: the job must not follow it.
    fleet.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id) VALUES(12,'Reused id','Voron','Voron',1,'moonraker',1)"))
    fleet.commit()
    run(fleet)
    assert rows(fleet, [job])[job][:2] == ('pending', None)
    fleet.expire_all()
    alerts = fleet.execute(text("SELECT metadata_json FROM alerts WHERE title LIKE 'Pinned printer deleted%'")).scalars().all()
    assert any('pinned_printer_deleted' in str(a) and str(job) in str(a) for a in alerts)


@pytest.mark.parametrize('bad', ['١٢', '+12', '12a', ' ', '0', '-3'])
def test_specific_target_filter_must_be_ascii_printer_id(fleet, signed_license, monkeypatch, bad):
    client = _pin_client(fleet, signed_license, monkeypatch)
    response = client.post('/api/v1/jobs', json={'item_name': 'Synthetic bad target', 'target_type': 'specific', 'target_filter': bad})
    if bad.strip():
        assert response.status_code == 422, response.text
    else:
        assert response.status_code == 201 and _pin(fleet, response.json()['id']) is None


def test_legacy_unparseable_target_is_no_pin_with_a_flag_and_never_breaks_list_or_scheduler(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    add_job(fleet, 904, target_type='specific', target_filter='printer-twelve')
    listed = client.get('/api/v1/jobs')
    assert listed.status_code == 200
    row = next(j for j in listed.json() if j['id'] == 904)
    assert (row['pinned_printer_id'], row['pinned_printer_issue']) == (None, 'pinned_target_invalid')
    result = run(fleet)
    assert result.success and rows(fleet, [904])[904][0] == 'scheduled'


def test_move_job_moves_its_pin_too(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    job = client.post('/api/v1/jobs', json={'item_name': 'Synthetic pinned move', 'duration_hours': 1, 'printer_id': 12}).json()['id']
    moved = client.patch(f'/api/v1/jobs/{job}/move', json={'printer_id': 11, 'scheduled_start': '2026-10-20T12:00:00'})
    assert moved.status_code == 200, moved.text
    assert rows(fleet, [job])[job][:2] == ('scheduled', 11) and _pin(fleet, job) == 11


def test_moving_a_job_with_a_deleted_pin_repins_it_to_the_chosen_printer(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    add_job(fleet, 915, target_type='specific', target_filter='deleted:44')
    moved = client.patch('/api/v1/jobs/915/move', json={'printer_id': 11, 'scheduled_start': '2026-10-20T12:00:00'})
    assert moved.status_code == 200, moved.text
    assert _pin(fleet, 915) == 11


def test_job_list_reads_printers_once_for_pin_issues(fleet, signed_license, monkeypatch):
    from sqlalchemy import event
    client = _pin_client(fleet, signed_license, monkeypatch)
    for n in range(5):
        client.post('/api/v1/jobs', json={'item_name': f'Synthetic pinned {n}', 'duration_hours': 1, 'printer_id': 9 + (n % 3)})
    seen = []
    def count(conn, cursor, statement, *args):
        if 'FROM printers' in statement and 'is_active' in statement:
            seen.append(statement)
    engine = fleet.get_bind()
    event.listen(engine, 'before_cursor_execute', count)
    try:
        assert client.get('/api/v1/jobs').status_code == 200
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert len(seen) == 1, seen


# ---------------- round 4: edits never wipe model/protocol targets ----------------

@pytest.mark.parametrize('target_type,target_filter', [('model', 'X1C'), ('protocol', 'moonraker')])
@pytest.mark.parametrize('body', [{'notes': 'notes only'}, {'notes': 'modal save', 'pinned_printer_id': None}])
def test_edit_keeps_model_and_protocol_targets(fleet, signed_license, monkeypatch, target_type, target_filter, body):
    client = _pin_client(fleet, signed_license, monkeypatch)
    add_job(fleet, 905, target_type=target_type, target_filter=target_filter)
    assert client.patch('/api/v1/jobs/905', json=body).status_code == 200
    fleet.expire_all()
    assert tuple(fleet.execute(text('SELECT target_type,target_filter FROM jobs WHERE id=905')).one()) == (target_type, target_filter)


def test_printer_id_alone_is_not_a_pin_and_explicit_pin_replaces_a_model_target(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    add_job(fleet, 906, target_type='model', target_filter='X1C')
    assert client.patch('/api/v1/jobs/906', json={'printer_id': 11}).status_code == 200
    fleet.expire_all()
    assert tuple(fleet.execute(text('SELECT target_type,target_filter FROM jobs WHERE id=906')).one()) == ('model', 'X1C')
    assert client.patch('/api/v1/jobs/906', json={'pinned_printer_id': 12}).status_code == 200
    assert _pin(fleet, 906) == 12


@pytest.mark.parametrize('body,expected,pin_after', [
    ({'printer_id': 11}, 409, 12),                               # conflicts with the pin
    ({'printer_id': 12}, 200, 12),                               # agrees with the pin
    ({'printer_id': 11, 'pinned_printer_id': 11}, 200, 11),      # explicit re-pin, consistent
    ({'printer_id': 11, 'pinned_printer_id': 10}, 409, 12),      # inconsistent pair
    ({'printer_id': 11, 'pinned_printer_id': None}, 200, None),  # pin cleared, assignment free
])
def test_patch_printer_id_must_agree_with_the_pin(fleet, signed_license, monkeypatch, body, expected, pin_after):
    client = _pin_client(fleet, signed_license, monkeypatch)
    add_job(fleet, 916, target_type='specific', target_filter='12')
    response = client.patch('/api/v1/jobs/916', json=body)
    assert response.status_code == expected, response.text
    assert _pin(fleet, 916) == pin_after


def test_patch_printer_id_on_a_deleted_pin_asks_for_an_explicit_choice(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    add_job(fleet, 917, target_type='specific', target_filter='deleted:44')
    assert client.patch('/api/v1/jobs/917', json={'printer_id': 11}).status_code == 409
    assert client.patch('/api/v1/jobs/917', json={'printer_id': 11, 'pinned_printer_id': 11}).status_code == 200
    assert _pin(fleet, 917) == 11


def _delete_printer_12(client, fleet):
    from modules.printers import routes_crud
    client.app.include_router(routes_crud.router, prefix='/api/v1')
    fleet.execute(text('DELETE FROM filament_slots WHERE printer_id=12')); fleet.commit()
    assert client.delete('/api/v1/printers/12').status_code in (200, 204)
    fleet.expire_all()


def test_printer_delete_marks_every_pinned_job_and_alerts_only_live_owners_once(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    for jid, status in ((907, 'pending'), (908, 'scheduled'), (909, 'submitted'), (910, 'completed'), (911, 'failed')):
        add_job(fleet, jid, status=status, target_type='specific', target_filter='12', printer=12 if status in ('completed', 'failed') else None)
    add_job(fleet, 912, status='completed', target_type='specific', target_filter='9', printer=9)   # another printer's pin
    _delete_printer_12(client, fleet)
    found = {r[0]: (r[1], r[2]) for r in fleet.execute(text('SELECT id,target_filter,printer_id FROM jobs WHERE id BETWEEN 907 AND 912')).all()}
    # The target is marked on every status (it is the target, not print history: print_jobs keeps that).
    assert {j: f for j, (f, _) in found.items()} == {907: 'deleted:12', 908: 'deleted:12', 909: 'deleted:12',
                                                     910: 'deleted:12', 911: 'deleted:12', 912: '9'}
    alerts = fleet.execute(text("SELECT user_id,message FROM alerts WHERE title LIKE 'Pinned printer deleted%'")).all()
    assert len(alerts) == 1 and '3 queued jobs' in alerts[0][1]  # one per owner, live jobs only


def test_deleted_pin_never_revives_on_printer_id_reuse(fleet, signed_license, monkeypatch):
    """Reset, resubmit and the scheduler keep deleted:<id> after a new printer reuses the id."""
    client = _pin_client(fleet, signed_license, monkeypatch)
    add_job(fleet, 913, status='completed', target_type='specific', target_filter='12', printer=12)
    add_job(fleet, 914, status='rejected', target_type='specific', target_filter='12')
    fleet.execute(text('UPDATE jobs SET submitted_by=3 WHERE id=914')); fleet.commit()
    _delete_printer_12(client, fleet)
    fleet.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id) "
                       "VALUES (12,'Reused-12','Voron 2.4','Voron',1,'moonraker',1)"))
    fleet.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES (12,1,'PLA')")); fleet.commit()
    assert client.post('/api/v1/jobs/913/reset').status_code == 200
    signed_license(tier='pro', features=['job_approval'])
    assert client.post('/api/v1/jobs/914/resubmit').status_code == 200
    fleet.expire_all()
    assert dict(fleet.execute(text('SELECT id,target_filter FROM jobs WHERE id IN (913,914)')).all()) == {913: 'deleted:12', 914: 'deleted:12'}
    issue = client.get('/api/v1/jobs/913').json()
    assert issue['pinned_printer_issue'] == 'pinned_printer_deleted' and issue['pinned_printer_id'] is None
    run(fleet)
    assert rows(fleet, [913])[913][:2] == ('pending', None)  # blocked visibly, never placed on the reused id


def test_status_transitions_may_never_write_target_columns(review_db):
    from modules.jobs.state_machine import TransitionError, transition_job
    for column in ('target_type', 'target_filter'):
        with pytest.raises(TransitionError):
            transition_job(review_db, 1, 'pending', 'operator', extra_fields={column: None})


def test_clear_pins_chunks_large_job_sets(fleet):
    from modules.jobs import pinning
    for jid in range(1000, 1012):
        add_job(fleet, jid, target_type='specific', target_filter='12')
    for jid in range(1012, 1015):
        add_job(fleet, jid, status='completed', target_type='specific', target_filter='12')
    pins = pinning.clear_pins_for_deleted_printer(fleet, 12, chunk=5)
    fleet.commit()
    assert pins.marked == list(range(1000, 1015)) and pins.live == list(range(1000, 1012))
    assert fleet.execute(text("SELECT COUNT(*) FROM jobs WHERE target_filter='deleted:12'")).scalar_one() == 15
