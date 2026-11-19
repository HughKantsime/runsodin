"""Creation, connection tests and conservative repair against the real printer schema."""
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text

from core.print_attempts import qmark_runner, sqlalchemy_runner
from tests.test_contracts.test_monitor_restart_reconciliation_postgres import pg_server, review_db as pg_review_db
from modules.printers.bambu_slots import full_ams_slots, repair_phantom_slots
from tests.test_contracts.test_ams_sync_acceptance import ams_client, own_ams_operator, sync_client, token_client
from tests.test_contracts.test_cert_A_harness import (
    broker_socket, cert_app, cert_client, fernet_env, load_frames, review_db as sqlite_review_db, wire_monitor as connected_monitor,
)


@pytest.fixture
def wire_monitor(connected_monitor, monkeypatch):
    # The socket's arrival stamp and the monitor's heartbeat use the same test clock.
    from modules.printers.adapters import bambu
    from modules.printers.monitors import mqtt_printer
    monkeypatch.setattr(bambu, 'time', mqtt_printer.time)
    return connected_monitor


both_databases = pytest.mark.parametrize('review_db', [
    'sqlite_review_db', pytest.param('pg_review_db', marks=pytest.mark.postgresql),
], indirect=True, ids=['sqlite', 'postgresql'])


@pytest.fixture
def review_db(request):
    # The monitor opens and commits independent connections, so use an isolated database per case.
    return request.getfixturevalue(getattr(request, 'param', 'sqlite_review_db'))


def h2d_packet():
    return next(payload for _, payload in load_frames('bambu-h2d-recovery')
                if payload.get('print', {}).get('ams', {}).get('ams'))


def single_ams_and_ht():
    packet = h2d_packet()
    root = packet['print']['ams']
    root['ams'] = [unit for unit in root['ams'] if unit['id'] != '1']
    root.update(ams_exist_bits='11', ams_exist_bits_raw='11')
    return packet


def slots(db, pid):
    return db.execute(text('SELECT slot_number FROM filament_slots WHERE printer_id=:pid ORDER BY slot_number'),
                      {'pid': pid}).scalars().all()


def seed_extra(db, pid, number):
    db.execute(text("UPDATE printers SET slot_count=:n WHERE id=:pid"), {'n': number, 'pid': pid})
    db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,loaded_at) "
                    "SELECT id,:n,'empty',created_at FROM printers WHERE id=:pid"),
               {'n': number, 'pid': pid})
    db.commit()


def count_changes(db, pid):
    rows = db.execute(text("SELECT details FROM audit_logs WHERE entity_id=:pid AND entity_type='printer' "
                           "AND action='printer.observed_config_changed'"), {'pid': pid})
    return [d for raw, in rows if (d := json.loads(raw) if isinstance(raw, str) else raw).get('field') == 'slot_count']


def partial_ams():
    packet = single_ams_and_ht()
    packet['print']['ams']['ams'][0]['tray'] = [{'id': '0'}, {'id': '1'}]
    return packet


def unit_without_trays():
    packet = h2d_packet()
    packet['print']['ams']['ams'][1]['tray'] = []
    return packet


def no_units():
    packet = h2d_packet()
    del packet['print']['ams']['ams']
    return packet


@pytest.mark.parametrize('standard,packet', [(4, single_ams_and_ht), (8, h2d_packet), (4, partial_ams),
                                             (8, unit_without_trays), (0, no_units)])
@pytest.mark.parametrize('v2', [False, True], ids=['legacy', 'v2'])
def test_connection_routes_count_standard_and_ht(monkeypatch, v2, standard, packet):
    from modules.printers import routes_crud
    from modules.printers.adapters import bambu
    from modules.printers.route_utils import TestConnectionRequest
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    from modules.system import routes_setup
    request = TestConnectionRequest(api_type='bambu', api_host='192.0.2.1', serial='test', access_code='test')
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: v2)
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    if v2:
        section = BambuPrintSection.model_validate(packet()['print'])
        view = SimpleNamespace(state='idle', bed_temp=0, nozzle_temp=0, printer_type='H2D')
        monkeypatch.setattr(session, 'read_status_once', lambda *a, **kw: session.StatusReadResult(True, view, section))
        setup_probe = routes_setup._test_bambu_v2
    else:
        printer = bambu.BambuPrinter(ip=request.api_host, serial='test', access_code='test')
        printer._parse_status(packet(), mqtt_retained=False)
        monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kw: SimpleNamespace(
            connect=lambda: True, get_status=printer.get_status, disconnect=lambda: None))
        setup_probe = routes_setup._test_bambu_legacy
    for response in (routes_crud.test_printer_connection(request, {}), setup_probe(request)):
        assert response['success']
        assert (response['ams_slots'], response['ams_ht_slots']) == (standard, 1 if standard else 0)


