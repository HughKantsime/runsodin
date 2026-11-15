"""Home Assistant status, integration 0.3.0 fields (spec: .claude/specs/ha-integration-next.md, items C, D, A, B).

Per printer: last_completed_at, last_error_at, finished_at, plate_clear_confirmed_at, slots.
Per next job: queued_after, short_spools, ready. Fixture data comes from
test_home_assistant_status.py (caller: user 3 in organization 1; printers 9 and 10 are its own,
printer 71 belongs to organization 2).
"""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, text
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers, token_client, review_db  # noqa: F401
from tests.test_contracts.test_home_assistant_print_floor import _get, _job, _printers, ams  # noqa: F401
from tests.test_contracts.test_home_assistant_status import URL, _mark_controlled, _stamp, ha_client  # noqa: F401

NEW_PRINTER_KEYS = {'last_completed_at', 'last_error_at', 'finished_at', 'plate_clear_confirmed_at', 'slots'}
NEW_JOB_KEYS = {'queued_after', 'short_spools', 'ready'}


BASE = datetime(2026, 11, 10, 12, 0, 0)


def _at(seconds_ago):
    """A stored (naive UTC) time, counted back from a fixed moment so tests can compare exact values."""
    return (BASE - timedelta(seconds=seconds_ago)).isoformat(sep=' ')


def _iso(seconds_ago):
    return (BASE - timedelta(seconds=seconds_ago)).isoformat() + 'Z'


def _print(db, printer, status, started, ended, job=None):
    db.execute(text('INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at,scheduled_job_id) '
                    'VALUES(:p,:n,:s,:a,:e,:j)'),
               {'p': printer, 'n': 'part.gcode', 's': status, 'a': _at(started),
                'e': None if ended is None else _at(ended), 'j': job})


def _error(db, printer, seconds_ago, message='m'):
    db.execute(text("UPDATE printers SET hms_errors='[]', last_error_code='PRINT_FAILED', last_error_message=:m, "
                    "last_error_at=:at WHERE id=:id"), {'m': message, 'at': _at(seconds_ago), 'id': printer})


def _spool(db, spool_id, remaining, initial=1000.0):
    db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(:i,'Polymaker','Galaxy Black','PLA')"),
               {'i': spool_id})
    db.execute(text('INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g) VALUES(:i,:i,:w,:r)'),
               {'i': spool_id, 'w': initial, 'r': remaining})


def _load(db, printer, slot, spool):
    db.execute(text('UPDATE filament_slots SET assigned_spool_id=:s WHERE printer_id=:p AND slot_number=:n'),
               {'s': spool, 'p': printer, 'n': slot})


def _clear(db, job, printer, seconds_ago, org=1):
    db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,occurred_at) "
                    "VALUES(:id,:j,:p,:o,'bed_clear_confirmed','{}',:at)"),
               {'id': f'clear-{job}-{seconds_ago}', 'j': job, 'p': printer, 'o': org, 'at': _at(seconds_ago)})


# ---------------------------------------------------------------- shape

def test_new_fields_are_additive_and_null_when_unknown(ha_client):
    client, issue = ha_client
    body = _get(client, issue)
    assert body['schema_version'] == 2  # fields were only added
    for printer in body['printers']:
        assert NEW_PRINTER_KEYS <= set(printer)
        assert {'id', 'name', 'status', 'next_job', 'bench', 'ams', 'last_error'} <= set(printer)
    stale = _printers(body)[72]  # no prints, no error, no slots: unknown is null, never 0
    assert stale['last_completed_at'] is None and stale['last_error_at'] is None
    assert stale['finished_at'] is None and stale['plate_clear_confirmed_at'] is None
    assert stale['slots'] == []
    assert _printers(body)[12]['last_error_at'] == '2026-11-08T23:36:50Z'


# ---------------------------------------------------------------- C: status after a finished print

