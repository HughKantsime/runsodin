"""Exercise the monitor's status transaction using independent writer and list connections."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_bambu_slot_count_and_bed import (
    both_databases, broker_socket, fernet_env, pg_review_db, pg_server, review_db,
    sqlite_review_db, connected_monitor,
)


def listed(engine):
    from modules.printers.routes_crud import list_printers
    with Session(engine) as reader:
        printers = list_printers(current_user={'role': 'admin'}, db=reader)
        printer = next(p for p in printers if p.id == 301)
        return printer.gcode_state, printer.bed_temp, printer.last_seen, printer.state_needs_attention


def intercept_evidence(monkeypatch, intercept):
    from core.db_utils import _ConnectionAdapter
    original = _ConnectionAdapter.execute

    def execute(conn, statement, parameters=None):
        if (parameters and parameters[0] == 'printer.attention.301'
                and statement.startswith(('INSERT INTO system_config', 'DELETE FROM system_config'))):
            intercept(conn, statement)
        return original(conn, statement, parameters)

    monkeypatch.setattr(_ConnectionAdapter, 'execute', execute)


@both_databases
def test_reader_between_status_and_evidence_writes(review_db, connected_monitor, monkeypatch):
    _, socket, _, _ = connected_monitor
    engine = review_db.get_bind()
    before = listed(engine)
    between = []
    intercept_evidence(monkeypatch, lambda conn, statement: between.append(listed(engine)))
    socket.deliver({'print': {'gcode_state': 'FAILED', 'bed_temper': 32, 'print_error': 0}})
    assert between == [before]
    state, temperature, heartbeat, attention = listed(engine)
    assert (state, temperature, attention) == ('FAILED', 32, False)
    assert heartbeat is not None


@both_databases
def test_list_keeps_evidence_if_a_report_commits_during_enrichment(review_db, connected_monitor, monkeypatch):
    from modules.printers import routes_crud
    _, socket, clock, _ = connected_monitor
    engine = review_db.get_bind()
    socket.deliver({'print': {'gcode_state': 'FAILED', 'bed_temper': 32, 'print_error': 0}})
    before = listed(engine)
    assert before[3] is False
    enrich = routes_crud.enrich_live_values

    def next_report(*args):
        clock[0] += 11
        socket.deliver({'print': {'gcode_state': 'FAILED', 'bed_temper': 55, 'print_error': 42}})
        return enrich(*args)

    with monkeypatch.context() as patch:
        patch.setattr(routes_crud, 'enrich_live_values', next_report)
        assert listed(engine) == before
    state, temperature, _, attention = listed(engine)
    assert (state, temperature, attention) == ('FAILED', 55, True)


@both_databases
def test_failed_heartbeat_uses_database_clock(review_db, connected_monitor, monkeypatch):
    from modules.printers.monitors import mqtt_printer
    _, socket, _, _ = connected_monitor
    monkeypatch.setattr(mqtt_printer.sql, 'now', lambda: "'2021-02-03 04:05:06'")
    socket.deliver({'print': {'gcode_state': 'FAILED', 'print_error': 0}})
    state, _, heartbeat, attention = listed(review_db.get_bind())
    assert (state, str(heartbeat), attention) == ('FAILED', '2021-02-03 04:05:06', False)


@both_databases
@pytest.mark.parametrize('previous,new_state,refuse_delete,rollback', [
    (None, 'FAILED', True, False),
    (42, 'FAILED', True, False),
    (0, 'FAILED', False, False),
    (0, 'FAILED', True, True),
    (0x0300400C, 'FAILED', True, True),
    (0, 'IDLE', True, True),
])
def test_evidence_failure_policy(review_db, connected_monitor, monkeypatch, caplog,
                                 previous, new_state, refuse_delete, rollback):
    from modules.printers.monitors import mqtt_status
    _, socket, clock, events = connected_monitor
    monkeypatch.setattr(mqtt_status, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(mqtt_status, '_attention_warning_after', {})
    engine = review_db.get_bind()
    initial = {'gcode_state': 'IDLE' if previous is None else 'FAILED', 'bed_temper': 20}
    if previous is not None:
        initial['print_error'] = previous
    socket.deliver({'print': initial})
    # Give the old heartbeat an unambiguous value on both database clocks.
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET last_seen='2020-01-01' WHERE id=301"))
    before = listed(engine)
    assert before[3] is (previous == 42)
    events.clear()
    packet = {'print': {'gcode_state': new_state, 'bed_temper': 55, 'print_error': 0 if previous is None else 17}}

    def refuse(conn, statement):
        if statement.startswith('INSERT') or refuse_delete:
            # A real SQL failure leaves PostgreSQL aborted until rollback to the savepoint.
            conn.execute('SELECT * FROM missing_attention_test_table')

    with monkeypatch.context() as faults:
        intercept_evidence(faults, refuse)
        clock[0] += 11
        socket.deliver(packet)
        state, temperature, heartbeat, attention = listed(engine)
        if rollback:
            assert (state, temperature, heartbeat, attention) == before
            clock[0] += 59
            socket.deliver(packet)
            clock[0] += 1
            socket.deliver(packet)
            warning = '[Cert Bambu] attention evidence could not be updated; status write retried'
            assert caplog.messages.count(warning) == 2
            assert not any(kind == 'printer_telemetry' for kind, _ in events)
        else:
            assert (state, temperature, attention) == ('FAILED', 55, True)
            assert heartbeat != before[2]
            assert [data['state_needs_attention'] for kind, data in events if kind == 'printer_telemetry'] == [True]

    if rollback:
        socket.deliver(packet)  # no clock advance: the rejected heartbeat is still due
        state, temperature, heartbeat, attention = listed(engine)
        assert (state, temperature, attention) == (new_state, 55, new_state == 'FAILED')
        assert heartbeat != before[2]
        if new_state != 'FAILED':
            with engine.connect() as reader:
                assert reader.execute(text("SELECT value FROM system_config WHERE key='printer.attention.301'")).first() is None