@pytest.mark.parametrize('route', ['crud', 'setup'])
def test_legacy_connection_counts_survive_temperature_delta(monkeypatch, route):
    from modules.printers import routes_crud
    from modules.printers.adapters import bambu
    from modules.printers.route_utils import TestConnectionRequest
    from modules.printers.telemetry import feature_flag
    from modules.system import routes_setup

    request = TestConnectionRequest(api_type='bambu', api_host='192.0.2.1', serial='test', access_code='test')
    printer = bambu.BambuPrinter(ip=request.api_host, serial='test', access_code='test')
    packet = h2d_packet()
    packet['print']['ams']['ams'].append(None)
    printer._parse_status(packet)
    printer._parse_status({'print': {'bed_temper': 42}})
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: False)
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kw: SimpleNamespace(
        connect=lambda: True, get_status=printer.get_status, disconnect=lambda: None))
    result = (routes_crud.test_printer_connection(request, {}) if route == 'crud'
              else routes_setup._test_bambu_legacy(request))
    assert (result['ams_slots'], result['ams_ht_slots']) == (8, 1)


@pytest.mark.parametrize('setup', [False, True], ids=['api', 'setup'])
@pytest.mark.parametrize('api_type,number', [('bambu', n) for n in (1, 4, 5, 8, 12, 16, 17)] + [('moonraker', 5)])
def test_creation_counts_and_model_bed(cert_app, review_db, setup, api_type, number):
    from modules.system.routes_setup import setup_create_printer, SetupPrinterRequest
    fields = dict(name='Fixture printer', api_type=api_type, model='H2D Pro', slot_count=number)
    invalid = api_type == 'bambu' and number in (5, 17)
    if setup:
        if invalid:
            with pytest.raises(HTTPException) as exc:
                setup_create_printer(SetupPrinterRequest(**fields), {'role': 'admin'}, review_db)
            assert exc.value.status_code == 400 and 'AMS HT' in exc.value.detail
            return
        result = setup_create_printer(SetupPrinterRequest(**fields), {'role': 'admin'}, review_db)
    else:
        response = cert_app.post('/printers', headers=cert_app.roles['admin'], json=fields)
        if invalid:
            assert response.status_code == 400 and 'AMS HT' in response.json()['detail']
            return
        assert response.status_code == 201, response.text
        result = response.json()
    assert slots(review_db, result['id']) == list(range(1, number + 1))
    assert review_db.execute(text('SELECT bed_x_mm,bed_y_mm FROM printers WHERE id=:id'),
                             {'id': result['id']}).one() == ((350, 320) if api_type == 'bambu' else (None, None))


@pytest.mark.parametrize('standard,packet', [(4, single_ams_and_ht), (8, h2d_packet)])
@both_databases
def test_monitor_repairs_once_and_keeps_real_trays(wire_monitor, review_db, standard, packet):
    _, sock, clock, _ = wire_monitor
    seed_extra(review_db, 301, standard + 1)
    for _ in range(2):
        sock.deliver(packet())
        clock[0] += 61
    assert slots(review_db, 301) == [*range(1, standard + 1), 17]
    assert review_db.execute(text('SELECT slot_count FROM printers WHERE id=301')).scalar_one() == standard
    assert count_changes(review_db, 301) == [{'field': 'slot_count', 'old': standard + 1, 'new': standard}]


@pytest.mark.parametrize('case', ['no-ht', 'delta', 'retained', 'mask-disagrees', 'no-mask', 'partial', 'equal',
                                  'above-range', 'non-bambu', 'sparse-units'])