def test_a_print_that_completed_after_the_error_resolves_it(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text('DELETE FROM print_jobs WHERE printer_id=10'))
    _error(review_db, 10, 3600)
    _print(review_db, 10, 'completed', started=7200, ended=600)  # started before the error, completed after it
    review_db.commit()
    printer = _printers(_get(client, issue))[10]
    assert printer['status'] == 'idle' and printer['last_error']['unresolved'] is False
    assert printer['last_completed_at'] == _iso(600) and printer['last_error_at'] == _iso(3600)
    assert printer['last_error']['at'] == printer['last_error_at']


@pytest.mark.parametrize('outcome', ['failed', 'cancelled'])
def test_a_print_that_failed_after_the_error_does_not_resolve_it(ha_client, review_db, outcome):
    client, issue = ha_client
    review_db.execute(text('DELETE FROM print_jobs WHERE printer_id=10'))
    _error(review_db, 10, 3600)
    _print(review_db, 10, outcome, started=7200, ended=600)
    review_db.commit()
    printer = _printers(_get(client, issue))[10]
    assert printer['status'] == 'error' and printer['last_error']['unresolved'] is True
    assert printer['last_completed_at'] is None and printer['finished_at'] == _iso(600)


def test_an_error_after_the_last_completed_print_stays_unresolved(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text('DELETE FROM print_jobs WHERE printer_id=10'))
    _print(review_db, 10, 'completed', started=7200, ended=3600)
    _error(review_db, 10, 600)
    review_db.commit()
    printer = _printers(_get(client, issue))[10]
    assert printer['status'] == 'error' and printer['last_error']['unresolved'] is True
    assert printer['last_completed_at'] == _iso(3600) and printer['last_error_at'] == _iso(600)


@pytest.mark.parametrize('error_at,started,completed,expected', [
    ('2026-11-08 10:00:00', '2026-11-08 09:00:00', None, True),
    ('2026-11-08 10:00:00', '2026-11-08 09:00:00', '2026-11-08 09:30:00', True),
    ('2026-11-08 10:00:00', '2026-11-08 09:00:00', '2026-11-08 10:00:00', True),
    ('2026-11-08 10:00:00', '2026-11-08 09:00:00', '2026-11-08T11:00:00+00:00', False),
    ('2026-11-08 10:00:00', None, '2026-11-08 11:00:00', False),
    ('2026-11-08 10:00:00', '2026-11-08 11:00:00', None, False),
    (None, None, '2026-11-08 11:00:00', False),
])
def test_unresolved_error_rule_with_a_completion(error_at, started, completed, expected):
    from modules.printers.routes_integrations import unresolved_error
    assert unresolved_error(error_at, started, completed) is expected


def test_last_completed_is_the_last_print_by_instant_not_by_text(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text('DELETE FROM print_jobs WHERE printer_id=10'))
    # ISO text with an offset sorts after a naive UTC time as text; the later print must win.
    review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at) VALUES"
                           "(10,'a','completed','2026-11-01T09:00:00+02:00','2026-11-01T10:00:00+02:00'),"
                           "(10,'b','completed','2026-11-01 08:30:00','2026-11-01 09:00:00')"))
    review_db.commit()
    printer = _printers(_get(client, issue))[10]
    assert printer['last_completed_at'] == printer['finished_at'] == '2026-11-01T09:00:00Z'


# ---------------------------------------------------------------- D: finished_at, plate_clear_confirmed_at

def test_finished_at_is_the_end_of_the_last_print_and_null_while_printing(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text('DELETE FROM print_jobs WHERE printer_id=10'))
    _print(review_db, 10, 'completed', started=9000, ended=7200)
    _print(review_db, 10, 'failed', started=3600, ended=1800)
    _print(review_db, 9, 'completed', started=9000, ended=7200)  # printer 9 is printing again
    review_db.commit()
    printers = _printers(_get(client, issue))
    assert printers[10]['finished_at'] == _iso(1800) and printers[10]['last_completed_at'] == _iso(7200)
    assert printers[9]['finished_at'] is None and printers[9]['last_completed_at'] == _iso(7200)


