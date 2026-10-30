"""Prints that end while no monitor is watching, through the real monitor paths."""
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_dispatch_restart_resume import running_bambu

CANCELLED_CODE = 0x0300400C
OUTCOMES = [('FINISH', 0, 'completed'), ('FAILED', 7, 'failed'), ('FAILED', CANCELLED_CODE, 'cancelled')]


def packet(monitor, **fields):
    """Deliver one MQTT report through the real status callback, which holds the lock."""
    monitor._on_status(SimpleNamespace(raw_data={'print': fields}))


def restarted(case):
    """A new monitor after a restart: nothing tracked, no state, no packet yet."""
    monitor = case.monitor()
    monitor._state = {}
    now = time.time()
    monitor._last_heartbeat = monitor._last_progress_update = monitor._last_spool_check = now
    monitor._camera_discovered = True
    return monitor


def observe(monitor, order, **fields):
    """Reach the monitor by each order a restart can produce; every order runs both paths."""
    if order == 'recovery_first':
        # The report is already merged when recovery wakes and is then redelivered.
        monitor._state.update(fields)
        monitor._recover_orphaned_jobs()
        packet(monitor, **fields)
    elif order == 'callback_first':
        packet(monitor, **fields)
        monitor._recover_orphaned_jobs()
    else:  # status_after_wait
        monitor._recover_orphaned_jobs()
        packet(monitor, **fields)


@pytest.fixture
def delivered(running_bambu, monkeypatch):
    """Terminal alerts (this job has no economic identity: the legacy route) and timeline items."""
    from core import job_timeline
    from modules.notifications import alert_dispatch
    seen = SimpleNamespace(alerts=[], timeline=[])
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kw: seen.alerts.append(kw['alert_type']))
    monkeypatch.setattr(job_timeline, 'enqueue', lambda item: seen.timeline.append(item['kind']))
    return seen


def row_state(case):
    with case.engine.connect() as conn:
        rows = conn.execute(text('SELECT * FROM print_jobs')).mappings().all()
        claims = conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()
        job = conn.execute(text('SELECT status,actual_end FROM jobs WHERE id=53')).one()
    return rows, claims, job


def assert_fenced(case, monitor):
    rows, claims, job = row_state(case)
    assert monitor._current_job_id is None
    assert len(rows) == 1 and dict(rows[0]) == case.before
    assert claims == 1 and job[0] == 'printing'
    assert case.effects['archive'] == []


def assert_terminal_effects(case, delivered, expected):
    from core.base import AlertType
    assert case.effects['archive'] == [dict(print_job_id=case.identifier, printer_id=59, success=expected == 'completed',
                                            result_status='cancelled' if expected == 'cancelled' else None)]
    assert len(case.effects['completed']) == int(expected == 'completed')
    assert len(case.effects['care']) == int(expected == 'completed')
    assert delivered.alerts == [{'completed': AlertType.PRINT_COMPLETE, 'failed': AlertType.PRINT_FAILED,
                                 'cancelled': AlertType.PRINT_CANCELLED}[expected]]
    assert [kind for kind in delivered.timeline if kind in ('completed', 'failed', 'cancelled')] == [expected]


# ---------------------------------------------------------------- gap 1: dispatched print
@pytest.mark.parametrize('order', ['callback_first', 'recovery_first', 'status_after_wait'])
@pytest.mark.parametrize('state,error,expected', OUTCOMES)
def test_dispatched_print_ended_in_downtime_is_closed_once(running_bambu, delivered, order, state, error, expected):
    from modules.printers import dispatch
    case = running_bambu
    monitor = restarted(case)
    fields = dict(gcode_state=state, subtask_name=case.state['subtask_name'], print_error=error)
    observe(monitor, order, **fields)
    packet(monitor, **fields)  # A repeated report and a second recovery change nothing.
    monitor._recover_orphaned_jobs()
    rows, claims, job = row_state(case)
    assert len(rows) == 1 and rows[0]['id'] == case.identifier
    assert rows[0]['status'] == expected and rows[0]['ended_at'] is not None
    assert job[0] == expected and job[1] == rows[0]['ended_at']
    assert claims == 0
    assert monitor._current_job_id is None and monitor._linked_job_id is None
    assert_terminal_effects(case, delivered, expected)
    # The whole point: the printer accepts its next job again.
    assert dispatch.dispatch_job(59, 54)[0]


