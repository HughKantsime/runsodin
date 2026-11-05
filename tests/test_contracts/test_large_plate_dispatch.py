"""Real dispatch orchestration; synthetic files and substituted hardware only."""
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_h2d_ams_dispatch import setup_h2d
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from tests.test_contracts.test_dispatch_capability import REPORT


def hardware(monkeypatch):
    from modules.printers import dispatch, dispatch_ams_gate as gate
    from modules.printers.nozzle_capability import installed_nozzles
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda runtime: None)
    def acquired(config, **options):
        snapshot = RawAMSSnapshot(options['operation_id'], 'c'*32, 1, time.monotonic(),
            options['configuration_key'], 1, (RawTrayFact(2, 0, 1, 'PETG', 'reported'),),
            True, nozzles=tuple(installed_nozzles(REPORT, dual=True)))
        return RuntimeBoundAMSResult(True, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)
    def adapter(*args, before_start, **kwargs):
        ok = before_start.prepare() and before_start()
        return ok, 'Synthetic hardware accepted' if ok else str(before_start.refusal)
    monkeypatch.setattr(dispatch, '_dispatch_bambu', adapter)


@pytest.mark.parametrize('explicit', [False, True])
def test_slow_source_inspection_precedes_fresh_admission_window(review_db, tmp_path, monkeypatch, explicit):
    from modules.printers import dispatch, dispatch_ams_gate as gate, dispatch_orchestration as orchestration, smart_plug
    engine, source_path = setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1] if explicit else None)
    hardware(monkeypatch)
    clock = [100.0]
    events = []
    monkeypatch.setattr(orchestration, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    from modules.printers import services
    original = services.inspect_ams_payload
    def slow(*args, **kwargs):
        assert 'power' not in events
        events.append('scan')
        assert str(args[0]) != str(source_path)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT state FROM printer_dispatch_claims WHERE job_id=53')).scalar_one() == 'preparing'
        clock[0] += 90
        return original(*args, **kwargs)
    monkeypatch.setattr(services, 'inspect_ams_payload', slow)
    power = smart_plug.prepare_bounded_dispatch_power
    def fresh(printer_id, deadline):
        events.append('power')
        assert deadline == clock[0] + 30
        # Pass a real monotonic deadline to the hardware boundary implementation.
        return power(printer_id, time.monotonic() + 30)
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', fresh)
    result = dispatch.dispatch_job(59, 53)
    assert result[0], result
    assert events == ['scan', 'power']


@pytest.mark.skipif(not os.environ.get('ODIN_LARGE_PLATE_BENCHMARK'), reason='opt-in large synthetic benchmark')
def test_two_concurrent_large_dispatches(review_db, tmp_path, monkeypatch):
    from core.crypto import encrypt
    from core.print_payload_snapshot import hash_regular_payload
    from core.ams_source_inspection import inspect_ams_sources
    from modules.printers import dispatch, services
    from ops.large_plate_benchmark import make_plate, peak_rss_bytes
    size = int(os.environ['ODIN_LARGE_PLATE_BENCHMARK'])
    engine, path = setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1])
    make_plate(path, size)
    hardware(monkeypatch)
    digest = hash_regular_payload(path)
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET payload_sha256=:hash WHERE id IN (53,54)'), {'hash': digest})
        conn.execute(text("INSERT INTO printers(id,name,is_active,api_type,api_host,api_key,org_id,slot_count,bed_x_mm,bed_y_mm,nozzle_diameter,gcode_state,machine_type,model) SELECT 60,'Second synthetic printer',is_active,api_type,'127.0.0.2',:key,org_id,slot_count,bed_x_mm,bed_y_mm,nozzle_diameter,gcode_state,machine_type,model FROM printers WHERE id=59"), {'key': encrypt('SYNTHETICSECOND|synthetic')})
        conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(60,2,'PETG')"))
        conn.execute(text('UPDATE jobs SET printer_id=60,ams_mapping=\'[-1,1]\' WHERE id=54'))
    inspection_baseline_rss = peak_rss_bytes()
    started = time.perf_counter()
    with path.open('rb') as source:
        assert len(inspect_ams_sources(source).sources) == 2
    inspection_seconds = time.perf_counter() - started
    inspection_peak_rss = peak_rss_bytes()
    # This file uses two sources, both PETG, on one slot. Update the committed metadata.
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET ams_mapping='[1,1]' WHERE id IN (53,54)"))
        conn.execute(text('UPDATE print_files SET filaments_json=:filaments WHERE id=52'),
                     {'filaments': json.dumps([{'slot': 1, 'type': 'PETG'}, {'slot': 2, 'type': 'PETG'}])})
    # Two-source H2D requires nozzle binding. Synthetic packet feeds both on physical 0.
    from modules.printers import dispatch_ams_gate as gate
    original_read = gate.read_current_selected_ams
    def bound(*args, **kwargs):
        from dataclasses import replace
        result = original_read(*args, **kwargs)
        return replace(result, snapshot=replace(result.snapshot, trays=tuple(replace(t, extruder=0) for t in result.snapshot.trays)))
    monkeypatch.setattr(gate, 'read_current_selected_ams', bound)
    # Synchronize the real inspection service to prove overlapping large dispatches.
    barrier = threading.Barrier(2)
    original = services.inspect_ams_payload
    scans = []
    def synchronized(*args, **kwargs):
        scans.append(str(args[0]))
        barrier.wait(timeout=300)
        return original(*args, **kwargs)
    monkeypatch.setattr(services, 'inspect_ams_payload', synchronized)
    baseline_rss = peak_rss_bytes()
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(dispatch.dispatch_job, printer, job) for printer, job in [(59,53),(60,54)]]
        results = [f.result() for f in futures]
    peak_rss = peak_rss_bytes()
    measurement = dict(dispatch_inspections=len(scans), inspection_baseline_rss_bytes=inspection_baseline_rss, inspection_peak_rss_bytes=inspection_peak_rss, gcode_mib=size, archive_bytes=path.stat().st_size, inspection_seconds=inspection_seconds,
                       concurrent_dispatch_seconds=time.perf_counter()-started,
                       baseline_peak_rss_bytes=baseline_rss, peak_rss_bytes=peak_rss, results=results)
    print('\nLARGE_PLATE_BENCHMARK=' + json.dumps(measurement), flush=True)
    assert all(result[0] for result in results), results
    assert len(scans) == 2