def test_plate_clear_confirmation_is_reported_until_the_next_print_starts(ha_client, review_db):
    client, issue = ha_client
    # Keep the fixture's completed print before this test's fixed confirmation.
    review_db.execute(text('UPDATE print_jobs SET started_at=:at WHERE printer_id=10'), {'at': _at(7200)})
    _job(review_db, 600, 'Done plate', 'completed', printer=10, actual_start=_at(7200), actual_end=_at(3600))
    _clear(review_db, 600, 10, 1800)
    review_db.commit()
    assert _printers(_get(client, issue))[10]['plate_clear_confirmed_at'] == _iso(1800)
    _job(review_db, 601, 'Next plate', 'printing', printer=10, actual_start=_at(600))
    review_db.commit()
    assert _printers(_get(client, issue))[10]['plate_clear_confirmed_at'] is None


@pytest.mark.parametrize('status', ['running', 'completed', 'failed', 'cancelled'])
@pytest.mark.parametrize('started_at', ['2026-11-10 11:50:00', '2026-11-10T06:50:00-05:00'])
def test_unlinked_print_invalidates_plate_clear_confirmation(ha_client, review_db, status, started_at):
    client, issue = ha_client
    review_db.execute(text('DELETE FROM print_jobs WHERE printer_id=10'))
    _job(review_db, 600, 'Done plate', 'completed', printer=10, actual_start=_at(7200), actual_end=_at(3600))
    _clear(review_db, 600, 10, 1800)
    # Older/equal starts and a newer print on another printer do not invalidate it.
    _print(review_db, 10, 'completed', started=7200, ended=3600)
    _print(review_db, 10, 'completed', started=1800, ended=1500)
    _print(review_db, 9, 'running', started=600, ended=None)
    review_db.commit()
    assert _printers(_get(client, issue))[10]['plate_clear_confirmed_at'] == _iso(1800)

    review_db.execute(text('INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at) '
                           "VALUES(10,'walk-up.gcode',:status,:started,:ended)"),
                      {'status': status, 'started': started_at, 'ended': None if status == 'running' else _at(300)})
    review_db.execute(text('UPDATE printers SET gcode_state=:state WHERE id=10'),
                      {'state': 'RUNNING' if status == 'running' else 'FINISH'})
    review_db.commit()
    assert _printers(_get(client, issue))[10]['plate_clear_confirmed_at'] is None


def test_times_of_another_organizations_work_on_a_shared_printer_are_withheld(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=71'))
    review_db.execute(text("DELETE FROM print_jobs WHERE printer_id=71"))
    _job(review_db, 610, 'foreign plate', 'completed', printer=71, charged_to_org_id=2,
         actual_start=_at(7200), actual_end=_at(3600))
    _print(review_db, 71, 'completed', started=7200, ended=3600, job=610)
    _clear(review_db, 610, 71, 1800, org=2)
    review_db.commit()
    printer = _printers(_get(client, issue))[71]
    assert printer['last_completed_at'] is None and printer['finished_at'] is None
    assert printer['plate_clear_confirmed_at'] is None
    # The printer's own walk-up print (no job) is the printer's, as in the day totals.
    _print(review_db, 71, 'completed', started=1500, ended=900)
    review_db.commit()
    printer = _printers(_get(client, issue))[71]
    assert printer['last_completed_at'] == printer['finished_at'] == _iso(900)
    # Organization 2's own people (here: a superadmin) see all of it.
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.commit()
    assert _printers(_get(client, issue))[71]['plate_clear_confirmed_at'] is None
    # A fresh confirmation after the walk-up print is visible to the superadmin.
    _clear(review_db, 610, 71, 300, org=2)
    review_db.commit()
    assert _printers(_get(client, issue))[71]['plate_clear_confirmed_at'] == _iso(300)
    review_db.execute(text('UPDATE users SET group_id=1 WHERE id=3'))
    review_db.commit()
    assert _printers(_get(client, issue))[71]['plate_clear_confirmed_at'] is None


def test_times_of_a_hidden_controlled_job_are_withheld_but_still_resolve_the_error(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text('DELETE FROM print_jobs WHERE printer_id=10'))
    _job(review_db, 620, 'secret-plate', 'completed', printer=10, actual_start=_at(7200), actual_end=_at(600))
    _mark_controlled(review_db, 620)  # the caller is not in the audience
    _error(review_db, 10, 3600)
    _print(review_db, 10, 'completed', started=7200, ended=600, job=620)
    _clear(review_db, 620, 10, 300)
    review_db.commit()
    response = client.get(URL, headers=issue(['read']))
    printer = _printers(response.json())[10]
    assert 'secret' not in response.text
    assert printer['last_completed_at'] is None and printer['finished_at'] is None
    assert printer['plate_clear_confirmed_at'] is None
    assert printer['status'] == 'idle'  # the printer's state is not hidden, only the job's details
    # An audience member sees the same times.
    review_db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)'))
    review_db.commit()
    printer = _printers(_get(client, issue))[10]
    assert printer['last_completed_at'] == printer['finished_at'] == _iso(600)
    assert printer['plate_clear_confirmed_at'] == _iso(300)


