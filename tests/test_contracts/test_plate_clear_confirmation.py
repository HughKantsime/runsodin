"""plate-cleared records the confirmation even when no job is waiting (spec plate-clear-confirmation.md).

Regression: on a farm that clears plates by hand, POST /printers/{id}/plate-cleared answered
"No jobs waiting for plate clear", wrote nothing, and plate_clear_confirmed_at stayed empty.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401

WRITE = ['write:printers', 'read:printers', 'read:jobs']


@pytest.fixture
def floor(review_db, token_client):  # noqa: F811
    from modules.printers import routes_controls
    from modules.printers.routes_crud import router as printers
    client, issue = token_client
    client.app.include_router(routes_controls.router)
    client.app.include_router(printers)
    # This is an idle printer. The offline-monitor fixture also seeds a running print,
    # which must now block confirmation even when the printer reports FINISH.
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,gcode_state,slot_count,"
                           "is_active,tags,timelapse_enabled,shared) "
                           "VALUES(199,'Synthetic printer','bambu',1,'FINISH',4,1,'[]',0,0)"))
    review_db.commit()
    return client, issue, review_db


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


# ---------------------------------------------------------------- Last finished print, including unlinked monitor prints

def print_record(db, identifier=901, *, job=None, status='completed',
                 start='2026-11-12 12:00:00', end='2026-11-12 13:00:00'):
    db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at,ended_at,scheduled_job_id) "
                    "VALUES(:id,199,'part.gcode','Part',:status,:start,:end,:job)"),
               {'id': identifier, 'status': status, 'start': start, 'end': end, 'job': job})
    db.commit()


def stored_clear(db):
    value = db.execute(text("SELECT value FROM system_config WHERE key='plate_clear:199'")).scalar()
    return json.loads(value) if value is not None else None


def audits(db):
    return db.execute(text("SELECT entity_type,entity_id,details FROM audit_logs "
                           "WHERE action='printer.plate_clear_confirmed' ORDER BY id")).all()


def set_clock(monkeypatch, at):
    from modules.printers import plate_clear
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)
    monkeypatch.setattr(plate_clear, 'datetime', Clock)


@pytest.mark.parametrize('outcome', ['completed', 'failed', 'cancelled'])
def test_unlinked_print_confirmation_is_current_visible_and_idempotent(floor, outcome):
    client, issue, db = floor
    db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=199"))
    print_record(db, status=outcome)
    before = datetime.now(timezone.utc)
    first = client.post('/printers/199/plate-cleared', headers=issue(['write:printers'])).json()
    at = datetime.fromisoformat(first['plate_clear_confirmed_at'])
    assert before <= at <= datetime.now(timezone.utc)
    assert datetime.fromisoformat(confirmed_at(client, issue)) == at
    second = client.post('/printers/199/plate-cleared', headers=issue(['write:printers'])).json()
    assert second == first and events(db) == []
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='plate_clear:199'")).scalar_one() == 1
    assert stored_clear(db) == {'at': first['plate_clear_confirmed_at'], 'print_job_id': 901, 'org_id': 1,
                                'printer_created_at': str(db.execute(text('SELECT created_at FROM printers WHERE id=199')).scalar_one())}
    assert len(audits(db)) == 1
    assert tuple(audits(db)[0][:2]) == ('printer', 199)
    details = json.loads(audits(db)[0].details)
    assert details['printer_id'] == 199 and details['print_job_id'] == 901 and details['job_id'] is None
    assert details['actor_token_id'] is not None


@pytest.mark.parametrize('later_end', ['2026-11-12 13:00:00', None])
def test_unlinked_start_after_job_confirmation_requires_a_new_time(floor, monkeypatch, later_end):
    client, issue, db = floor
    finished(db, 802)
    set_clock(monkeypatch, datetime(2026, 11, 12, 11, 30, tzinfo=timezone.utc))
    first = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    print_record(db, status='failed', end=later_end)
    assert confirmed_at(client, issue) is None
    set_clock(monkeypatch, datetime(2026, 11, 12, 14, tzinfo=timezone.utc))
    second = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert second['plate_clear_confirmed_at'] == '2026-11-12T14:00:00+00:00'
    assert second['plate_clear_confirmed_at'] != first['plate_clear_confirmed_at']
    assert datetime.fromisoformat(confirmed_at(client, issue)) == datetime.fromisoformat(second['plate_clear_confirmed_at'])
    assert len(events(db)) == (2 if later_end is None else 1)
    assert len(audits(db)) == 2


@pytest.mark.parametrize('source', ['job', 'print'])
def test_later_start_voids_printer_owned_confirmation(floor, monkeypatch, source):
    client, issue, db = floor
    print_record(db)
    set_clock(monkeypatch, datetime(2026, 11, 12, 14, tzinfo=timezone.utc))
    client.post('/printers/199/plate-cleared', headers=issue(WRITE))
    if source == 'print':
        print_record(db, 902, start='2026-11-12 15:00:00', end=None, status='failed')
    else:
        finished(db, 802, start='2026-11-12 15:00:00', end=None, status='failed')
    assert confirmed_at(client, issue) is None
    set_clock(monkeypatch, datetime(2026, 11, 12, 16, tzinfo=timezone.utc))
    response = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert response['plate_clear_confirmed_at'] == '2026-11-12T16:00:00+00:00'
    assert stored_clear(db)['at'] == response['plate_clear_confirmed_at']
    assert len(audits(db)) == 2


def test_running_unlinked_print_blocks_confirmation_even_with_idle_reported_state(floor):
    client, issue, db = floor
    finished(db, 802)
    print_record(db, status='running', end=None)
    body = client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()
    assert body == {'success': True, 'message': 'No jobs waiting for plate clear'}
    assert events(db) == [] and stored_clear(db) is None and audits(db) == []


def test_operator_finished_job_with_unknown_print_end_still_confirms(floor):
    client, issue, db = floor
    finished(db, 802)
    print_record(db, job=802, status='failed', start='2026-11-12 10:00:00', end=None)
    assert client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()['message'] == 'Plate clear recorded'
    assert [row.job_id for row in events(db)] == [802] and stored_clear(db) is None
    assert json.loads(audits(db)[0].details)['job_id'] == 802


def test_monitor_end_decides_linked_candidate_instead_of_the_jobs_end(floor):
    client, issue, db = floor
    finished(db, 801, end='2026-11-12 15:00:00')
    finished(db, 802)
    print_record(db, 901, job=801, start='2026-11-12 08:00:00', end='2026-11-12 09:00:00')
    print_record(db, 902, job=802)
    client.post('/printers/199/plate-cleared', headers=issue(WRITE))
    assert [row.job_id for row in events(db)] == [802]
    assert json.loads(audits(db)[0].details)['print_job_id'] == 902


@pytest.mark.parametrize('end,expected', [('2026-11-12T14:00:00+02:00', 901),
                                         ('2026-11-12T15:00:00+02:00', 902)])
def test_print_selection_compares_instants_then_higher_ids(floor, end, expected):
    client, issue, db = floor
    print_record(db, 901)
    print_record(db, 902, end=end)
    client.post('/printers/199/plate-cleared', headers=issue(WRITE))
    assert stored_clear(db)['print_job_id'] == expected


@pytest.mark.parametrize('owner', [1, None])
def test_printer_owned_confirmation_respects_organization_visibility(floor, owner):
    from core.printer_display_facts import plate_clear_times
    client, issue, db = floor
    db.execute(text('UPDATE printers SET org_id=:org WHERE id=199'), {'org': owner})
    print_record(db)
    client.post('/printers/199/plate-cleared', headers=issue(WRITE))
    other = {'id': 5, 'role': 'admin', 'group_id': 2}
    shown = plate_clear_times(db, [199], other)
    assert bool(shown) is (owner is None)
    # Job visibility predicates only restrict job confirmations.
    assert plate_clear_times(db, [199], {'id': 3, 'group_id': 1}, job_clauses=('1=0',))


def test_unlinked_print_cannot_be_confirmed_from_another_organization(floor):
    from modules.printers.models import Printer
    from modules.printers.plate_clear import record_plate_clear
    client, issue, db = floor
    print_record(db)
    db.execute(text('UPDATE printers SET org_id=2 WHERE id=199')); db.commit()
    assert client.post('/printers/199/plate-cleared', headers=issue(WRITE)).status_code == 404
    assert record_plate_clear(db, db.get(Printer, 199), {'id': 3, 'group_id': 1}) is None
    assert stored_clear(db) is None and audits(db) == []


@pytest.mark.parametrize('blocked', ['organization', 'controlled', 'archived'])
def test_linked_print_keeps_job_visibility_fences(floor, monkeypatch, blocked):
    from core import controlled_parts
    client, issue, db = floor
    finished(db, 802, org=2 if blocked == 'organization' else 1)
    print_record(db, job=802)
    if blocked == 'controlled':
        monkeypatch.setattr(controlled_parts, 'allowed', lambda *args: False)
    if blocked == 'archived':
        archive_submission(db, 802)
    assert client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()['message'] == 'No jobs waiting for plate clear'
    assert events(db) == [] and stored_clear(db) is None and audits(db) == []


@pytest.mark.parametrize('stored_at,expected', [('2026-11-12T16:00:00+02:00', '2026-11-12T14:00:00+00:00'),
                                              ('2026-11-12T17:30:00+04:00', '2026-11-12T13:45:00+00:00')])
def test_newest_visible_store_wins_by_instant_on_read_and_replay(floor, stored_at, expected):
    client, issue, db = floor
    print_record(db)
    client.post('/printers/199/plate-cleared', headers=issue(WRITE))
    entry = stored_clear(db); entry['at'] = stored_at
    db.execute(text("UPDATE system_config SET value=:v WHERE key='plate_clear:199'"), {'v': json.dumps(entry)})
    finished(db, 802)
    db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,occurred_at) "
                    "VALUES('both',802,199,1,'bed_clear_confirmed','{}','2026-11-12 13:45:00')")); db.commit()
    shown = datetime.fromisoformat(confirmed_at(client, issue))
    assert (shown.replace(tzinfo=timezone.utc) if shown.tzinfo is None else shown) == datetime.fromisoformat(expected)
    assert client.post('/printers/199/plate-cleared', headers=issue(WRITE)).json()['plate_clear_confirmed_at'] == expected
    assert len(audits(db)) == 1 and len(events(db)) == 1


def test_recreated_printer_ignores_old_confirmation_and_deletion_removes_it(review_db):
    from core.printer_display_facts import plate_clear_times
    from modules.printers.models import Printer
    db = review_db
    db.execute(text("INSERT INTO printers(id,name,created_at) VALUES(199,'Recreated','2026-11-12 10:00:00')"))
    entry = {'at': '2026-11-12T14:00:00+00:00', 'print_job_id': 901, 'org_id': None,
             'printer_created_at': '2026-11-11 10:00:00'}
    db.execute(text("INSERT INTO system_config(key,value) VALUES('plate_clear:199',:v)"), {'v': json.dumps(entry)})
    db.commit()
    assert plate_clear_times(db, [199], {'id': 3, 'group_id': 1}) == {}
    db.delete(db.get(Printer, 199)); db.commit()
    assert stored_clear(db) is None


@pytest.fixture
def audited_floor(floor):
    from fastapi import Request
    from sqlalchemy.orm import Session
    from core.db import get_db
    from core.audit_actor import register_request_session, clear_session
    from core.middleware.audit_actor import AuditActorMiddleware
    client, issue, db = floor
    client.app.add_middleware(AuditActorMiddleware)
    def database(request: Request):
        with Session(db.get_bind()) as session:
            register_request_session(session, request)
            try:
                yield session
            finally:
                clear_session(session)
    client.app.dependency_overrides[get_db] = database
    return client, issue, db


@pytest.mark.parametrize('linked', [False, True])
@pytest.mark.parametrize('credential', ['token', 'user'])
def test_confirmation_audit_records_actor_once_in_both_stores(audited_floor, linked, credential):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client, issue, db = audited_floor
    if linked:
        finished(db, 802)
    print_record(db, job=802 if linked else None)
    headers = issue(WRITE) if credential == 'token' else interactive_headers()
    first = client.post('/printers/199/plate-cleared', headers=headers).json()
    second = client.post('/printers/199/plate-cleared', headers=headers).json()
    assert first == second and len(audits(db)) == 1
    row = db.execute(text("SELECT actor_user_id,actor_auth_kind FROM audit_logs "
                          "WHERE action='printer.plate_clear_confirmed'")).one()
    assert tuple(row) == (3, 'user_api_token' if credential == 'token' else 'bearer_jwt')
    details = json.loads(audits(db)[0].details)
    assert details['print_job_id'] == 901 and details['job_id'] == (802 if linked else None)
    assert (details['actor_token_id'] is not None) is (credential == 'token')


@pytest.mark.parametrize('linked', [False, True])
def test_audit_failure_rolls_back_confirmation(floor, linked):
    from sqlalchemy import event
    client, issue, db = floor
    if linked:
        finished(db, 802)
    print_record(db, job=802 if linked else None)
    headers = issue(WRITE)
    def refuse(conn, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO audit_logs'):
            raise RuntimeError('Synthetic audit failure')
    event.listen(db.get_bind(), 'before_cursor_execute', refuse)
    try:
        with pytest.raises(RuntimeError, match='Synthetic audit failure'):
            client.post('/printers/199/plate-cleared', headers=headers)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', refuse)
    db.rollback()
    assert stored_clear(db) is None and events(db) == [] and audits(db) == []
