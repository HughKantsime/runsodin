"""Late Bambu tray evidence, using owned SQLite/PostgreSQL and real accounting."""
import json
import logging
from threading import Event, RLock
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_spool_rfid_identity import identity_engine, postgres_server, inventory, seed, rows
from tests.test_contracts.test_tray_identity import print_case, USER

KEY = 'quota_attempt:filament:job:87:print:129:observed_trays'


@pytest.fixture(autouse=True)
def monitor_database(inventory, monkeypatch):
    """Keep production daemon connections and SQL dialect on the owned engine."""
    from core import db, db_compat, db_utils
    monkeypatch.setattr(db, 'engine', inventory)
    monkeypatch.setattr(db_utils, 'engine', inventory)
    # Production derives these together from DATABASE_URL at process startup.
    # This suite switches dialects in-process, so replacing only the connection
    # leaves sql.now()/now_offset()/returning_id() using the startup dialect.
    for module in (db, db_compat):
        monkeypatch.setattr(module, 'IS_SQLITE', inventory.dialect.name == 'sqlite')
        monkeypatch.setattr(module, 'IS_POSTGRES', inventory.dialect.name == 'postgresql')


def stored_trays(engine):
    """The persisted tray list; PostgreSQL returns system_config JSON already decoded."""
    found = rows(engine, "SELECT value FROM system_config WHERE key='" + KEY + "'")
    return [value if isinstance(value, list) else json.loads(value) for (value,) in found]


def setup_slots(engine, bound=True, pending=False):
    from core.tray_identity import advance
    seed(engine, 38, sealed=False)
    seed(engine, 56, sealed=False)
    seed(engine, 57, sealed=False)
    state, _ = advance({}, dict(tray_type='PETG', tray_color='FFFFFF'))
    if pending:
        state, _ = advance(state, dict(tray_type='PETG', tray_color='898989'))
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='A1',machine_type='A1' WHERE id=1"))
        c.execute(text('INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id,tray_observation) VALUES(1,3,38,NULL),(1,4,:spool,:state)'),
                  dict(spool=56 if bound else None, state=json.dumps(state)))
        c.execute(text('UPDATE spools SET location_printer_id=1,location_slot=3 WHERE id=38'))
    return state


def telemetry(engine):
    """Real status ingestion, with transport, clock and unrelated I/O excluded."""
    from core.db_live import live_get_db
    from modules.printers.monitors.mqtt_status import handle_status
    monitor = SimpleNamespace(
        _lock=RLock(), _connection_retired=False, _connection_generation=1,
        _state={}, _current_job_id=129, _pending_running_state=None,
        _pending_hms_terminal=None, _status_received=Event(), _last_gcode_state='RUNNING',
        _transport_offline=False, _last_heartbeat=100, _last_progress_update=100, _last_spool_check=100,
        _camera_discovered=True, printer_id=1, name='A1 fixture',
        _on_state_change=lambda *args: None,
    )
    runtime = SimpleNamespace(
        get_db=live_get_db, time=SimpleNamespace(time=lambda: 100), log=logging.getLogger(__name__),
        finite_nozzle_measurement=lambda _: None, h2d_extruder_temperatures=lambda _: {},
        parse_bambu_speed=lambda _: {}, json=json,
    )
    def send(tray=None, state='RUNNING', **fields):
        raw = dict(gcode_state=state, **fields)
        if tray is not None:
            raw['ams'] = {'tray_now': tray}
        handle_status(monitor, SimpleNamespace(raw_data={'print':raw}), 1, runtime)
    send.monitor, send.runtime = monitor, runtime
    return send


def observe(engine, values):
    send = telemetry(engine)
    send(255)
    for value in values:
        send(value)
    return send


@pytest.mark.parametrize('bound', [False, True])
def test_a1_start_255_then_only_tray_three_uses_start_slot_four(inventory, bound):
    setup_slots(inventory, bound)
    def during():
        # A new binding after START must never supply the fallback's identity.
        with inventory.begin() as c:
            c.execute(text('UPDATE filament_slots SET assigned_spool_id=57 WHERE slot_number=4'))
        observe(inventory, [3, '3', 255, 254, 3])
    outcome, reason, _ = print_case(inventory, target=None, after_capture=during)
    assert (outcome, reason) == (('accounted', None) if bound else ('unresolved', 'no_bound_spool'))
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools WHERE id=38') == [(1000,)]
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools WHERE id=57') == [(1000,)]
    assert rows(inventory, 'SELECT spool_id,weight_used_g FROM spool_usage') == ([(56, 165.89)] if bound else [])
    entry = json.loads(rows(inventory, 'SELECT consumption_json FROM print_archives')[0][0])[0]
    assert entry['spool_id'] == (56 if bound else None)
    if not bound:
        assert entry['slot_number'] == 4 and entry['reason'] == 'no_bound_spool'
    assert stored_trays(inventory) == [[3]]