# ---------------------------------------------------------------- A: queued_after, short_spools, ready

def test_queued_after_counts_the_further_visible_jobs_for_the_printer(ha_client, review_db):
    client, issue = ha_client
    _job(review_db, 630, 'First', printer=10, scheduled_start='2026-12-01 10:00:00')
    review_db.commit()
    job = _printers(_get(client, issue))[10]['next_job']
    assert NEW_JOB_KEYS <= set(job) and job['queued_after'] == 0
    _job(review_db, 631, 'Second', printer=10, scheduled_start='2026-12-02 10:00:00')
    _job(review_db, 632, 'Third', 'scheduled', printer=10)
    _job(review_db, 633, 'Done', 'completed', printer=10)
    _job(review_db, 634, 'Elsewhere', printer=9)
    _job(review_db, 635, 'Foreign', printer=10, charged_to_org_id=2)   # another organization's job
    _job(review_db, 636, 'secret-queued', printer=10)                  # controlled, caller not in the audience
    _mark_controlled(review_db, 636)
    review_db.commit()
    response = client.get(URL, headers=issue(['read']))
    printers = _printers(response.json())
    assert printers[10]['next_job']['id'] == 630 and printers[10]['next_job']['queued_after'] == 2
    assert printers[9]['next_job']['queued_after'] == 0
    assert 'secret' not in response.text
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))  # a superadmin also lists the foreign job
    review_db.commit()
    assert _printers(_get(client, issue))[10]['next_job']['queued_after'] == 3


def test_short_spools_come_from_the_filament_check_and_make_the_job_unready(ams, review_db):
    client, issue = ams
    _job(review_db, 640, 'Bracket', printer=9, model_id=12, ams_mapping='[0, 1]', quantity=1)
    _spool(review_db, 1, 500.0)
    _spool(review_db, 2, 2.0)   # PETG needs 3 g plus the 10 % margin
    _load(review_db, 9, 1, 1)
    _load(review_db, 9, 2, 2)
    review_db.commit()
    job = _printers(_get(client, issue))[9]['next_job']
    assert job['ams_check']['status'] == 'match'
    assert job['short_spools'] == [{'slot': 2, 'needed_g': 3.3, 'remaining_g': 2.0}]
    assert job['ready'] is False
    review_db.execute(text('UPDATE spools SET remaining_weight_g=400 WHERE id=2'))
    review_db.commit()
    job = _printers(_get(client, issue))[9]['next_job']
    assert job['short_spools'] == [] and job['ready'] is True


