"""plate-cleared records the confirmation even when no job is waiting (spec plate-clear-confirmation.md).

Regression: on a farm that clears plates by hand, POST /printers/{id}/plate-cleared answered
"No jobs waiting for plate clear", wrote nothing, and plate_clear_confirmed_at stayed empty.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401

WRITE = ['write:printers', 'read:printers', 'read:jobs']


@pytest.fixture
def floor(offline_store, token_client):  # noqa: F811
    from modules.printers import routes_controls
    from modules.printers.routes_crud import router as printers
    client, issue = token_client
    client.app.include_router(routes_controls.router)
    client.app.include_router(printers)
    offline_store.execute(text("UPDATE printers SET slot_count=COALESCE(slot_count,4),tags=COALESCE(tags,'[]'),"
                               "timelapse_enabled=COALESCE(timelapse_enabled,0),shared=COALESCE(shared,0),gcode_state='FINISH' WHERE id=199"))
    offline_store.commit()
    return client, issue, offline_store


def finished(db, job_id, *, org=1, start='2026-11-12 10:00:00', end='2026-11-12 11:00:00', status='completed'):
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,actual_start,actual_end) "
                    "VALUES(:id,'Finished print',199,:org,:status,:start,:end)"),
               {'id': job_id, 'org': org, 'status': status, 'start': start, 'end': end})
    db.commit()


def archive_submission(db, job_id):
    """An archived Education submission for `job_id`; other required columns get placeholder values."""
    values = {'job_id': job_id, 'archived_at': '2026-11-12 12:00:00'}
    for _, name, kind, notnull, default, primary in db.execute(text("PRAGMA table_info(education_submissions)")):
        if name in values or primary or not notnull or default is not None:
            continue
        values[name] = 'x' if 'CHAR' in kind.upper() or 'TEXT' in kind.upper() else 1
    db.execute(text("PRAGMA foreign_keys=OFF"))
    db.execute(text(f"INSERT INTO education_submissions({','.join(values)}) VALUES({','.join(':' + k for k in values)})"), values)
    db.commit()


def events(db):
    return db.execute(text("SELECT job_id, detail FROM job_timeline_events WHERE kind='bed_clear_confirmed' ORDER BY occurred_at")).all()


def confirmed_at(client, issue):
    return client.get('/printers/199', headers=issue(['read:printers', 'read:jobs'])).json()['plate_clear_confirmed_at']


def test_no_waiting_job_records_the_confirmation_on_the_last_finished_print(floor):
    client, issue, db = floor
    finished(db, 801, end='2026-11-12 09:00:00', start='2026-11-12 08:00:00')
    finished(db, 802)
    body = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert body['success'] is True and body['message'] == 'Plate clear recorded'
    assert [row.job_id for row in events(db)] == [802]
    shown = confirmed_at(client, issue)
    assert shown is not None and shown[:16] == body['plate_clear_confirmed_at'][:16]


def test_a_second_call_adds_no_second_row(floor):
    client, issue, db = floor
    finished(db, 802)
    first = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    second = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert len(events(db)) == 1 and second['message'] == 'Plate clear recorded'
    assert second['plate_clear_confirmed_at'][:19] == first['plate_clear_confirmed_at'][:19]


@pytest.mark.parametrize('setup', ['printing_state', 'paused_state', 'printing_job', 'no_finished_print', 'other_org'])
def test_nothing_is_recorded_when_there_is_nothing_to_confirm(floor, setup):
    client, issue, db = floor
    if setup != 'no_finished_print':
        finished(db, 802, org=2 if setup == 'other_org' else 1)
    if setup == 'printing_state':
        db.execute(text("UPDATE printers SET gcode_state='RUNNING' WHERE id=199"))
    if setup == 'paused_state':
        db.execute(text("UPDATE printers SET gcode_state='PAUSE' WHERE id=199"))
    if setup == 'printing_job':
        db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,actual_start) "
                        "VALUES(803,'Running',199,1,'printing','2026-11-12 12:00:00')"))
    db.commit()
    body = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert body == {'success': True, 'message': 'No jobs waiting for plate clear'}   # the old answer, unchanged
    assert events(db) == [] and confirmed_at(client, issue) is None


def test_the_confirmation_stands_only_until_the_next_print_starts(floor):
    client, issue, db = floor
    finished(db, 802)
    client.post('/printers/199/plate-cleared', headers=issue(WRITE))
    assert confirmed_at(client, issue) is not None
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,actual_start) "
                    "VALUES(803,'Next print',199,1,'printing','2099-01-01 00:00:00')"))
    db.commit()
    assert confirmed_at(client, issue) is None          # a newer print started after the confirmation
    db.execute(text("UPDATE jobs SET status='completed', actual_end='2099-01-01 01:00:00' WHERE id=803"))
    db.commit()
    assert confirmed_at(client, issue) is None          # finished, plate not confirmed yet
    assert client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()['message'] == 'Plate clear recorded'
    assert [row.job_id for row in events(db)] == [802, 803]


def test_releasing_a_waiting_job_also_records_the_confirmation(floor, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, issue, db = floor
    finished(db, 802)
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,priority) "
                    "VALUES(804,'Waiting',199,1,'waiting_plate_clear',3)"))
    db.commit()
    body = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert body['message'] == 'Job 804 released for dispatch'
    assert db.execute(text("SELECT status FROM jobs WHERE id=804")).scalar() == 'pending'
    assert [row.job_id for row in events(db)] == [802]


def test_read_only_tokens_still_cannot_confirm(floor):
    client, issue, db = floor
    finished(db, 802)
    assert client.post('/printers/199/plate-cleared', headers=issue(['read'])).status_code == 403
    assert events(db) == []


# ---------------------------------------------------------------- Codex sign-off 11-12
def test_times_are_compared_as_instants_not_as_text(floor):
    """'10:00+02:00' (08:00 UTC) sorts after a naive '09:30' as text. A confirmation at 09:00 UTC
    must not survive a print that started at 09:30 UTC, and the last finished print is the one
    that really ended last."""
    client, issue, db = floor
    finished(db, 811, start='2026-11-12 06:00:00', end='2026-11-12T10:00:00+02:00')   # ended 08:00 UTC
    finished(db, 812, start='2026-11-12 08:10:00', end='2026-11-12 08:45:00')         # ended later
    assert client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()['message'] == 'Plate clear recorded'
    assert [row.job_id for row in events(db)] == [812]


def test_a_confirmation_written_with_an_offset_does_not_outlive_a_later_naive_start(floor):
    client, issue, db = floor
    finished(db, 812, start='2026-11-12 08:10:00', end='2026-11-12 08:45:00')
    db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,occurred_at) "
                    "VALUES('offset',812,199,1,'bed_clear_confirmed','{}','2026-11-12T11:00:00+02:00')"))   # 09:00 UTC
    db.commit()
    assert confirmed_at(client, issue) is not None
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,actual_start) "
                    "VALUES(813,'Next',199,1,'printing','2026-11-12 09:30:00')"))                              # 09:30 UTC
    db.commit()
    assert confirmed_at(client, issue) is None


def test_a_controlled_job_outside_the_callers_audience_is_never_written_to(floor, monkeypatch):
    from core import controlled_parts
    client, issue, db = floor
    finished(db, 802)
    monkeypatch.setattr(controlled_parts, 'allowed', lambda db_, kind, identifier, actor: not (kind == 'job' and identifier == 802))
    body = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert body == {'success': True, 'message': 'No jobs waiting for plate clear'} and events(db) == []


def test_the_newest_confirmation_is_chosen_by_instant_before_a_later_start_is_judged(floor):
    """Second sign-off: '11:00+02:00' (09:00 UTC) sorted ahead of '12:00+00:00' as text, so a
    09:30 start wrongly cancelled the valid noon confirmation."""
    client, issue, db = floor
    finished(db, 812, start='2026-11-12 08:10:00', end='2026-11-12 08:45:00')
    finished(db, 813, start='2026-11-12 09:30:00', end='2026-11-12 11:30:00')
    db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,occurred_at) VALUES"
                    "('old',812,199,1,'bed_clear_confirmed','{}','2026-11-12T11:00:00+02:00'),"
                    "('new',813,199,1,'bed_clear_confirmed','{}','2026-11-12 12:00:00+00:00')"))
    db.commit()
    shown = confirmed_at(client, issue)
    assert shown is not None and '12:00:00' in shown


def test_an_old_job_that_finished_last_is_the_one_confirmed(floor):
    """Third sign-off: only the 50 highest job ids were considered."""
    client, issue, db = floor
    for job_id in range(900, 960):
        finished(db, job_id, start='2026-11-10 08:00:00', end='2026-11-10 09:00:00')
    finished(db, 805, start='2026-11-12 08:00:00', end='2026-11-12 12:00:00')     # lowest id, latest end
    assert client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()['message'] == 'Plate clear recorded'
    assert [row.job_id for row in events(db)] == [805]


def test_an_earlier_confirmation_is_not_reused_after_a_later_print_that_never_got_an_end_time(floor):
    # 802 finishes and is confirmed; 803 then starts and fails with no end time (printer went offline).
    # The printer read drops the old confirmation; confirming again must record a new one, not hand
    # the old one back as "recorded".
    client, issue, db = floor
    finished(db, 802, start='2020-01-01 10:00:00', end='2020-01-01 11:00:00')
    first = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert first['message'] == 'Plate clear recorded' and confirmed_at(client, issue) is not None
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,actual_start) "
                    "VALUES(803,'Lost print',199,1,'failed',:start)"),
               {'start': (datetime.now(timezone.utc) + timedelta(seconds=1)).replace(tzinfo=None)})
    db.commit()
    time.sleep(1.2)
    assert confirmed_at(client, issue) is None
    again = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert again['message'] == 'Plate clear recorded' and len(events(db)) == 2
    assert confirmed_at(client, issue) is not None


def test_a_job_of_an_archived_education_submission_is_never_the_one_confirmed(floor):
    # The printer read hides confirmations on such jobs, so one written there would be reported as
    # recorded and never shown.
    client, issue, db = floor
    finished(db, 801, start='2026-11-12 08:00:00', end='2026-11-12 09:00:00')
    finished(db, 802)
    columns = {row[1] for row in db.execute(text("PRAGMA table_info(education_submissions)"))}
    assert {'job_id', 'archived_at'} <= columns
    archive_submission(db, 802)
    body = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert body['message'] == 'Plate clear recorded'
    assert [row.job_id for row in events(db)] == [801]
    assert confirmed_at(client, issue) is not None


def test_nothing_is_recorded_when_the_only_finished_print_is_an_archived_education_submission(floor):
    client, issue, db = floor
    finished(db, 802)
    archive_submission(db, 802)
    body = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert body['message'] != 'Plate clear recorded' and events(db) == []
    assert confirmed_at(client, issue) is None