def test_report_cannot_authorize_repair(wire_monitor, review_db, case):
    _, sock, _, _ = wire_monitor
    packet = single_ams_and_ht()
    root = packet['print']['ams']
    count = {'equal': 4, 'above-range': 6}.get(case, 5)
    seed_extra(review_db, 301, 5)
    review_db.execute(text("UPDATE printers SET slot_count=:n,api_type=:api WHERE id=301"),
                      {'n': count, 'api': 'moonraker' if case == 'non-bambu' else 'bambu'})
    review_db.commit()
    if case == 'no-ht':
        root.update(ams=root['ams'][:1], ams_exist_bits='1')
    elif case == 'delta':
        packet['print']['msg'] = 1
    elif case == 'mask-disagrees':
        root['ams_exist_bits'] = '13'
    elif case == 'no-mask':
        del root['ams_exist_bits']
    elif case == 'partial':
        root['ams'][0]['tray'].pop()
    elif case == 'sparse-units':
        root['ams'][0]['id'] = '1'
        root['ams_exist_bits'] = '12'
    sock.deliver(packet, retain=case == 'retained')
    assert 5 in slots(review_db, 301)
    assert review_db.execute(text('SELECT slot_count FROM printers WHERE id=301')).scalar_one() == count
    assert not count_changes(review_db, 301)


@pytest.mark.parametrize('v2,case', [(False, 'full'), (False, 'retained')] + [
    (True, case) for case in ('full', 'retained', 'delta', 'mask-disagrees', 'no-mask', 'partial', 'unknown-age', 'stale')
])
def test_manual_sync_requires_fresh_full_v2_section(sync_client, review_db, monkeypatch, v2, case):
    from modules.printers.adapters import bambu
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client, headers, _ = sync_client
    seed_extra(review_db, 199, 9)
    packet = h2d_packet()
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: v2)
    root = packet['print']['ams']
    retained = case == 'retained'
    if case == 'delta':
        packet['print']['msg'] = 1
    elif case == 'mask-disagrees':
        root['ams_exist_bits'] = '11'
    elif case == 'no-mask':
        del root['ams_exist_bits']
    elif case == 'partial':
        root['ams'][1]['tray'].pop()
    received = None if case == 'unknown-age' else time.time() - (120 if case == 'stale' else 0)
    if v2:
        response = session.StatusReadResult(True, section=BambuPrintSection.model_validate(packet['print']),
                                            mqtt_retained=retained, received_at=received)
        monkeypatch.setattr(session, 'read_status_once', lambda *a, **kw: response)
    else:
        printer = bambu.BambuPrinter(ip='192.0.2.1', serial='test', access_code='test')
        printer._parse_status(packet, mqtt_retained=retained)
        monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kw: SimpleNamespace(
            connect=lambda: True, get_status=printer.get_status, disconnect=lambda: None))
        monkeypatch.setattr(time, 'sleep', lambda _: None)
    result = client.post('/printers/199/sync-ams', headers=headers)
    assert result.status_code == 200, result.text
    repaired = v2 and case == 'full'
    assert 17 in slots(review_db, 199) and (9 not in slots(review_db, 199)) == repaired
    assert count_changes(review_db, 199) == ([{'field': 'slot_count', 'old': 9, 'new': 8}] if repaired else [])


def test_removed_stored_mapping_names_slot_before_upload(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from tests.test_contracts.test_multi_source_ams_dispatch import setup_multi, transport
    _, path, _ = setup_multi(review_db, tmp_path, monkeypatch, mapping=[4], ids=(0,), materials=('PLA',))
    review_db.execute(text("UPDATE printers SET slot_count=5 WHERE id=59"))
    review_db.execute(text("UPDATE filament_slots SET filament_type='empty' WHERE printer_id=59 AND slot_number=5"))
    review_db.execute(text("UPDATE filament_slots SET loaded_at=(SELECT created_at FROM printers WHERE id=59) "
                           "WHERE printer_id=59 AND slot_number=5"))
    repair_phantom_slots(sqlalchemy_runner(review_db), 59, full_ams_slots(single_ams_and_ht()['print']))
    review_db.commit()
    assert 5 not in slots(review_db, 59)
    events = []
    sent = transport(monkeypatch, path, 'legacy', events)
    ok, message = dispatch.dispatch_job(59, 53)
    assert not ok and 'slot 5' in message
    assert sent == events == []


@pytest.mark.parametrize('before,fields,expected', [
    ((None, None), [], (350, 320)), ((320, 320), ['name'], (350, 320)),
    ((None, None), ['bed_y_mm'], (None, None)), ((None, None), ['name'], (350, 320)),
    ((320, 320), ['bed_x_mm'], (320, 320)), ((320, 320), ['bed_y_mm'], (320, 320)),
    ((333, 300), [], (333, 300)), ((310, None), [], (310, None)),
])
def test_h2d_repairs_only_the_old_odin_bed(wire_monitor, review_db, before, fields, expected):
    _, sock, _, _ = wire_monitor
    review_db.execute(text('UPDATE printers SET bed_x_mm=:x,bed_y_mm=:y WHERE id=301'),
                      dict(x=before[0], y=before[1]))
    if fields:
        review_db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details) "
                               "VALUES('printer.updated','printer',301,:d)"), {'d': json.dumps({'fields': fields, 'fields_mean': 'changed'})})
    review_db.commit()
    info = next(payload for _, payload in load_frames('bambu-h2d-recovery')
                if any(m.get('name') == 'ota' for m in payload.get('info', {}).get('module', [])))
    sock.deliver(info)
    sock.deliver(h2d_packet())
    assert review_db.execute(text('SELECT model,bed_x_mm,bed_y_mm FROM printers WHERE id=301')).one() == ('H2D', *expected)


    changes = [json.loads(d) for d in review_db.execute(text("SELECT details FROM audit_logs WHERE entity_id=301 "
               "AND action='printer.observed_config_changed'")).scalars() if json.loads(d).get('field') == 'bed_size']
    old = list(before) if before != (None, None) else None
    assert changes == ([{'field': 'bed_size', 'old': old, 'new': list(expected)}] if before != expected else [])