@pytest.mark.parametrize('values,reason', [([3, 2, 3], 'multiple_trays_observed'),
    ([255, 254, -1, 16, True, False, 3.5, '3.5', 'bad'], 'missing_material_mapping')])
def test_ambiguous_or_missing_tray_stays_unattributed(inventory, values, reason):
    setup_slots(inventory)
    outcome, actual, _ = print_case(inventory, target=None,
                                   after_capture=lambda: observe(inventory, values))
    assert (outcome, actual) == ('unresolved', reason)
    assert rows(inventory, 'SELECT count(*) FROM spool_usage') == [(0,)]
    assert json.loads(rows(inventory, 'SELECT consumption_json FROM print_archives')[0][0]) == [
        dict(spool_id=None, grams_used=165.89, reason=reason)]


@pytest.mark.parametrize('confirm_before', [False, True])
def test_observed_pending_slot_held_until_same_confirmation(inventory, confirm_before):
    from modules.inventory.routes.filament_slots import confirm_tray_identity, ConfirmTrayIdentity
    state = setup_slots(inventory, pending=True)
    def confirm():
        with Session(inventory) as db:
            return confirm_tray_identity(1, 4, ConfirmTrayIdentity(observed_state=state['id'], decision='same'),
                                         current_user=USER, db=db)
    def during():
        observe(inventory, [3])
        if confirm_before:
            confirm()
    outcome, reason, entries = print_case(inventory, target=None, after_capture=during)
    if not confirm_before:
        assert (outcome, reason) == ('unresolved', 'spool_change_pending')
        assert entries[0]['identity_state'] == state['hold_id']
        assert rows(inventory, 'SELECT count(*) FROM spool_usage') == [(0,)]
        confirm()
    else:
        assert (outcome, reason) == ('accounted', None)
    assert rows(inventory, 'SELECT spool_id,weight_used_g FROM spool_usage') == [(56, 165.89)]
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools WHERE id=38') == [(1000,)]
    assert confirm()['replayed']


def test_observations_are_distinct_durable_transactional_and_cleaned(inventory):
    from core.print_attempts import clear_job_attempts, sqlalchemy_runner
    from modules.archives.attempt_accounting import record_attempt_tray
    setup_slots(inventory)
    def during():
        with pytest.raises(RuntimeError, match='rollback'):
            with inventory.begin() as c:
                assert record_attempt_tray(c, 129, 1, 2)
                raise RuntimeError('rollback')
        assert rows(inventory, "SELECT value FROM system_config WHERE key='" + KEY + "'") == []
        observe(inventory, [0, 3, 15, 3])
        # A recreated monitor rereads the durable set; duplicates do no writes.
        writes = []
        def statement(conn, cursor, sql, parameters, context, executemany):
            if sql.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')):
                writes.append(sql)
        event.listen(inventory, 'before_cursor_execute', statement)
        try:
            with inventory.begin() as c:
                assert not record_attempt_tray(c, 129, 1, 3)
                assert not record_attempt_tray(c, 129, 2, 4)
                assert not record_attempt_tray(c, 129, 1, 255)
        finally:
            event.remove(inventory, 'before_cursor_execute', statement)
        assert writes == []
        assert stored_trays(inventory) == [[0, 3, 15]]
    print_case(inventory, target=None, after_capture=during)
    with inventory.begin() as c:
        c.execute(text("UPDATE print_jobs SET status='completed' WHERE id=129"))
        assert not record_attempt_tray(c, 129, 1, 4)
        clear_job_attempts(sqlalchemy_runner(c), 87)
    assert rows(inventory, "SELECT key FROM system_config WHERE key LIKE '%observed_trays'") == []


