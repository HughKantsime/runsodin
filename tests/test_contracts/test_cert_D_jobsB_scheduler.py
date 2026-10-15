"""cert-D jobsB: real Scheduler.run against a bootstrapped SQLite schema.

Claims: F04-4.2-01 (color-match scoring minimizes swaps), F04-4.2-02 (target type
filtering), F04-4.2-03 (availability-window search), F04-4.2-04 (proactive stale
cleanup on every scheduler run). Competing printers, fixed clock, no device I/O.
"""
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

@pytest.mark.parametrize('target_filter', ['X1C', 'x1c'])
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
    response = client.patch('/api/v1/jobs/901', json={'printer_id': 12})
    assert response.status_code == 200, response.text
    assert rows(fleet, [901])[901][:2] == ('pending', 12)
    run(fleet)
    assert rows(fleet, [901])[901][:2] == ('scheduled', 12)