@pytest.mark.parametrize('details', ['{', '[]', '{}', '{"fields":null}', '{"fields":[1]}', '{"fields":["name","bed_x_mm","bed_y_mm"]}'])
def test_unreadable_audit_protects_bed_but_valid_legacy_fields_allow_repair(wire_monitor, review_db, details):
    _, sock, _, _ = wire_monitor
    review_db.execute(text('UPDATE printers SET bed_x_mm=320,bed_y_mm=320 WHERE id=301'))
    review_db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details) "
                           "VALUES('printer.updated','printer',301,:details)"), {'details': details})
    review_db.commit()
    info = next(payload for _, payload in load_frames('bambu-h2d-recovery')
                if any(m.get('name') == 'ota' for m in payload.get('info', {}).get('module', [])))
    sock.deliver(info)
    sock.deliver(h2d_packet())
    assert review_db.execute(text('SELECT machine_type,bed_x_mm,bed_y_mm FROM printers WHERE id=301')).one() == (
        'H2D', 350 if 'bed_x_mm' in details else 320, 320)
    assert bool(review_db.execute(text("SELECT 1 FROM audit_logs WHERE entity_id=301 "
        "AND action='printer.observed_config_changed' AND details->>'field'='bed_size'")).first()) == ('bed_x_mm' in details)


def test_printer_edit_audits_only_changed_fields(cert_app, review_db):
    from tests.test_contracts.test_cert_A_harness import ACCESS, SERIAL
    review_db.execute(text('UPDATE printers SET bed_x_mm=320,bed_y_mm=320 WHERE id=301'))
    review_db.commit()
    form = {'name': 'Renamed', 'bed_x_mm': 320, 'bed_y_mm': 320, 'api_key': f'{SERIAL}|{ACCESS}'}
    for bed_x, expected in [(320, ['name']), (350, ['bed_x_mm']), (350, [])]:
        form['bed_x_mm'] = bed_x
        response = cert_app.patch('/printers/301', json=form, headers=cert_app.roles['admin'])
        assert response.status_code == 200, response.text
        audit = review_db.execute(text("SELECT details FROM audit_logs WHERE entity_id=301 "
            "AND action='printer.updated' ORDER BY id DESC LIMIT 1")).scalar_one()
        assert json.loads(audit) == {'fields': expected, 'fields_mean': 'changed'}


def test_printer_update_service_distinguishes_rotated_and_reencrypted_values(fernet_env):
    from types import SimpleNamespace
    from core.crypto import encrypt
    from modules.printers.services import apply_printer_updates

    printer = SimpleNamespace(api_key=encrypt('old'), camera_url=encrypt('camera'))
    updates = {'api_key': encrypt('new'), 'camera_url': encrypt('camera')}
    assert printer.camera_url != updates['camera_url']
    assert apply_printer_updates(printer, updates) == ['api_key']
    assert vars(printer) == updates