def test_transient_write_failure_retries_before_terminal(inventory, monkeypatch):
    from modules.archives import attempt_accounting
    setup_slots(inventory)
    original = attempt_accounting.record_attempt_tray
    def during():
        send = observe(inventory, [3])
        transitions = []
        send.monitor._on_state_change = lambda *args: transitions.append(args)
        def fail(*args):
            raise RuntimeError('synthetic write failure')
        monkeypatch.setattr(attempt_accounting, 'record_attempt_tray', fail)
        send(2)
        assert send.monitor._pending_attempt_trays == {2}
        monkeypatch.setattr(attempt_accounting, 'record_attempt_tray', original)
        send(state='PAUSE')
        assert stored_trays(inventory) == [[2, 3]]
        assert send.monitor._pending_attempt_trays == set()
        transitions.clear()
        send(state='FINISH')
        assert transitions == [('PAUSE', 'FINISH')]
    assert print_case(inventory, target=None, after_capture=during)[:2] == ('unresolved', 'multiple_trays_observed')


@pytest.mark.parametrize('materials,expected', [
    (None, ('accounted', None)),
    ('[]', ('accounted', None)),
    ('[{"used_grams":80},{"used_grams":85.89}]', ('unresolved', 'missing_material_mapping')),
    ('[{"used_grams":-1}]', ('unresolved', 'invalid_material_quantity')),
    ('[{"used_grams":10}]', ('unresolved', 'conflicting_material_quantities')),
    ('{}', ('unresolved', 'invalid_material_metadata')),
])
def test_fallback_eligibility_is_frozen_from_file(inventory, materials, expected):
    setup_slots(inventory)
    def during():
        with inventory.begin() as c:
            c.execute(text("UPDATE print_files SET filaments_json='[]',filament_weight_grams=900 WHERE id=8"))
        observe(inventory, [3])
    assert print_case(inventory, target=None, after_capture=during, materials_json=materials)[:2] == expected
    assert rows(inventory, 'SELECT weight_used_g FROM spool_usage') == ([(165.89,)] if expected[0] == 'accounted' else [])


def test_actual_a1_fixture_start_sentinel_then_slot_four(inventory):
    """Replay the fixture's sparse tray/state sequence, moving its tray 0 to 3."""
    from pathlib import Path
    setup_slots(inventory)
    fixture = Path(__file__).parents[1] / 'fixtures/telemetry/bambu-a1-happy-path.jsonl'
    state, tray = None, None
    sequence = []
    for line in fixture.read_text().splitlines():
        record = json.loads(line)
        if record.get('direction') != 'recv':
            continue
        packet = record.get('payload', {}).get('print', {})
        state = packet.get('gcode_state', state)
        tray = packet.get('ams', {}).get('tray_now', tray)
        if state in ('RUNNING', 'PAUSE') and (not sequence or 'tray_now' in packet.get('ams', {})):
            sequence.append(tray)
    assert str(sequence[0]) == '255'
    assert {str(value) for value in sequence} == {'255', '0'}
    def during():
        observe(inventory, [3 if str(value) == '0' else value for value in sequence])
    assert print_case(inventory, target=None, after_capture=during)[:2] == ('accounted', None)
    assert rows(inventory, 'SELECT spool_id,weight_used_g FROM spool_usage') == [(56, 165.89)]


def test_cached_tray_and_previous_attempt_are_not_new_evidence(inventory):
    from modules.archives.attempt_accounting import capture_attempt_spools, resolve_attempt_material_plan
    setup_slots(inventory)
    def during():
        send = observe(inventory, [3])
        with inventory.begin() as c:
            c.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at) VALUES(130,1,87,'running',CURRENT_TIMESTAMP)"))
            assert capture_attempt_spools(c, 130, 1, source='fixture')
        send.monitor._current_job_id = 130
        send()  # Cached ams.tray_now=3 must not become evidence for print 130.
        with inventory.connect() as c:
            snapshot = json.loads(c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=130')).scalar_one())
            assert resolve_attempt_material_plan(c, snapshot, 130)['reason'] == 'missing_material_mapping'
        assert rows(inventory, "SELECT key FROM system_config WHERE key LIKE '%observed_trays'") == [(KEY,)]
    assert print_case(inventory, target=None, after_capture=during)[:2] == ('accounted', None)


def test_concurrent_new_trays_union_without_lost_update(inventory):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from modules.archives.attempt_accounting import record_attempt_tray
    setup_slots(inventory)
    def during():
        ready = Barrier(2)
        def write(tray):
            with inventory.begin() as c:
                ready.wait(timeout=10)
                return record_attempt_tray(c, 129, 1, tray)
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(write, [2, 3])) == [True, True]
        assert stored_trays(inventory) == [[2, 3]]
    assert print_case(inventory, target=None, after_capture=during)[:2] == ('unresolved', 'multiple_trays_observed')