def test_preparing_claim_survives_stale_schedule_cleanup(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, services
    from modules.jobs.scheduler import Scheduler
    engine, _ = setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1])
    hardware(monkeypatch)
    original = services.inspect_ams_payload
    def inspect(*args, **kwargs):
        # The scheduled time can cross the two-hour cleanup cutoff mid-scan.
        from sqlalchemy.orm import Session
        with engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET scheduled_start='2000-01-01',status='scheduled' WHERE id=53"))
            conn.execute(text("UPDATE printer_dispatch_claims SET created_at='2000-01-01',updated_at='2000-01-01' WHERE job_id=53"))
        with Session(engine) as db:
            assert Scheduler()._cleanup_stale_schedules(db, candidate_ids=[53]) == 0
            db.commit()
        return original(*args, **kwargs)
    monkeypatch.setattr(services, 'inspect_ams_payload', inspect)
    result = dispatch.dispatch_job(59, 53)
    assert result[0], result


def test_inspection_refusal_keeps_specific_reason(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, services
    engine, _ = setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1])
    def refuse(*args, **kwargs):
        raise ValueError('Plate G-code is 257 MiB; the limit is 256 MiB')
    monkeypatch.setattr(services, 'inspect_ams_payload', refuse)
    monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda *a, **kw: pytest.fail('transport reached'))
    result = dispatch.dispatch_job(59, 53)
    assert not result[0] and '257 MiB; the limit is 256 MiB' in result[1]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0


def test_source_changed_after_inspection_refuses(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, services
    engine, path = setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1])
    original = services.inspect_ams_payload
    def inspect(*args, **kwargs):
        facts = original(*args, **kwargs)
        with path.open('ab') as source:
            source.write(b'changed')
        return facts
    monkeypatch.setattr(services, 'inspect_ams_payload', inspect)
    monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda *a, **kw: pytest.fail('transport reached'))
    result = dispatch.dispatch_job(59, 53)
    assert not result[0] and 'payload changed during preparation' in result[1]


def test_background_handle_remains_preparing_during_slow_inspection(review_db, tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    from modules.printers import dispatch, services, dispatch_background as background
    engine, _ = setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1])
    hardware(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = services.inspect_ams_payload
    def inspect(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(services, 'inspect_ams_payload', inspect)
    handle = dispatch.stage_dispatch(59, 53)
    try:
        assert entered.wait(5)
        assert handle['accepted'] and handle['phase'] == 'preparing'
        token = handle['attempt_id']
        with engine.begin() as conn:
            conn.execute(text("UPDATE printer_dispatch_claims SET created_at='2000-01-01',updated_at='2000-01-01' WHERE job_id=53"))
        with Session(engine) as db:
            record = dispatch.read_dispatch_progress(db, token, 53)
            assert record['phase'] == 'preparing' and record['retry_blocked']
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as duplicate:
            dispatch.stage_dispatch(59, 53)
        assert duplicate.value.status_code == 409
    finally:
        release.set()
        deadline = time.monotonic() + 10
        while background.active(handle['attempt_id']) and time.monotonic() < deadline:
            time.sleep(.01)
    assert not background.active(handle['attempt_id'])
    with Session(engine) as db:
        assert dispatch.read_dispatch_progress(db, handle['attempt_id'], 53)['phase'] == 'start_accepted'


def test_capability_inspection_refusal_keeps_specific_reason(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, dispatch_capability_gate
    from core import dispatch_claims
    setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1])
    def refuse(*args, **kwargs):
        raise ValueError('Capability metadata exceeds inspection bound')
    monkeypatch.setattr(dispatch_capability_gate, 'static_refusal', refuse)
    monkeypatch.setattr(dispatch_claims, 'acquire', lambda *a, **kw: pytest.fail('claim reached'))
    result = dispatch.dispatch_job(59, 53)
    assert not result[0] and 'Capability metadata exceeds inspection bound' in result[1]
    assert 'reconciliation' not in result[1]