@pytest.mark.parametrize('history', [
    'material', 'color', 'color-hex', 'assigned', 'spoolman', 'confirmed', 'feed-flag', 'feed-note',
    'later-load', 'observation', 'location', 'confirmation', 'reported', 'reported-missing', 'slot-audit', 'spool-audit', 'repair-history',
])
@both_databases
def test_any_used_row_protects_whole_range(review_db, history, monkeypatch, caplog):
    # Slot 5 is eligible on its own. Finding history on 6 must also preserve 5.
    monkeypatch.setattr('modules.printers.bambu_slots._repair_warning_claims', {})
    review_db.execute(text("INSERT INTO printers(id,name,api_type) VALUES(301,'Fixture','bambu')"))
    seed_extra(review_db, 301, 5)
    seed_extra(review_db, 301, 6)
    db = review_db
    db.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(980,'Test','Fixture')"))
    db.execute(text("INSERT INTO spools(id,filament_id,qr_code,org_id) VALUES(980,980,'fixture',1)"))
    slot = db.execute(text('SELECT id FROM filament_slots WHERE printer_id=301 AND slot_number=6')).scalar_one()
    edits = {
        'material': "filament_type='PLA'", 'color': "color='Blue'", 'color-hex': "color_hex='0000ff'",
        'assigned': 'assigned_spool_id=980', 'spoolman': 'spoolman_spool_id=1', 'confirmed': 'spool_confirmed=TRUE',
        'feed-flag': 'do_not_feed=TRUE', 'feed-note': "do_not_feed_note='Keep'", 'observation': "tray_observation='{}'",
        'later-load': "loaded_at='2099-01-01 00:00:00'",
    }
    if history in edits:
        db.execute(text('UPDATE filament_slots SET ' + edits[history] + ' WHERE id=:id'), {'id': slot})
    elif history == 'location':
        db.execute(text('UPDATE spools SET location_printer_id=301, location_slot=6 WHERE id=980'))
    elif history == 'confirmation':
        db.execute(text("INSERT INTO tray_confirmations(state_id,slot_id,decision,spool_id) "
                        "VALUES(:state,:slot,'keep',980)"), {'state': 'a' * 32, 'slot': slot})
    elif history in ('reported', 'reported-missing'):
        if history == 'reported-missing':
            db.execute(text('DELETE FROM filament_slots WHERE id=:id'), {'id': slot})
        db.execute(text("INSERT INTO ams_read_generations(generation_id,printer_id,configuration_key,runtime_incarnation,started_at,state) "
                        "VALUES(:gen,301,:key,:runtime,CURRENT_TIMESTAMP,'acquiring')"),
                   {'gen': '1' * 32, 'key': '2' * 64, 'runtime': '3' * 32})
        seq = db.execute(text("INSERT INTO ams_report_packets(packet_id,generation_id,received_at,receipt_provenance) "
                              "VALUES(:pid,:gen,CURRENT_TIMESTAMP,'db-clock-estimate-v1') RETURNING packet_sequence"),
                         {'pid': '4' * 32, 'gen': '1' * 32}).scalar_one()
        db.execute(text("INSERT INTO ams_report_trays(packet_sequence,slot_number,unit_id,tray_id,material_state) "
                        "VALUES(:seq,6,1,1,'unknown')"), {'seq': seq})
    elif history == 'slot-audit':
        db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id) VALUES('filament_slot.feed_policy','filament_slot',:id)"),
                   {'id': slot})
    elif history == 'spool-audit':
        db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details) VALUES('scan_assign','spool',980,:d)"),
                   {'d': json.dumps({'printer_id': 301, 'slot': 6})})
    else:
        db.execute(text("INSERT INTO inventory_repairs(event_id,spool_id,action,before_json,after_json) "
                        "VALUES('old-repair',980,'stale-location.clear','[301, 6]','[null, null]')"))
    repair_phantom_slots(sqlalchemy_runner(db), 301, {1, 2, 3, 4, 17, 18})
    db.commit()
    assert slots(db, 301) == ([5] if history == 'reported-missing' else [5, 6])
    assert db.execute(text('SELECT slot_count FROM printers WHERE id=301')).scalar_one() == 6
    assert not count_changes(db, 301)
    assert 'Could not repair extra AMS slots' not in caplog.text


def test_deleted_printer_is_a_repair_noop(review_db):
    repair_phantom_slots(sqlalchemy_runner(review_db), 987654, {1, 2, 3, 4, 17})
    assert not count_changes(review_db, 987654)