def test_short_spools_are_null_when_the_check_cannot_judge_or_is_off(ams, review_db):
    client, issue = ams
    _job(review_db, 641, 'Bracket', printer=9, model_id=12, ams_mapping='[0, 1]', quantity=1)
    review_db.commit()
    job = _printers(_get(client, issue))[9]['next_job']  # no spool assigned: unknown, never 0 and never short
    assert job['short_spools'] is None
    assert job['ready'] is True  # colours match; an unknown weight never holds a dispatch
    _spool(review_db, 2, 1.0)
    _load(review_db, 9, 2, 2)
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('filament_check_mode','\"off\"')"))
    review_db.commit()
    from modules.printers import filament_check
    assert filament_check.read_settings(review_db.connection())['mode'] == 'off'
    job = _printers(_get(client, issue))[9]['next_job']
    assert job['short_spools'] is None and job['ready'] is True


@pytest.mark.parametrize('case', ['held', 'mismatch', 'refused', 'maintenance', 'unknown_colours'])
def test_ready_is_false_when_held_or_blocked_and_null_when_colours_are_unknown(ams, review_db, case):
    client, issue = ams
    fields = {'model_id': 12, 'ams_mapping': '[0, 1]', 'quantity': 1}
    if case == 'held':
        fields['hold'] = 1
    if case == 'unknown_colours':
        fields.pop('ams_mapping')
    _job(review_db, 642, 'Bracket', printer=9, **fields)
    if case == 'mismatch':
        review_db.execute(text("UPDATE filament_slots SET filament_type='PLA' WHERE printer_id=9 AND slot_number=2"))
    if case == 'refused':
        review_db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,kind,code,occurred_at) "
                               "VALUES('refusal-642',642,9,'gate_refused','NOZZLE_MISMATCH',:at)"), {'at': _stamp(60)})
    if case == 'maintenance':
        review_db.execute(text("INSERT INTO system_config(key,value) VALUES('printer.maintenance.9',:v)"),
                          {'v': json.dumps({'state': 'hold', 'note': 'Belt', 'set_by': 3, 'set_by_name': 'admin',
                                            'set_at': '2026-11-12T05:00:00+00:00'})})
    review_db.commit()
    job = _printers(_get(client, issue))[9]['next_job']
    assert job['ready'] is (None if case == 'unknown_colours' else False)


def test_a_controlled_next_job_keeps_spool_details_withheld(ams, review_db):
    client, issue = ams
    _job(review_db, 401, 'secret-bracket', 'printing', printer=9)
    _mark_controlled(review_db, 401)  # printer 9 carries work this caller cannot name
    _job(review_db, 643, 'secret-visible', printer=9, model_id=12, ams_mapping='[0, 1]', quantity=1)
    _job(review_db, 644, 'secret-later', printer=9, model_id=12)
    _spool(review_db, 2, 1.0)
    _load(review_db, 9, 2, 2)
    review_db.commit()
    response = client.get(URL, headers=issue(['read']))
    job = _printers(response.json())[9]['next_job']
    assert job['name'] == 'Controlled work' and job['filaments'] is None
    assert job['short_spools'] is None and job['ready'] is None
    assert job['queued_after'] == 1  # both jobs are listable by this caller, so both are counted
    assert 'secret' not in response.text
    # What is loaded on that printer describes the controlled work's filaments: withheld too.
    assert _printers(response.json())[9]['slots'] is None
    assert _printers(response.json())[10]['slots'] is not None  # other printers are unaffected
    review_db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)'))
    review_db.commit()
    member = _printers(_get(client, issue))[9]
    assert [slot['material'] for slot in member['slots']] == ['PLA', 'PETG']
    assert member['next_job']['short_spools'] == [{'slot': 2, 'needed_g': 3.3, 'remaining_g': 1.0}]
    assert member['next_job']['ready'] is False


