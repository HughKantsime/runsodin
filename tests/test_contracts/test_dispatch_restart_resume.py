"""Receipt-bound Bambu restart and terminal drift through real monitor paths."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch


@pytest.fixture(params=[None, 1], ids=['single-tenant', 'org'])
def running_bambu(review_db, tmp_path, monkeypatch, request):
    from core import event_bus, events
    from core.db_utils import _ConnectionAdapter
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle as lifecycle, mqtt_printer
    from modules.notifications import alert_dispatch, event_dispatcher
    from modules.archives import archive

    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    with engine.begin() as conn:
        # The affected single-tenant install has NULL printer ownership.
        conn.execute(text('UPDATE printers SET org_id=:owner WHERE id=59'), {'owner': request.param})
        conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(700,'Synthetic','PLA')"))
        conn.execute(text('INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g,org_id) VALUES(700,700,1000,800,1)'))
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=700 WHERE printer_id=59'))
    names = []
    monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda *a, **kw: (names.append(a[2]) or True, 'Synthetic accepted'))
    assert dispatch.dispatch_job(59, 53)[0]

    @contextmanager
    def db():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()

    monkeypatch.setattr(lifecycle, 'get_db', db)
    monkeypatch.setattr(mqtt_printer, 'get_db', db)
    effects = {'started': [], 'completed': [], 'archive': [], 'care': []}
    bus = event_bus.get_event_bus()
    bus.subscribe(events.JOB_STARTED, lambda event: effects['started'].append(event))
    bus.subscribe(events.JOB_COMPLETED, lambda event: effects['completed'].append(event))
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kw: None)
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *a: None)
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *a: effects['care'].append(a))
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kw: effects['archive'].append(kw))

    state = {'gcode_state': 'RUNNING', 'subtask_name': names[0],
             'gcode_file': 'Metadata/plate_1.gcode', '_ams_remain': {1: 80}, 'print_error': 0}

    def monitor():
        result = mqtt_printer.PrinterMonitor(59, 'Synthetic', '127.0.0.1', 'SYNTHETICPRINTER', 'synthetic')
        result._state = dict(state)
        result._status_received.set()
        result._dispatch_alert = lambda **kw: None
        result._trigger_reschedule = lambda: None
        return result

    original = monitor()
    assert original._job_started()
    identifier = original._current_job_id
    with engine.connect() as conn:
        before = dict(conn.execute(text('SELECT * FROM print_jobs WHERE id=:id'), {'id': identifier}).mappings().one())
    assert before['economic_birth_nonce'] and before['spool_snapshot']
    return SimpleNamespace(engine=engine, monitor=monitor, identifier=identifier, before=before,
                           effects=effects, weights=original._start_spool_weights, state=state)


def drift(engine):
    with engine.begin() as conn:
        conn.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=59"))


@pytest.mark.parametrize('first', ['callback', 'recovery'])
@pytest.mark.parametrize('loadout_changed', [False, True])
def test_reserved_restart_resumes_once_and_finish_retires(running_bambu, first, loadout_changed):
    case = running_bambu
    if loadout_changed:
        drift(case.engine)
        # Reproduce the old failed proof precisely without exposing its keys.
        from core import auth
        from core.dispatch_identity import build_identity, stable_configuration_key
        from modules.printers import dispatch
        with case.engine.connect() as conn:
            receipt = conn.execute(text('SELECT * FROM dispatch_attempt_receipts')).mappings().one()
            creds = dispatch._get_printer_info(59, connection=conn)
            assert build_identity(creds, auth.SECRET_KEY.encode()).configuration_key != receipt['configuration_key']
            assert stable_configuration_key(creds, auth.SECRET_KEY.encode()) == receipt['stable_configuration_key']
    restarted = case.monitor()
    restarted._state['_ams_remain'] = {1: 70}
    if first == 'callback':
        restarted._on_state_change(None, 'RUNNING')
        restarted._recover_orphaned_jobs()
    else:
        restarted._recover_orphaned_jobs()
        restarted._on_state_change(None, 'RUNNING')
    for _ in range(3):
        restarted._on_state_change('RUNNING', 'RUNNING')
        assert restarted._job_started()
    assert restarted._current_job_id == case.identifier
    assert restarted._linked_job_id == 53
    assert restarted._start_spool_weights == case.weights
    with case.engine.connect() as conn:
        rows = conn.execute(text('SELECT * FROM print_jobs')).mappings().all()
        assert len(rows) == 1 and dict(rows[0]) == case.before
    restarted._on_state_change('RUNNING', 'FINISH')
    restarted._on_state_change('FINISH', 'FINISH')
    # Also exercise duplicate terminal delivery with the old persisted ID.
    from modules.printers.monitors import mqtt_job_lifecycle
    mqtt_job_lifecycle.record_job_ended(59, 'Synthetic', case.identifier, 53, 'completed',
                                      restarted._state, case.weights, lambda **kw: None)
    with case.engine.connect() as conn:
        row = conn.execute(text('SELECT * FROM print_jobs')).mappings().one()
        assert row['id'] == case.identifier and row['status'] == 'completed'
        assert row['filament_used_g'] == (100 if case.weights else None)
        for field in ('started_at', 'scheduled_job_id', 'economic_uid', 'economic_birth_nonce', 'spool_snapshot'):
            assert row[field] == case.before[field]
        job = conn.execute(text('SELECT status,actual_end FROM jobs WHERE id=53')).one()
        assert job[0] == 'completed' and job[1] == row['ended_at']
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts')).scalar_one() == case.identifier
        assert conn.execute(text("SELECT count(*) FROM cost_source_lifetimes WHERE resource_kind='attempt'")).scalar_one() == 1
    for effect in case.effects.values():
        assert len(effect) == 1


@pytest.mark.parametrize('mutation', [
    "UPDATE printers SET api_host='changed.example.test' WHERE id=59",
    "UPDATE printers SET plug_auto_on=TRUE WHERE id=59",
    "UPDATE printers SET org_id=2 WHERE id=59",
    "UPDATE jobs SET status='completed' WHERE id=53",
    "UPDATE printer_dispatch_claims SET state='preparing' WHERE printer_id=59",
    "UPDATE dispatch_attempt_receipts SET stable_key_source='untrusted'",
    "UPDATE print_jobs SET scheduled_job_id=NULL",
])
@pytest.mark.parametrize('first', ['callback', 'recovery'])
def test_refused_resume_keeps_fence_without_duplicate(running_bambu, mutation, first, caplog):
    case = running_bambu
    with case.engine.begin() as conn:
        conn.execute(text(mutation))
    restarted = case.monitor()
    actions = [restarted._job_started, restarted._recover_orphaned_jobs]
    if first == 'recovery':
        actions.reverse()
    for action in actions + actions:
        action()
    assert restarted._current_job_id is None
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 1
        assert conn.execute(text('SELECT status FROM print_jobs')).scalar_one() == 'running'
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1
    assert 'reason=receipt_bound_resume_refused' in caplog.text


@pytest.mark.parametrize('loadout_changed', [False, True])
def test_legacy_receipt_without_stable_key_keeps_full_equality(running_bambu, loadout_changed):
    case = running_bambu
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE dispatch_attempt_receipts SET stable_configuration_key=NULL,stable_key_source=NULL'))
    if loadout_changed:
        drift(case.engine)
    restarted = case.monitor()
    restarted._recover_orphaned_jobs()
    assert restarted._job_started() is (not loadout_changed)
    assert restarted._current_job_id == (None if loadout_changed else case.identifier)
    # Original in-memory monitor can still finish; legacy drift retains its fence.
    original = case.monitor()
    original._current_job_id = case.identifier
    original._linked_job_id = 53
    original._start_spool_weights = case.weights
    original._on_state_change('RUNNING', 'FINISH')
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 1
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == int(loadout_changed)


def test_different_reserved_transport_still_creates_new_physical_row(running_bambu):
    case = running_bambu
    restarted = case.monitor()
    restarted._state['subtask_name'] = 'odin-dispatch-' + 'f' * 32 + '.3mf'
    assert restarted._job_started()
    assert restarted._current_job_id != case.identifier and restarted._linked_job_id is None
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 2
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts')).scalar_one() == case.identifier
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1


@pytest.mark.parametrize('source', ['dispatch', 'backfilled_no_printer_audit', 'untrusted'])
def test_terminal_stable_key_requires_accepted_provenance(running_bambu, source):
    case = running_bambu
    drift(case.engine)
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE dispatch_attempt_receipts SET stable_key_source=:source'), {'source': source})
    original = case.monitor()
    original._current_job_id = case.identifier
    original._linked_job_id = 53
    original._start_spool_weights = case.weights
    original._on_state_change('RUNNING', 'FINISH')
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == int(source == 'untrusted')


def test_terminal_loadout_race_uses_locked_stable_identity(running_bambu, monkeypatch):
    from modules.printers import dispatch
    case = running_bambu
    read = dispatch._get_printer_info
    changed = []

    def change_after_hint(printer_id, **kwargs):
        creds = read(printer_id, **kwargs)
        if kwargs.get('connection') is None and not changed:
            changed.append(True)
            drift(case.engine)
        return creds

    monkeypatch.setattr(dispatch, '_get_printer_info', change_after_hint)
    monitor = case.monitor()
    monitor._current_job_id = case.identifier
    monitor._linked_job_id = 53
    monitor._start_spool_weights = case.weights
    monitor._on_state_change('RUNNING', 'FINISH')
    assert changed == [True]
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert conn.execute(text('SELECT status FROM print_jobs')).scalar_one() == 'completed'


@pytest.mark.parametrize('first', ['callback', 'recovery'])
def test_receipt_proves_bambu_suffix_alias_without_changing_persisted_name(running_bambu, first):
    case = running_bambu
    monitor = case.monitor()
    monitor._state['subtask_name'] = case.state['subtask_name'].removesuffix('.3mf')
    actions = [monitor._job_started, monitor._recover_orphaned_jobs]
    if first == 'recovery':
        actions.reverse()
    for action in actions:
        action()
    assert monitor._current_job_id == case.identifier and monitor._linked_job_id == 53
    with case.engine.connect() as conn:
        rows = conn.execute(text('SELECT * FROM print_jobs')).mappings().all()
        assert len(rows) == 1 and dict(rows[0]) == case.before
