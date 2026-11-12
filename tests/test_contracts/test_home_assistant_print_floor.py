"""Home Assistant print floor: next job, bench state, fleet day totals (spec: .claude/specs/ha-print-floor.md).

Fixture style and seed data come from test_home_assistant_status.py (printers 9, 10, 12, 71-73;
jobs 13, 301-304; org 1 caller).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers, token_client, review_db  # noqa: F401
from tests.test_contracts.test_home_assistant_status import URL, _mark_controlled, _stamp, ha_client  # noqa: F401

FILAMENTS = [{'slot': 1, 'type': 'PLA', 'color': '#000000', 'used_grams': 12.5},
             {'slot': 2, 'type': 'PETG', 'color': '#FFFFFF', 'used_grams': 3}]


def _job(db, job_id, name, status='pending', printer=None, **fields):
    columns = {'id': job_id, 'item_name': name, 'status': status, 'charged_to_org_id': 1,
               'printer_id': printer, 'priority': 3, **fields}
    db.execute(text('INSERT INTO jobs(' + ','.join(columns) + ') VALUES(' + ','.join(':' + k for k in columns) + ')'), columns)


def _get(client, issue):
    response = client.get(URL, headers=issue(['read']))
    assert response.status_code == 200
    return response.json()


def _printers(body):
    return {p['id']: p for p in body['printers']}


def _utc_text(moment):
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')


@pytest.fixture
def ams(ha_client, review_db, monkeypatch):
    """Printer 9 (Bambu) with trays 1 PLA black and 2 PETG white; file 11 needs PLA then PETG."""
    import core.db as core_db
    monkeypatch.setattr(core_db, 'engine', review_db.get_bind())  # the AMS gate comparison opens its own connection
    review_db.execute(text('UPDATE print_files SET plate_count=3, filaments_json=:f WHERE id=11'), {'f': json.dumps(FILAMENTS)})
    review_db.execute(text("UPDATE filament_slots SET color='black' WHERE printer_id=9 AND slot_number=1"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color) VALUES(9,2,'PETG','white')"))
    review_db.commit()
    return ha_client


def test_schema_version_bumped_and_keys_are_additive(ha_client):
    client, issue = ha_client
    body = _get(client, issue)
    assert body['schema_version'] == 2
    for printer in body['printers']:
        assert 'next_job' in printer and 'bench' in printer
        assert {'id', 'name', 'status', 'job_name', 'last_error', 'camera_available'} <= set(printer)
    assert {'queue_length', 'today', 'queued_hours', 'queued_unestimated', 'next_unassigned'} <= set(body['fleet'])
    assert {'plates_done', 'plates_failed', 'since'} == set(body['fleet']['today'])


def test_next_job_is_the_first_queued_job_assigned_to_the_printer(ha_client, review_db):
    client, issue = ha_client
    _job(review_db, 501, 'Later', printer=9, scheduled_start='2026-12-02 10:00:00')
    _job(review_db, 502, 'Sooner', printer=9, scheduled_start='2026-12-01 10:00:00', duration_hours=1.5)
    _job(review_db, 507, 'Sooner but urgent', printer=9, scheduled_start='2026-12-01 10:00:00', priority=1)
    _job(review_db, 503, 'Unscheduled', printer=9)
    _job(review_db, 504, 'Done', 'completed', printer=9, scheduled_start='2026-11-01 10:00:00')
    _job(review_db, 505, 'Other printer', printer=10, scheduled_start='2026-11-01 10:00:00')
    review_db.commit()
    printers = _printers(_get(client, issue))
    assert printers[9]['next_job']['id'] == 507
    assert printers[9]['next_job']['scheduled_start'] == '2026-12-01T10:00:00Z'
    assert printers[10]['next_job']['id'] == 505
    assert printers[12]['next_job'] is None and printers[72]['next_job'] is None
    review_db.execute(text('UPDATE jobs SET status=\'completed\' WHERE id=507'))
    review_db.commit()
    nine = _printers(_get(client, issue))[9]['next_job']
    assert nine['id'] == 502 and nine['est_minutes'] == 90 and nine['held'] is False


def test_next_job_carries_copies_plate_filaments_and_slots(ams, review_db):
    client, issue = ams
    _job(review_db, 550, 'Bracket', printer=9, model_id=12, ams_mapping='[0, 1]', quantity_on_bed=4, plate_index=1)
    review_db.commit()
    job = _printers(_get(client, issue))[9]['next_job']
    assert job['id'] == 550 and 'Bracket' in job['name']
    assert job['copies_on_plate'] == 4
    assert job['plate'] == {'index': 2, 'count': 3}  # index is one-based, plate_index is stored zero-based
    assert job['filaments'] == [{'slot': 1, 'color': '#000000', 'material': 'PLA', 'grams': 12.5},
                                {'slot': 2, 'color': '#FFFFFF', 'material': 'PETG', 'grams': 3.0}]


def test_filaments_fall_back_to_the_files_order_without_a_mapping(ams, review_db):
    client, issue = ams
    _job(review_db, 551, 'No mapping', printer=9, model_id=12)
    review_db.commit()
    job = _printers(_get(client, issue))[9]['next_job']
    assert [f['slot'] for f in job['filaments']] == [1, 2]
    assert job['ams_check']['status'] == 'unknown'
    assert [s['slot'] for s in job['ams_check']['slots']] == [1, 2]


def test_ams_check_match(ams, review_db):
    client, issue = ams
    _job(review_db, 552, 'Matches', printer=9, model_id=12, ams_mapping='[0, 1]')
    review_db.commit()
    printer = _printers(_get(client, issue))[9]
    check = printer['next_job']['ams_check']
    assert check['status'] == 'match'
    assert check['slots'] == [
        {'slot': 1, 'need': {'color': '#000000', 'material': 'PLA'}, 'loaded': {'color': 'black', 'material': 'PLA'}, 'ok': True},
        {'slot': 2, 'need': {'color': '#FFFFFF', 'material': 'PETG'}, 'loaded': {'color': 'white', 'material': 'PETG'}, 'ok': True}]
    assert printer['bench'] is None


def test_ams_check_mismatch_names_the_slot_and_blocks_the_bench(ams, review_db):
    client, issue = ams
    _job(review_db, 553, 'Mismatch', printer=9, model_id=12, ams_mapping='[0, 1]')
    review_db.execute(text("UPDATE filament_slots SET filament_type='PLA' WHERE printer_id=9 AND slot_number=2"))
    review_db.commit()
    printer = _printers(_get(client, issue))[9]
    check = printer['next_job']['ams_check']
    assert check['status'] == 'mismatch'
    assert [(s['slot'], s['ok']) for s in check['slots']] == [(1, True), (2, False)]
    assert printer['bench']['state'] == 'blocked'
    assert printer['bench']['reason'] == 'AMS slot 2: needs PETG #FFFFFF, has PLA white'


def test_ams_check_unknown_when_a_tray_cannot_be_read(ams, review_db):
    client, issue = ams
    _job(review_db, 554, 'Missing tray', printer=9, model_id=12, ams_mapping='[0, 2]')  # tray 3 does not exist
    review_db.commit()
    printer = _printers(_get(client, issue))[9]
    check = printer['next_job']['ams_check']
    assert check['status'] == 'unknown'
    assert [s['slot'] for s in check['slots']] == [1, 3] and all(s['ok'] is None for s in check['slots'])
    assert printer['bench'] is None  # unknown never blocks


def test_held_next_job_shows_held_bench(ha_client, review_db):
    client, issue = ha_client
    _job(review_db, 540, 'Held part', printer=9, hold=1)
    review_db.commit()
    printer = _printers(_get(client, issue))[9]
    assert printer['next_job']['id'] == 540 and printer['next_job']['held'] is True
    assert printer['bench']['state'] == 'held' and printer['bench']['reason'] == 'Held, waiting for go'


def test_refused_dispatch_blocks_with_plain_words_until_a_later_attempt(ha_client, review_db):
    client, issue = ha_client
    _job(review_db, 541, 'Refused', printer=10)
    review_db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,kind,code,detail,occurred_at) "
                           "VALUES('t-refused',541,10,'gate_refused','PRINTER_MAINTENANCE_HOLD','{}','2026-11-12 08:00:00')"))
    review_db.commit()
    bench = _printers(_get(client, issue))[10]['bench']
    assert bench == {'state': 'blocked', 'reason': 'Printer is on a maintenance hold',
                     'since': '2026-11-12T08:00:00Z', 'pause_cause': None}
    review_db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,kind,detail,occurred_at) "
                           "VALUES('t-later',541,10,'dispatch_starting','{}','2026-11-12 09:00:00')"))
    review_db.commit()
    assert _printers(_get(client, issue))[10]['bench'] is None


def test_waiting_plate_clear_shows_clear_plate(ha_client, review_db):
    client, issue = ha_client
    _job(review_db, 530, 'Finished plate', 'waiting_plate_clear', printer=10)
    _job(review_db, 531, 'Next up', printer=10)
    review_db.commit()
    body = _get(client, issue)
    bench = _printers(body)[10]['bench']
    assert bench['state'] == 'clear_plate' and bench['pause_cause'] is None
    assert _printers(body)[10]['next_job']['id'] == 531  # the finished plate is not queued work


def test_next_unassigned_is_capped_at_five_in_queue_order(ha_client, review_db):
    client, issue = ha_client
    for i in range(7):
        _job(review_db, 520 + i, f'Free {i}', scheduled_start=f'2026-12-0{i + 1} 08:00:00')
    _job(review_db, 527, 'Assigned', printer=9, scheduled_start='2026-11-01 08:00:00')
    review_db.commit()
    unassigned = _get(client, issue)['fleet']['next_unassigned']
    assert [job['id'] for job in unassigned] == [520, 521, 522, 523, 524]
    assert all(job['candidate_printer_id'] is None for job in unassigned)
    assert 303 not in {job['id'] for job in unassigned}  # another organization's job


@pytest.mark.parametrize('state,hms,layer,cause,reason', [
    ('PAUSE', '[]', 12, 'planned', 'Planned pause at layer 12'),
    ('PAUSE', json.dumps([{'attr': 0x0C000300, 'code': 0x00030008, 'raw_code': 0x00030008}]), 12, 'spaghetti', None),
    ('PAUSE', json.dumps([{'attr': 1, 'code': 2, 'raw_code': 2}]), 12, 'error', 'Printer error 00000001_00000002'),
])
def test_pause_cause_planned_spaghetti_error(ha_client, review_db, state, hms, layer, cause, reason):
    client, issue = ha_client
    review_db.execute(text('UPDATE printers SET gcode_state=:s, hms_errors=:h WHERE id=9'), {'s': state, 'h': hms})
    review_db.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,kind,detail,occurred_at) "
                           "VALUES('t-paused',13,9,'paused','{}','2026-11-12 09:00:00')"))
    review_db.commit()
    printer = _printers(_get(client, issue))[9]
    assert printer['status'] == 'paused'
    assert printer['bench']['state'] == 'paused' and printer['bench']['pause_cause'] == cause
    assert printer['bench']['since'] == '2026-11-12T09:00:00Z'
    if reason:
        assert printer['bench']['reason'] == reason
    else:
        assert 'spaghetti' in printer['bench']['reason'].lower()


def test_vision_auto_pause_is_spaghetti(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text("UPDATE printers SET gcode_state='PAUSED' WHERE id=9"))
    running = review_db.execute(text("SELECT id FROM print_jobs WHERE printer_id=9 AND status='running'")).scalar_one()
    review_db.execute(text("INSERT INTO vision_detections(printer_id,print_job_id,detection_type,confidence,status) "
                           "VALUES(9,:p,'spaghetti',0.9,'auto_paused')"), {'p': running})
    review_db.commit()
    assert _printers(_get(client, issue))[9]['bench']['pause_cause'] == 'spaghetti'


def test_recorded_error_on_a_paused_non_bambu_printer_is_an_error_pause(ha_client):
    client, issue = ha_client
    bench = _printers(_get(client, issue))[12]['bench']  # fixture: PAUSED, unresolved 'Nozzle clog'
    assert bench['state'] == 'paused' and bench['pause_cause'] == 'error' and bench['reason'] == 'Nozzle clog'


def test_paused_reason_is_redacted_while_controlled_work_is_hidden(ha_client, review_db):
    client, issue = ha_client
    _job(review_db, 401, 'secret-bracket', 'printing', printer=9)
    _mark_controlled(review_db, 401)  # the caller is not in the audience
    review_db.commit()
    bench = _printers(_get(client, issue))[12]['bench']
    assert bench['pause_cause'] == 'error' and bench['reason'] == 'Details hidden (controlled work)'


def test_controlled_printer_work_is_named_controlled_and_details_withheld(ams, review_db):
    client, issue = ams
    _job(review_db, 401, 'secret-bracket', 'printing', printer=9)
    _mark_controlled(review_db, 401)  # printer 9 carries work this caller cannot name
    _job(review_db, 570, 'secret-visible', printer=9, model_id=12, ams_mapping='[0, 1]', duration_hours=1, quantity_on_bed=2)
    review_db.commit()
    response = client.get(URL, headers=issue(['read']))
    job = _printers(response.json())[9]['next_job']
    assert job['id'] == 570 and job['name'] == 'Controlled work'
    assert job['filaments'] is None and job['plate'] is None and job['ams_check'] is None
    assert job['est_minutes'] == 60
    import json
    assert 'secret-visible' not in response.text
    # Another, uncontrolled job in the fixture uses the same file, so look at this printer only.
    for leaked in ('#000000', 'PETG'):
        assert leaked not in json.dumps(_printers(response.json())[9])


def test_hidden_controlled_job_never_appears(ha_client, review_db):
    client, issue = ha_client
    before = _get(client, issue)['fleet']
    _job(review_db, 571, 'secret-queued', printer=10, duration_hours=4)
    _job(review_db, 572, 'secret-free', duration_hours=2)
    _mark_controlled(review_db, 571)
    review_db.execute(text("INSERT INTO controlled_jobs(job_id,audience_id,label,printer_tag,retention_days,marked_at,reason) "
                           "VALUES(572,1,'CUI','Secure',7,'2026-01-01T00:00:00+00:00','test')"))
    review_db.commit()
    response = client.get(URL, headers=issue(['read']))
    body = response.json()
    assert 'secret' not in response.text
    assert _printers(body)[10]['next_job'] is None and _printers(body)[10]['bench'] is None
    assert 572 not in {job['id'] for job in body['fleet']['next_unassigned']}
    for key in ('queue_length', 'queued_hours', 'queued_unestimated'):
        assert body['fleet'][key] == before[key]  # hidden work is not counted either


def test_audience_member_sees_the_controlled_job(ha_client, review_db):
    client, issue = ha_client
    _job(review_db, 573, 'member-part', printer=10)
    _mark_controlled(review_db, 573, member=True)
    review_db.commit()
    job = _printers(_get(client, issue))[10]['next_job']
    assert job['id'] == 573 and job['name'] != 'Controlled work'


def test_tenant_and_archived_education_jobs_are_not_surfaced(ha_client, review_db):
    client, issue = ha_client
    _job(review_db, 574, 'Foreign org', printer=10, charged_to_org_id=2)
    review_db.commit()
    body = _get(client, issue)
    assert _printers(body)[10]['next_job'] is None
    assert 13 in {job['id'] for job in body['fleet']['next_unassigned']}
    review_db.execute(text('UPDATE education_submissions SET archived_at=CURRENT_TIMESTAMP WHERE job_id=13'))
    review_db.commit()
    assert 13 not in {job['id'] for job in _get(client, issue)['fleet']['next_unassigned']}


def test_queued_hours_and_unestimated_jobs(ha_client, review_db):
    client, issue = ha_client
    before = _get(client, issue)['fleet']
    _job(review_db, 560, 'Two and a half', duration_hours=2.5)
    _job(review_db, 561, 'Half', 'submitted', printer=9, duration_hours=0.5)
    _job(review_db, 562, 'Zero estimate', 'scheduled', duration_hours=0)
    _job(review_db, 563, 'Finished', 'completed', duration_hours=9)
    review_db.commit()
    after = _get(client, issue)['fleet']
    assert after['queued_hours'] == pytest.approx(before['queued_hours'] + 3.0)
    assert after['queued_unestimated'] == before['queued_unestimated'] + 1
    assert after['queue_length'] == before['queue_length'] + 3


def test_day_start_is_local_midnight_in_the_server_zone():
    from modules.printers.integration_floor import day_start
    now = datetime(2026, 11, 12, 3, 30, tzinfo=timezone.utc)
    assert day_start(now, timezone(timedelta(hours=-5))) == datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc)
    assert day_start(now, timezone(timedelta(hours=13))) == datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc)
    assert day_start(now, timezone.utc) == datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)


def test_day_totals_start_at_local_midnight(ha_client, review_db, monkeypatch):
    from modules.printers import integration_floor
    client, issue = ha_client
    zone = timezone(timedelta(hours=-5))
    monkeypatch.setattr(integration_floor, 'server_zone', lambda: zone)
    start = integration_floor.day_start(datetime.now(timezone.utc), zone)
    minute = timedelta(minutes=1)
    rows = [(9, 'completed', start - minute), (9, 'completed', start + minute), (10, 'failed', start + 2 * minute),
            (10, 'failed', start - 2 * minute), (12, 'completed', start + 3 * minute),
            (71, 'completed', start + minute), (9, 'cancelled', start + minute)]  # 71 is another organization's printer
    for printer, status, ended in rows:
        review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at) "
                               "VALUES(:p,'part.gcode',:s,:a,:e)"),
                          {'p': printer, 's': status, 'a': _utc_text(ended - timedelta(hours=1)), 'e': _utc_text(ended)})
    review_db.commit()
    today = _get(client, issue)['fleet']['today']
    assert today == {'plates_done': 2, 'plates_failed': 1, 'since': start.isoformat().replace('+00:00', 'Z')}


def test_response_is_database_only_and_read_only(ams, review_db):
    client, issue = ams
    _job(review_db, 580, 'Bracket', printer=9, model_id=12, ams_mapping='[0, 1]', hold=1)
    _job(review_db, 581, 'Free', duration_hours=1)
    review_db.commit()
    headers = issue(['read'])
    assert client.get(URL, headers=headers).status_code == 200  # warm-up: first use of the token records its use
    writes = []
    engine = review_db.get_bind()

    def watch(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
            writes.append(statement)
    event.listen(engine, 'before_cursor_execute', watch)
    try:
        # The fixture makes any printer or MQTT call raise; a 200 means none was made.
        assert client.get(URL, headers=headers).status_code == 200
    finally:
        event.remove(engine, 'before_cursor_execute', watch)
    touched = ('jobs', 'printers', 'print_jobs', 'job_timeline_events', 'filament_slots', 'vision_detections')
    assert not [s for s in writes if any(f' {t} ' in s or f' {t}(' in s for t in touched)]


def test_maintenance_hold_reports_out_of_service_and_keeps_the_printer_listed(ha_client, review_db):
    import json
    client, issue = ha_client
    before = {p['id']: p for p in _get(client, issue)['printers']}
    assert all(p['out_of_service'] is False for p in before.values())
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('printer.maintenance.10',:v)"),
                      {'v': json.dumps({'state': 'hold', 'note': 'Heatbed fault, waiting on a part', 'set_by': 3,
                                        'set_by_name': 'admin', 'set_at': '2026-11-12T05:00:00+00:00'})})
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('printer.maintenance.12',:v)"),
                      {'v': json.dumps({'state': 'warn', 'note': 'Nozzle wearing', 'set_by': 3, 'set_by_name': 'admin', 'set_at': None})})
    review_db.commit()
    after = {p['id']: p for p in _get(client, issue)['printers']}
    assert set(after) == set(before)                                  # still listed, camera flag untouched
    assert after[10]['camera_available'] == before[10]['camera_available']
    assert after[10]['out_of_service'] is True and after[12]['out_of_service'] is False
    if before[10]['status'] != 'paused':
        assert after[10]['bench'] == {'state': 'out_of_service', 'reason': 'Heatbed fault, waiting on a part',
                                      'since': '2026-11-12T05:00:00+00:00', 'pause_cause': None}