def test_recovered_end_uses_the_first_start_filament_evidence(running_bambu):
    case = running_bambu
    monitor = restarted(case)
    monitor._state['_ams_remain'] = {1: 70}
    monitor._state.update(gcode_state='FINISH', subtask_name=case.state['subtask_name'], print_error=0)
    monitor._recover_orphaned_jobs()
    rows, _, _ = row_state(case)
    assert rows[0]['status'] == 'completed'
    assert rows[0]['filament_used_g'] == (100 if case.weights else None)


def test_bambu_suffix_alias_proves_the_end(running_bambu):
    case = running_bambu
    monitor = restarted(case)
    packet(monitor, gcode_state='FINISH', subtask_name=case.state['subtask_name'].removesuffix('.3mf'), print_error=0)
    rows, claims, _ = row_state(case)
    assert rows[0]['status'] == 'completed' and claims == 0


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
@pytest.mark.parametrize('fields', [
    dict(gcode_state='IDLE'),  # Cannot be closed from idle.
    dict(gcode_state='FINISH', subtask_name='some-other-print.3mf'),  # Or an unrelated task.
    dict(gcode_state='FAILED', subtask_name='odin-dispatch-' + 'f' * 32 + '.3mf'),  # Or another reserved transport.
    dict(gcode_state='FINISH'),  # A report without a name proves nothing.
], ids=['idle', 'unrelated', 'other_reserved', 'unnamed'])
def test_unproven_end_keeps_the_dispatch_fence(running_bambu, order, fields):
    case = running_bambu
    monitor = restarted(case)
    if fields['gcode_state'] == 'IDLE':
        fields = dict(fields, subtask_name=case.state['subtask_name'])
    observe(monitor, order, **fields)
    assert_fenced(case, monitor)


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
def test_education_sensitive_end_stays_untouched(running_bambu, order):
    case = running_bambu
    monitor = restarted(case)
    observe(monitor, order, gcode_state='FINISH', subtask_name='odin-' + 'a' * 32 + '.3mf')
    assert_fenced(case, monitor)


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
@pytest.mark.parametrize('mutation', [
    "UPDATE printers SET api_host='changed.example.test' WHERE id=59",
    "UPDATE printer_dispatch_claims SET state='preparing' WHERE printer_id=59",
    "UPDATE dispatch_attempt_receipts SET stable_key_source='untrusted'",
])
def test_refused_identity_keeps_the_fence(running_bambu, order, mutation, caplog):
    case = running_bambu
    with case.engine.begin() as conn:
        conn.execute(text(mutation))
    monitor = restarted(case)
    observe(monitor, order, gcode_state='FINISH', subtask_name=case.state['subtask_name'])
    assert monitor._current_job_id is None
    rows, claims, _ = row_state(case)
    assert len(rows) == 1 and rows[0]['status'] == 'running' and claims == 1
    assert case.effects['archive'] == []
    assert 'reason=receipt_bound_resume_refused' in caplog.text


def test_failed_end_does_not_fail_connect(running_bambu, monkeypatch):
    # connect() reports any recovery exception as a failed connection.
    case = running_bambu
    monitor = restarted(case)
    attempted = []
    def fail(status):
        attempted.append(status)
        raise RuntimeError('synthetic')
    monkeypatch.setattr(monitor, '_job_ended', fail)
    monitor._state.update(gcode_state='FINISH', subtask_name=case.state['subtask_name'])
    monitor._recover_orphaned_jobs()
    assert attempted == ['completed']
    assert monitor._current_job_id is None and monitor._linked_job_id is None