def test_failed_repair_warns_once_per_printer_and_retries(review_db, caplog, monkeypatch):
    db = review_db
    monkeypatch.setattr('modules.printers.bambu_slots._repair_warning_claims', {})
    ids = (9001, 9002)
    for pid in ids:
        db.execute(text("INSERT INTO printers(id,name,api_type) VALUES(:pid,:name,'bambu')"),
                   {'pid': pid, 'name': f'Printer {pid}'})
        seed_extra(db, pid, 5)
    db.execute(text("INSERT INTO audit_logs(action,entity_type,details) VALUES('scan_assign','spool','{')"))
    for pid in (*ids, *ids):
        repair_phantom_slots(sqlalchemy_runner(db), pid, {1, 2, 3, 4, 17})
        assert slots(db, pid) == [5]
        assert not count_changes(db, pid)
    assert [r.getMessage() for r in caplog.records if r.name == 'modules.printers.bambu_slots'] == [
        f'Could not repair extra AMS slots on printer {pid}' for pid in ids]
    db.execute(text("DELETE FROM audit_logs WHERE action='scan_assign'"))
    for pid in ids:
        repair_phantom_slots(sqlalchemy_runner(db), pid, {1, 2, 3, 4, 17})
        assert slots(db, pid) == []
        assert count_changes(db, pid) == [{'field': 'slot_count', 'old': 5, 'new': 4}]


@pytest.mark.parametrize('next_report,delay,repairs', [
    ({}, 11, True), ({}, 65, False), ({'ams': {'ams_exist_bits': '1'}}, 11, False),
])
def test_monitor_uses_only_fresh_matching_held_report(wire_monitor, review_db, next_report, delay, repairs):
    _, sock, clock, _ = wire_monitor
    seed_extra(review_db, 301, 5)
    sock.deliver({'print': {'gcode_state': 'IDLE'}})
    clock[0] += 1
    sock.deliver(single_ams_and_ht())
    assert 5 in slots(review_db, 301)
    clock[0] += delay
    sock.deliver({'print': {'gcode_state': 'IDLE', **next_report}})
    assert 17 in slots(review_db, 301)
    assert (5 not in slots(review_db, 301)) == repairs


def test_v2_retained_flag_and_arrival_belong_to_returned_section(monkeypatch):
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    from modules.printers.telemetry.events import BambuReportEvent, ConnectionEvent
    fresh = BambuPrintSection.model_validate(h2d_packet()['print'])
    retained = BambuPrintSection.model_validate(single_ams_and_ht()['print'])

    def adapter(config, emitter):
        def start():
            emitter(ConnectionEvent('fixture', 10, 'connected'))
            emitter(BambuReportEvent('fixture', 11, fresh, False))
            emitter(BambuReportEvent('fixture', 12, retained, True))
        return SimpleNamespace(start=start, status=lambda: None, stop=lambda: None)

    monkeypatch.setattr(session, 'BambuTelemetryAdapter', adapter)
    result = session.read_status_once(object())
    assert result.section is retained and result.mqtt_retained is True and result.received_at == 12


@pytest.mark.parametrize('audit_fails', [False, True])
def test_h2d_pro_bed_audit_is_atomic_and_independent_of_machine_type(wire_monitor, review_db, audit_fails):
    _, sock, _, _ = wire_monitor
    review_db.execute(text('UPDATE printers SET bed_x_mm=320,bed_y_mm=320 WHERE id=301'))
    if audit_fails:
        review_db.execute(text("""CREATE TRIGGER fail_bed_audit BEFORE INSERT ON audit_logs
            WHEN NEW.action='printer.observed_config_changed' AND NEW.details->>'field'='bed_size'
            BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"""))
    review_db.commit()
    sock.deliver({'print': {'printer_type': 'O1E', 'gcode_state': 'IDLE'}})
    assert review_db.execute(text('SELECT machine_type,bed_x_mm,bed_y_mm FROM printers WHERE id=301')).one() == (
        'H2D Pro', 320 if audit_fails else 350, 320)


def test_repair_audit_failure_preserves_slots_and_regular_sync(wire_monitor, review_db):
    _, sock, _, _ = wire_monitor
    seed_extra(review_db, 301, 5)
    review_db.execute(text("""CREATE TRIGGER fail_count_audit BEFORE INSERT ON audit_logs
        WHEN NEW.action='printer.observed_config_changed' AND NEW.details->>'field'='slot_count'
        BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"""))
    review_db.commit()
    sock.deliver(single_ams_and_ht())
    assert slots(review_db, 301) == [1, 2, 3, 4, 5, 17]
    assert review_db.execute(text('SELECT slot_count FROM printers WHERE id=301')).scalar_one() == 5
    assert not count_changes(review_db, 301)


