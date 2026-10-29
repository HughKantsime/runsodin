"""H2D evidence through the real validator, admission and post-upload gates."""
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db as review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from tests.test_contracts.test_dispatch_capability import REPORT
from tests.test_contracts.test_ams_source_inspection import H2D, h2d_archive


def setup_h2d(review_db, tmp_path, monkeypatch, *, mapping=None):
    from modules.printers import dispatch_acquisition
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu', slot_count=4)
    path.write_bytes(H2D.read_bytes())
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET machine_type='O1D',model='H2D',bed_x_mm=350,bed_y_mm=320 WHERE id=59"))
        conn.execute(text('UPDATE jobs SET payload_sha256=:hash,ams_mapping=:mapping WHERE id=53'),
            {'hash': hashlib.sha256(path.read_bytes()).hexdigest(), 'mapping': json.dumps(mapping) if mapping is not None else None})
        conn.execute(text('UPDATE print_files SET filaments_json=:filaments WHERE id=52'),
            {'filaments': json.dumps([{'slot': 2, 'type': 'PETG'}])})
        conn.execute(text("UPDATE filament_slots SET slot_number=2,filament_type='PETG' WHERE printer_id=59"))
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: REPORT)
    return engine, path


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('change', ['none', 'material', 'nozzle', 'job', 'ams_configuration'])
def test_h2d_dispatch_uploads_then_checks_exact_mapping(review_db, tmp_path, monkeypatch, explicit, protocol, change):
    from modules.printers import dispatch, dispatch_ams_gate as gate, dispatch_acquisition, dispatch_background as background
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    from core.durable_config import insert
    engine, path = setup_h2d(review_db, tmp_path, monkeypatch, mapping=[-1, 1] if explicit else None)
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda runtime: None)
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: protocol == 'v2')
    monkeypatch.setattr(dispatch.time, 'sleep', lambda *args: None)
    events = []
    def acquired(config, **options):
        events.append('fresh_read')
        assert options['selected_slots'] == (2,)
        assert engine.pool.checkedout() == 0
        snapshot = RawAMSSnapshot(options['operation_id'], 'c'*32, 1, time.monotonic(),
            options['configuration_key'], 1,
            (RawTrayFact(2, 0, 1, 'PLA' if change == 'material' else 'PETG', 'reported'),), True)
        return RuntimeBoundAMSResult(True, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)
    def uploaded(staged, remote):
        events.append('upload')
        assert Path(staged).read_bytes() == H2D.read_bytes()
        if change == 'nozzle':
            monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a:
                {'device': {'nozzle': {'info': [{'id': 1, 'type': 'HS01', 'diameter': .4}]}}})
        if change == 'ams_configuration':
            with engine.begin() as conn:
                conn.execute(text('UPDATE printers SET slot_count=1 WHERE id=59'))
        if change == 'job':
            with engine.begin() as conn:
                conn.execute(text("UPDATE jobs SET ams_mapping='[-1,0]' WHERE id=53"))
        return True
    def started(*args, **options):
        events.append('start')
        assert options == {'plate_num': 1, 'ams_mapping': [-1, 1]}
        return True
    if protocol == 'legacy':
        from modules.printers.adapters import bambu
        monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kwargs: SimpleNamespace(
            upload_file=uploaded, connect=lambda: True, start_print=started, disconnect=lambda: None))
    else:
        from modules.printers.telemetry.bambu import ftp_upload, session
        monkeypatch.setattr(ftp_upload, 'upload_file', lambda host, code, staged, remote: uploaded(staged, remote))
        monkeypatch.setattr(session, 'BambuCommandAdapter', lambda config: SimpleNamespace(
            start=lambda: None, is_connected=lambda: True, start_print=started, stop=lambda: None))
    # Use the same durable progress context as an HTTP dispatch worker.
    token = 'a' * 32
    from threading import Event
    marker = background._context.set({'token': token, 'staged': Event()})
    try:
        with engine.begin() as conn:
            insert(conn, background.progress_key(token), {'job_id': 53, 'printer_id': 59,
                'org_id': 1, 'attempt_id': token, 'phase': 'preparing'})
        result = dispatch.dispatch_job(59, 53)
        assert result[0] is (change == 'none'), result
        background.progress('start_accepted' if result[0] else 'refused', message=result[1])
    finally:
        background._context.reset(marker)
    assert events[0] == 'upload'
    assert events.count('start') == int(change == 'none')
    if change in ('none', 'material'):
        assert events[:2] == ['upload', 'fresh_read']
    if change == 'ams_configuration':
        assert 'admission_changed' in result[1]
    if change == 'nozzle':
        assert 'nozzle_mismatch' in result[1]
    with engine.connect() as conn:
        record = background.load(conn, background.result_key(53))
        assert record.get('ams_mapping_derived', False) is (not explicit)
        if change != 'job':
            assert conn.execute(text('SELECT ams_mapping FROM jobs WHERE id=53')).scalar_one() == ('[-1, 1]' if explicit else None)