def test_short_spools_on_a_shared_printer_hide_the_owners_remaining_weight(ams, review_db):
    client, issue = ams
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=71'))  # organization 2's, reached only because it is shared
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color) VALUES"
                           "(71,1,'PLA','black'),(71,2,'PETG','white')"))
    _job(review_db, 645, 'Bracket', printer=71, model_id=12, ams_mapping='[0, 1]', quantity=1)
    _spool(review_db, 2, 2.0)
    _load(review_db, 71, 2, 2)
    review_db.commit()
    printer = _printers(_get(client, issue))[71]
    assert printer['next_job']['short_spools'] == [{'slot': 2, 'needed_g': 3.3, 'remaining_g': None}]
    assert printer['next_job']['ready'] is False
    assert all(slot['remaining_g'] is None and slot['remaining_percent'] is None for slot in printer['slots'])


def test_a_failing_filament_check_leaves_the_rest_of_the_payload(ams, review_db, monkeypatch):
    from modules.printers import filament_check
    client, issue = ams
    _job(review_db, 646, 'Bracket', printer=9, model_id=12, ams_mapping='[0, 1]', quantity=1)
    review_db.commit()

    def broken(*args, **kwargs):
        raise RuntimeError('check unavailable')
    monkeypatch.setattr(filament_check, 'check_job', broken)
    job = _printers(_get(client, issue))[9]['next_job']
    assert job['id'] == 646 and job['short_spools'] is None and job['ready'] is True


# ---------------------------------------------------------------- B: slots

def test_slots_report_what_is_loaded_with_clean_names(ams, review_db):
    client, issue = ams
    _spool(review_db, 1, 250.0)
    _load(review_db, 9, 1, 1)
    review_db.execute(text("UPDATE filament_slots SET color='Polymaker Galaxy Black', color_hex='#1a1a1aff' "
                           "WHERE printer_id=9 AND slot_number=1"))
    review_db.execute(text("UPDATE filament_slots SET filament_type='PLA_SUPPORT', color='#FFFFFF', color_hex='FFFFFF' "
                           "WHERE printer_id=9 AND slot_number=2"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,3,'empty')"))
    review_db.commit()
    slots = _printers(_get(client, issue))[9]['slots']
    assert [set(slot) for slot in slots] == [{'slot', 'color_hex', 'name', 'material', 'remaining_percent', 'remaining_g'}] * 3
    # Printer 9 is a Bambu: the percentage is the printer's own measurement, which this fixture
    # does not have, so it is unknown and the gram balance is not shown either (the Printers page's rule).
    assert slots[0] == {'slot': 1, 'color_hex': '#1A1A1A', 'name': 'Galaxy Black', 'material': 'PLA',
                        'remaining_percent': None, 'remaining_g': None}
    assert slots[1] == {'slot': 2, 'color_hex': '#FFFFFF', 'name': None, 'material': 'PLA Support',
                        'remaining_percent': None, 'remaining_g': None}
    assert slots[2] == {'slot': 3, 'color_hex': None, 'name': None, 'material': None,
                        'remaining_percent': None, 'remaining_g': None}


def test_slot_remaining_follows_the_printers_page(ha_client, review_db):
    client, issue = ha_client
    _spool(review_db, 5, 250.0)
    _spool(review_db, 6, 0.0)
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,assigned_spool_id) VALUES"
                           "(12,1,'PETG','blue',5),(12,2,'PLA','red',6),(12,3,'PLA','green',NULL)"))
    # A Bambu tray reports its own percentage (tray_observation).
    _spool(review_db, 7, 640.0)
    review_db.execute(text("UPDATE filament_slots SET tray_observation=:o, assigned_spool_id=7 WHERE printer_id=9 AND slot_number=1"),
                      {'o': json.dumps({'remaining': 64})})
    review_db.commit()
    printers = _printers(_get(client, issue))
    kobra = {slot['slot']: slot for slot in printers[12]['slots']}
    assert (kobra[1]['remaining_percent'], kobra[1]['remaining_g']) == (25.0, 250.0)
    assert (kobra[2]['remaining_percent'], kobra[2]['remaining_g']) == (0.0, 0.0)   # a known zero stays zero
    assert (kobra[3]['remaining_percent'], kobra[3]['remaining_g']) == (None, None)  # no spool: unknown
    assert (printers[9]['slots'][0]['remaining_percent'], printers[9]['slots'][0]['remaining_g']) == (64.0, 640.0)