@pytest.mark.parametrize('native_json', [False, True])
def test_driver_json_representation_preserves_tray_union_and_accounting(inventory, monkeypatch, native_json):
    """Exercise PG's decoded JSON return type even on an owned SQLite run."""
    from modules.archives import attempt_accounting
    setup_slots(inventory)
    original = attempt_accounting.execute

    def execute(connection, statement, parameters=()):
        result = original(connection, statement, parameters)
        if native_json and statement.startswith('SELECT value FROM system_config WHERE key='):
            row = result.fetchone()
            if row and isinstance(row[0], str):
                row = (json.loads(row[0]),)
            return SimpleNamespace(fetchone=lambda: row)
        return result

    monkeypatch.setattr(attempt_accounting, 'execute', execute)
    def during():
        with inventory.begin() as connection:
            assert attempt_accounting.record_attempt_tray(connection, 129, 1, 3)
            assert not attempt_accounting.record_attempt_tray(connection, 129, 1, 3)
        observe(inventory, [3, 255])
    assert print_case(inventory, target=None, after_capture=during)[:2] == ('accounted', None)
    assert rows(inventory, 'SELECT spool_id,weight_used_g FROM spool_usage') == [(56, 165.89)]


def wire_terminal(send, engine, monkeypatch):
    """Real MQTT lifecycle/terminal transactions; suppress external delivery only."""
    from core import job_timeline, event_bus, job_terminal_observations
    from core.db_compat import sql
    from modules.notifications import error_handling, alert_dispatch, event_dispatcher
    from modules.archives import archive
    from modules.printers.monitors import mqtt_printer
    monkeypatch.setattr(job_terminal_observations, 'dispatch_terminal_alert', lambda *a, **kw: None)
    monkeypatch.setattr(job_timeline, 'enqueue', lambda *a, **kw: None)
    monkeypatch.setattr(job_timeline, 'close_pause_episode', lambda *a, **kw: None)
    monkeypatch.setattr(job_timeline, 'for_printer', lambda *a, **kw: None)
    monkeypatch.setattr(event_bus, 'get_event_bus', lambda: SimpleNamespace(publish=lambda *a: None))
    monkeypatch.setattr(archive, 'create_print_archive', lambda *a, **kw: None)
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *a: None)
    monkeypatch.setattr(error_handling, 'dispatch_alert', lambda *a, **kw: None)
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *a, **kw: None)
    monitor = send.monitor
    monitor._linked_job_id = 87
    monitor._dispatch_alert = lambda **kw: None
    monitor._job_ended = lambda status: mqtt_printer.PrinterMonitor._job_ended(monitor, status)
    transitions = []
    def transition(old, new):
        transitions.append((old, new))
        mqtt_printer.PrinterMonitor._on_state_change(monitor, old, new)
    monitor._on_state_change = transition
    # Run the actual heartbeat UPDATE and assert its persisted status below.
    send.runtime.sql = sql
    send.runtime.resolve_stage_label = lambda *a: None
    send.runtime.parse_lights = lambda *a: None
    send.runtime.mqtt_republish = None
    return transitions