@pytest.fixture
def owned_bambu(creators, review_db, tmp_path, monkeypatch):
    """An order-owned dispatched print, so a terminal claims its durable summary and alert intent."""
    import hashlib
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    from core.registry import registry
    from modules.archives import archive
    from modules.notifications import alert_dispatch, alert_outbox, event_dispatcher
    from modules.orders.production import OrderFulfillmentService
    from modules.orders.routes.orders_crud import router
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle, mqtt_printer
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    client, auth, _, _ = creators
    client.app.include_router(router, prefix='/api')
    monkeypatch.setitem(registry._providers, 'OrderFulfillmentProvider', OrderFulfillmentService())
    engine, source = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    with engine.begin() as c:
        c.execute(text("INSERT INTO models(id,name,org_id,quantity_per_bed,build_time_hours,default_filament_type) VALUES(101,'Owned model',1,1,1,'PLA')"))
        c.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(101,'Owned product',1,20)"))
        c.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(101,101,1)'))
        c.execute(text('INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(58,101,:name,:name,:source,1)'), {'name': source.name, 'source': str(source)})
    reply = client.post('/api/orders', headers=auth('admin'), json={'order_number': 'Owned order', 'items': [{'product_id': 101, 'quantity': 1, 'unit_price': 20}]})
    assert reply.status_code == 200, reply.text
    order_id = reply.json()['id']
    reply = client.post(f'/api/orders/{order_id}/schedule', headers=auth('admin'))
    assert reply.status_code == 200, reply.text
    with engine.begin() as c:
        job = c.execute(text('SELECT min(j.id) FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'), {'id': order_id}).scalar_one()
        c.execute(text('UPDATE jobs SET printer_id=59,print_file_id=58,payload_sha256=:sha,plate_index=0 WHERE id=:id'), {'sha': hashlib.sha256(source.read_bytes()).hexdigest(), 'id': job})
    names = []
    monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda jid, path, name, creds, **kw: (names.append(name) or True, 'Owned stub'))
    assert dispatch.dispatch_job(59, job)[0]

    @contextmanager
    def database():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()

    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', database)
    monkeypatch.setattr(mqtt_printer, 'get_db', database)
    alerts = []
    monkeypatch.setattr(alert_outbox, 'dispatch', lambda *a: alerts.append(a))
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kw: pytest.fail('Durable intent must deliver this alert'))
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *a: None)
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *a: None)
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kw: None)
    identifier, linked, _ = mqtt_job_lifecycle.record_job_started(
        59, 'Owned', {'subtask_name': names[0], 'gcode_file': 'Metadata/plate_1.gcode'}, lambda **kw: None, lambda: None)
    assert identifier and linked == job

    def monitor():
        result = mqtt_printer.PrinterMonitor(59, 'Owned', '127.0.0.1', 'SYNTHETICPRINTER', 'synthetic')
        result._state = {}
        result._status_received.set()
        result._last_heartbeat = result._last_progress_update = result._last_spool_check = time.time()
        result._camera_discovered = True
        return result

    return SimpleNamespace(engine=engine, monitor=monitor, identifier=identifier, job=job, name=names[0], alerts=alerts)


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first', 'status_after_wait'])
@pytest.mark.parametrize('state,error,expected', OUTCOMES)
def test_durable_terminal_intent_is_claimed_once(owned_bambu, order, state, error, expected):
    case = owned_bambu
    monitor = case.monitor()
    fields = dict(gcode_state=state, subtask_name=case.name, print_error=error)
    observe(monitor, order, **fields)
    packet(monitor, **fields)
    monitor._recover_orphaned_jobs()
    assert len(case.alerts) == 1
    with case.engine.connect() as conn:
        uid, status = conn.execute(text('SELECT economic_uid,status FROM print_jobs WHERE id=:id'), {'id': case.identifier}).one()
        assert uid and status == expected
        assert conn.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': case.job}).scalar_one() == expected
        assert tuple(conn.execute(text('SELECT print_attempt_uid,outcome FROM job_summary_outbox')).one()) == (uid, expected)
        assert conn.execute(text("SELECT count(*) FROM alert_outbox WHERE alert_kind LIKE 'terminal:%'")).scalar_one() == 1
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 1