@pytest.mark.parametrize('failure', ['source', 'material', 'slot', 'file_sources', 'plate', 'inspection_unavailable', 'preflight_unavailable'])
def test_unverifiable_ams_default_refuses_before_transport(review_db, tmp_path, monkeypatch, failure):
    from modules.printers import dispatch
    engine, path = setup_h2d(review_db, tmp_path, monkeypatch)
    if failure in ('inspection_unavailable', 'preflight_unavailable'):
        from modules.printers import services
        from modules.printers.dispatch_payload_snapshot import PayloadUnavailable
        original = services.inspect_ams_payload
        reads = []
        def unavailable(*args, **kwargs):
            reads.append(True)
            if failure == 'preflight_unavailable' and len(reads) % 2 == 1:
                return original(*args, **kwargs)
            raise PayloadUnavailable('Inspection snapshot unavailable')
        monkeypatch.setattr(services, 'inspect_ams_payload', unavailable)
    if failure == 'source':
        path.write_bytes(h2d_archive(command='T0 H-1'))
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET payload_sha256=:hash WHERE id=53'), {'hash': hashlib.sha256(path.read_bytes()).hexdigest()})
        if failure == 'material':
            conn.execute(text("UPDATE filament_slots SET filament_type='PLA' WHERE printer_id=59"))
        if failure == 'slot':
            conn.execute(text('DELETE FROM filament_slots WHERE printer_id=59'))
        if failure == 'file_sources':
            conn.execute(text('UPDATE print_files SET filaments_json=:filaments WHERE id=52'),
                {'filaments': json.dumps([{'slot': 1, 'type': 'PETG'}])})
    job = dispatch._load_job(53)
    # Plate selection itself remains authoritative; only source inspection falls back.
    if failure == 'plate':
        job['plate_index'] = None
    options, error = dispatch._validated_print_options(job, dispatch._get_printer_info(59), path,
        hashlib.sha256(path.read_bytes()).hexdigest())
    if failure == 'plate':
        assert error is not None
    else:
        assert options is None
        assert 'ams_mapping_required' in error
        monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda *a, **kw: pytest.fail('transport reached'))
        ok, message = dispatch.dispatch_job(59, 53)
        assert not ok and 'ams_mapping_required' in message


@pytest.mark.parametrize('model', ['X1C', 'A1', 'P1S', None])
def test_all_bambu_models_derive_verified_source(review_db, tmp_path, monkeypatch, model):
    from modules.printers import dispatch
    engine, path = setup_h2d(review_db, tmp_path, monkeypatch)
    creds = dispatch._get_printer_info(59)
    creds.update(machine_type=model, model=model)
    options, error = dispatch._validated_print_options(dispatch._load_job(53), creds, path,
        hashlib.sha256(path.read_bytes()).hexdigest())
    assert (options, error) == ({'plate_num': 1, 'ams_mapping': [-1, 1]}, None)


@pytest.mark.parametrize('mapping', [[1], [-1, 0], [-1, 1, -1]])
def test_derived_preflight_cannot_select_another_tray_or_vector(mapping):
    from modules.printers.dispatch_ams_gate import preflight_ams_source
    with pytest.raises(ValueError):
        preflight_ams_source({'ams_mapping': None, 'plate_index': 0}, mapping=mapping,
            staged_path=H2D, payload_sha256=hashlib.sha256(H2D.read_bytes()).hexdigest(), derived=True)