def test_slots_on_a_shared_printer_hide_the_owners_remaining_amounts(ha_client, review_db):
    client, issue = ha_client
    _spool(review_db, 5, 250.0)
    review_db.execute(text('UPDATE printers SET shared=1, api_type=\'moonraker\' WHERE id=71'))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,color_hex,assigned_spool_id) "
                           "VALUES(71,1,'PETG','blue','0000FF',5)"))
    review_db.commit()
    assert _printers(_get(client, issue))[71]['slots'] == [
        {'slot': 1, 'color_hex': '#0000FF', 'name': 'blue', 'material': 'PETG',
         'remaining_percent': None, 'remaining_g': None}]
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))  # the owner's side (a superadmin)
    review_db.commit()
    slot = _printers(_get(client, issue))[71]['slots'][0]
    assert (slot['remaining_percent'], slot['remaining_g']) == (25.0, 250.0)


@pytest.mark.parametrize('code,label', [
    ('PLA', 'PLA'), ('PLA_SUPPORT', 'PLA Support'), ('PETG_CF', 'PETG-CF'), ('NYLON_GF', 'NYLON-GF'),
    ('SOME_NEW_TYPE', 'SOME NEW TYPE'), ('empty', None), (None, None), ('', None)])
def test_material_label_matches_the_web_page(code, label):
    from modules.printers.integration_floor import material_label
    assert material_label(code) == label


@pytest.mark.parametrize('color,name', [
    ('preset: Bambu Lab PLA Basic Jade White', 'PLA Basic Jade White'), ('Polymaker Galaxy Black', 'Galaxy Black'),
    ('#FF0000', None), ('FF0000FF', None), ('', None), (None, None), ('red matte', 'red matte')])
def test_slot_color_name_matches_the_web_page(color, name):
    from modules.printers.integration_floor import slot_color_name
    assert slot_color_name(color) == name


# ---------------------------------------------------------------- cost

def test_new_fields_do_not_add_queries_per_printer_or_write(ha_client, review_db):
    from sqlalchemy.orm import Session
    from core.db import get_db
    client, issue = ha_client

    def request_scoped_database():
        with Session(review_db.get_bind()) as db:
            db.info['_controlled_request'] = True
            yield db
    client.app.dependency_overrides[get_db] = request_scoped_database
    headers = issue(['read'])
    engine = review_db.get_bind()
    statements = []
    listener = lambda conn, cursor, statement, *args: statements.append(statement)

    def measure():
        statements.clear()
        event.listen(engine, 'before_cursor_execute', listener)
        try:
            assert client.get(URL, headers=headers).status_code == 200
        finally:
            event.remove(engine, 'before_cursor_execute', listener)
        return len(statements)

    review_db.execute(text("UPDATE print_jobs SET ended_at=:end WHERE printer_id=10"), {'end': _stamp(60)})
    review_db.commit()
    measure()
    small = measure()
    for pid in range(500, 540):
        review_db.execute(text("INSERT INTO printers(id,name,api_type,is_active,org_id,gcode_state,last_seen) "
                               "VALUES(:id,:name,'bambu',1,1,'IDLE',:seen)"), {'id': pid, 'name': f'Fleet {pid}', 'seen': _stamp(5)})
        review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at) "
                               "VALUES(:id,'fleet.gcode','completed',:start,:end)"),
                          {'id': pid, 'start': _stamp(600), 'end': _stamp(60)})
        review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color) VALUES(:id,1,'PLA','black')"),
                          {'id': pid})
    review_db.commit()
    assert measure() == small
    assert not [s for s in statements if s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