@pytest.mark.parametrize('uri', ['/setup/printer', '/printers'])
@pytest.mark.parametrize('invalid', [None, 'four', '4', 0, -4])
def test_invalid_bambu_count_is_a_plain_http_error(cert_app, uri, invalid):
    from core.dependencies import get_current_user
    from modules.system.routes_setup import router
    cert_app.app.include_router(router)
    cert_app.app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'admin', 'group_id': None}
    response = cert_app.post(uri, json={'name': 'Fixture', 'api_type': 'bambu', 'slot_count': invalid},
                             headers=cert_app.roles['admin'])
    assert response.status_code == 400
    assert '4 slots per AMS' in response.json()['detail']


def test_valid_eight_survives_four_ht_units(cert_app, review_db):
    for number in range(5, 9):
        seed_extra(review_db, 301, number)
    repair_phantom_slots(sqlalchemy_runner(review_db), 301, {1, 2, 3, 4, 17, 18, 19, 20})
    assert slots(review_db, 301) == [5, 6, 7, 8]
    assert review_db.execute(text('SELECT slot_count FROM printers WHERE id=301')).scalar_one() == 8
    assert not count_changes(review_db, 301)


@pytest.mark.parametrize('action,before,after', [
    ('admin.resolve', '{}', '{}'),
    ('stale-location.clear', '{', '[null,null]'),
    ('telemetry.relocate', '{"printer_id":301,"slot":6}', '{'),
])
def test_unreadable_inventory_repair_keeps_count_quietly(cert_app, review_db, caplog, action, before, after):
    seed_extra(review_db, 301, 5)
    review_db.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(980,'Test','Fixture')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id) VALUES(980,980)"))
    review_db.execute(text("INSERT INTO inventory_repairs(event_id,spool_id,action,before_json,after_json) "
                           "VALUES('old',980,:action,:before,:after)"), dict(action=action, before=before, after=after))
    for _ in range(2):
        repair_phantom_slots(sqlalchemy_runner(review_db), 301, {1, 2, 3, 4, 17})
    assert slots(review_db, 301) == [5]
    assert review_db.execute(text('SELECT slot_count FROM printers WHERE id=301')).scalar_one() == 5
    assert not count_changes(review_db, 301)
    assert 'Could not repair' not in caplog.text


@pytest.mark.parametrize('reset', ['reconnect', 'new-connection', 'unknown-age'])
def test_monitor_drops_unusable_repair_evidence(wire_monitor, review_db, reset):
    monitor, sock, clock, _ = wire_monitor
    seed_extra(review_db, 301, 5)
    sock.deliver({'print': {'gcode_state': 'IDLE'}})
    clock[0] += 1
    if reset == 'unknown-age':
        monitor._bambu._parse_status(single_ams_and_ht(), mqtt_retained=False)
    else:
        sock.deliver(single_ams_and_ht())
        assert monitor._phantom_slot_evidence is not None
        if reset == 'reconnect':
            sock.on_connect(sock, None, {}, 0)
        else:
            monitor._begin_connection()
    assert monitor._phantom_slot_evidence is None
    clock[0] += 12
    sock.deliver({'print': {'gcode_state': 'IDLE'}})
    assert 5 in slots(review_db, 301)
    assert not count_changes(review_db, 301)


def test_machine_type_survives_bed_savepoint_failure(wire_monitor, review_db, monkeypatch):
    from core.db_utils import _ConnectionAdapter
    execute = _ConnectionAdapter.execute
    failed = []

    def refuse_savepoint(conn, sql, parameters=None):
        if sql == 'SAVEPOINT observed_bed':
            failed.append(sql)
            raise RuntimeError('Fixture savepoint failure')
        return execute(conn, sql, parameters)

    monkeypatch.setattr(_ConnectionAdapter, 'execute', refuse_savepoint)
    _, sock, _, _ = wire_monitor
    sock.deliver({'print': {'printer_type': 'O1E', 'gcode_state': 'IDLE'}})
    assert failed == ['SAVEPOINT observed_bed']
    assert review_db.execute(text('SELECT machine_type,bed_x_mm,bed_y_mm FROM printers WHERE id=301')).one() == (
        'H2D Pro', None, None)