@pytest.mark.parametrize('terminal', ['FINISH', 'HMS'])
def test_persistent_tray_failure_does_not_stall_terminal_or_heartbeat(inventory, monkeypatch, terminal):
    from modules.archives import attempt_accounting
    setup_slots(inventory)
    record_tray = attempt_accounting.record_attempt_tray
    def during():
        send = observe(inventory, [2])
        transitions = wire_terminal(send, inventory, monkeypatch)
        def fail(*args):
            raise RuntimeError('synthetic persistent tray failure')
        monkeypatch.setattr(attempt_accounting, 'record_attempt_tray', fail)
        send(3)
        send.monitor._last_heartbeat = 0
        if terminal == 'FINISH':
            for _ in range(100):
                send(state='FINISH')
            assert transitions == [('RUNNING', 'FINISH')]
            assert send.monitor._current_job_id is None
            status = 'completed'
        else:
            with inventory.begin() as c:
                c.execute(text("UPDATE print_jobs SET filename='fixture.3mf' WHERE id=129"))
            send(hms=[{'attr': 0x0C000300, 'code': 0x00030006}], subtask_name='fixture.3mf')
            status = 'failed'
        assert rows(inventory, 'SELECT status FROM print_jobs WHERE id=129') == [(status,)]
        assert rows(inventory, 'SELECT last_seen IS NOT NULL FROM printers WHERE id=1') == [(True,)]
        assert send.monitor._last_heartbeat == 100
        assert stored_trays(inventory) == [[2]]
        assert send.monitor._pending_attempt_trays == {3}
        # Even a later recovered writer cannot repair a completed attempt.
        monkeypatch.setattr(attempt_accounting, 'record_attempt_tray', record_tray)
        with inventory.begin() as c:
            assert not record_tray(c, 129, 1, 3)
        with inventory.connect() as c:
            snapshot = json.loads(c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=129')).scalar_one())
            assert attempt_accounting.resolve_attempt_material_plan(c, snapshot, 129)['reason'] == 'tray_evidence_incomplete'
    assert print_case(inventory, target=None, after_capture=during)[:2] == ('unresolved', 'tray_evidence_incomplete')
    assert rows(inventory, 'SELECT count(*) FROM spool_usage') == [(0,)]
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools ORDER BY id') == [(1000,)] * 3
    assert json.loads(rows(inventory, 'SELECT consumption_json FROM print_archives')[0][0]) == [
        dict(spool_id=None, grams_used=165.89, reason='tray_evidence_incomplete')]
    from core.print_attempts import clear_job_attempts, sqlalchemy_runner
    with inventory.begin() as c:
        c.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at) VALUES(130,1,87,'running',CURRENT_TIMESTAMP)"))
        assert attempt_accounting.capture_attempt_spools(c, 130, 1, source='next-attempt')
        snapshot = json.loads(c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=130')).scalar_one())
        assert attempt_accounting.resolve_attempt_material_plan(c, snapshot, 130)['reason'] == 'missing_material_mapping'
        clear_job_attempts(sqlalchemy_runner(c), 87)
    assert rows(inventory, "SELECT key FROM system_config WHERE key LIKE '%tray_evidence_incomplete'") == []



def test_valid_first_packet_tray_then_second_tray_never_charges_first(inventory):
    setup_slots(inventory)
    def during():
        send = telemetry(inventory)
        send.monitor._current_job_id = None
        send.monitor._last_gcode_state = 'IDLE'
        def start(*args):
            send.monitor._current_job_id = 129
        send.monitor._on_state_change = start
        send(2)  # Valid first RUNNING packet must remain only observed evidence.
        with inventory.connect() as c:
            plan = json.loads(c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=129')).scalar_one())['material_plan']
            assert plan['state'] == 'unresolved' and plan['reason'] == 'missing_material_mapping'
            assert plan['observed_tray_fallback']['estimated_grams'] == 165.89
        send(3)
    assert print_case(inventory, target=None, after_capture=during)[:2] == ('unresolved', 'multiple_trays_observed')
    assert stored_trays(inventory) == [[2, 3]]
    assert rows(inventory, 'SELECT count(*) FROM spool_usage') == [(0,)]
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools ORDER BY id') == [(1000,)] * 3


def test_previous_print_cached_start_tray_is_not_evidence(inventory, monkeypatch):
    """Exercise the actual start callback with an AMS value from the previous print."""
    from modules.printers.monitors import mqtt_job_lifecycle
    setup_slots(inventory)
    send = telemetry(inventory)
    identifier, _, _ = mqtt_job_lifecycle.record_job_started(
        1, 'A1 fixture', {'subtask_name': 'fixture', 'ams': {'tray_now': 2}}, lambda **kw: None, lambda: None)
    assert identifier is not None
    with inventory.connect() as c:
        plan = json.loads(c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'), dict(id=identifier)).scalar_one())['material_plan']
        assert plan['state'] == 'unresolved' and plan['reason'] == 'missing_material_mapping'
    send.monitor._current_job_id = identifier
    send.monitor._state = {'ams': {'tray_now': 2}, 'subtask_name': 'fixture'}
    send()  # Fresh RUNNING without fresh AMS must not reuse cached tray 2.
    assert rows(inventory, "SELECT key FROM system_config WHERE key LIKE '%observed_trays'") == []