@pytest.mark.parametrize('spool_audit', [False, True])
@both_databases
def test_created_slot_repair_respects_database_stamps_and_spool_audit(review_db, spool_audit):
    from core.db_compat import as_utc_datetime
    from core.db_utils import _ConnectionAdapter
    from modules.system.routes_setup import setup_create_printer, SetupPrinterRequest

    db = review_db
    engine = db.get_bind()
    sqlite = engine.dialect.name == 'sqlite'

    def cross_second(conn, cursor, statement, params, context, many):
        if statement.startswith('INSERT INTO filament_slots') and not getattr(context, 'delayed', False):
            context.delayed = True
            time.sleep(1.05)

    if sqlite:
        event.listen(engine, 'before_cursor_execute', cross_second)
    try:
        # An unconfigured five-slot printer uses the real creation defaults. Identify it
        # as Bambu afterwards to reproduce the state predating Bambu count validation.
        printer = setup_create_printer(SetupPrinterRequest(name='Fixture', slot_count=5), {'role': 'admin'}, db)
    finally:
        if sqlite:
            event.remove(engine, 'before_cursor_execute', cross_second)
    pid = printer['id']
    stamps = db.execute(text('SELECT p.created_at,s.loaded_at FROM printers p JOIN filament_slots s '
                             'ON p.id=s.printer_id WHERE p.id=:p AND s.slot_number=5'), {'p': pid}).one()
    age = (as_utc_datetime(stamps[1]) - as_utc_datetime(stamps[0])).total_seconds()
    assert (1 if sqlite else 0) <= age <= 5
    db.execute(text("UPDATE printers SET api_type='bambu' WHERE id=:p"), {'p': pid})
    db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details) VALUES('scan_assign','spool',1,:d)"),
               {'d': json.dumps({'printer_id': pid, 'slot': 5 if spool_audit else 4})})
    conn = _ConnectionAdapter(db.connection().connection, not sqlite)
    repair_phantom_slots(qmark_runner(conn), pid, {1, 2, 3, 4, 17})
    assert slots(db, pid) == list(range(1, 6 if spool_audit else 5))
    assert count_changes(db, pid) == ([] if spool_audit else [{'field': 'slot_count', 'old': 5, 'new': 4}])


@pytest.mark.parametrize('fields', [['name'], ['bed_x_mm']])
@both_databases
def test_h2d_bed_respects_changed_fields_on_both_databases(review_db, fields):
    from core.db_utils import _ConnectionAdapter
    from modules.printers.monitors.mqtt_status import observe_bambu_bed
    db = review_db
    db.execute(text("INSERT INTO printers(id,name,bed_x_mm,bed_y_mm) VALUES(9000,'Fixture',320,320)"))
    db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details) "
                    "VALUES('printer.updated','printer',9000,:d)"),
               {'d': json.dumps({'fields_mean': 'changed', 'fields': fields})})
    conn = _ConnectionAdapter(db.connection().connection, db.get_bind().dialect.name == 'postgresql')
    observe_bambu_bed(conn, 9000, 'H2D', (350, 320))
    changed = fields == ['name']
    assert db.execute(text('SELECT bed_x_mm,bed_y_mm FROM printers WHERE id=9000')).one() == (
        350 if changed else 320, 320)
    assert db.execute(text("SELECT count(*) FROM audit_logs WHERE entity_id=9000 "
                           "AND action='printer.observed_config_changed'")).scalar_one() == int(changed)


@pytest.mark.parametrize('stamp,repaired', [
    (None, True), ('2026-11-19 12:00:05.250000', True),
    ('2026-11-19 12:00:05.250001', False), ('2026-11-19 12:00:00.249999', False),
])
@both_databases
def test_creation_stamp_window(review_db, stamp, repaired):
    review_db.execute(text("INSERT INTO printers(id,name,api_type,slot_count,created_at) "
                         "VALUES(9000,'Fixture','bambu',5,'2026-11-19 12:00:00.250000')"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,loaded_at) "
                         "VALUES(9000,5,'empty',:stamp)"), {'stamp': stamp})
    repair_phantom_slots(sqlalchemy_runner(review_db), 9000, {1, 2, 3, 4, 17})
    assert (slots(review_db, 9000) == []) == repaired
    assert review_db.execute(text('SELECT slot_count FROM printers WHERE id=9000')).scalar_one() == (4 if repaired else 5)


def test_api_stores_suggested_prusa_bed(cert_app, review_db):
    response = cert_app.post('/printers', headers=cert_app.roles['admin'], json={
        'name': 'workshop-mk4', 'api_type': 'prusalink', 'model': 'Prusa MK4', 'bed_x_mm': 250, 'bed_y_mm': 210,
    })
    assert response.status_code == 201, response.text
    assert review_db.execute(text('SELECT bed_x_mm,bed_y_mm FROM printers WHERE id=:id'),
                             {'id': response.json()['id']}).one() == (250, 210)
