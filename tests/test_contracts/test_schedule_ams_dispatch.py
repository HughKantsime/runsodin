"""A job admitted by the schedule HTTP route reaches the real AMS dispatch gate."""
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME


@pytest.mark.parametrize('source_id', [1, 4])
@pytest.mark.parametrize('matches', [True, False])
def test_scheduled_mapping_runs_post_upload_material_gate(creators, review_db, tmp_path, monkeypatch, matches, caplog, source_id):
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    from tests.support.loopback_devices import allow_loopback_devices
    from modules.models_library.routes.print_files import router
    from modules.printers import dispatch, dispatch_ams_gate as gate
    from modules.printers.adapters import bambu
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    allow_loopback_devices(monkeypatch)
    engine, source = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    payload = (Path(__file__).parents[1] / 'fixtures/telemetry/a1-petg-source-sanitized.3mf').read_bytes()
    source.write_bytes(payload)
    if source_id == 4:
        from tests.test_contracts.test_ams_file_source_ids import source_file
        source_file(source)
        payload = source.read_bytes()
    review_db.execute(text("UPDATE print_files SET filaments_json='[{\"slot\": 1,\"type\":\"PETG\"}]' WHERE id=52"))
    import json
    review_db.execute(text('UPDATE print_files SET filaments_json=:f WHERE id=52'),
                      {'f':json.dumps([{'slot':source_id,'type':'PETG'}])})
    review_db.execute(text("UPDATE filament_slots SET filament_type='PETG',slot_number=4 WHERE printer_id=59"))
    review_db.commit()
    client, auth, _, _ = creators
    client.app.include_router(router, prefix='/api')
    response = client.post('/api/print-files/52/schedule?printer_id=59', headers=auth(),
                           json={'ams_mapping': [{'source_slot': source_id, 'target_slot': 4}]})
    assert response.status_code == 200, response.text
    job = response.json()['job_id']
    # Simulate scheduler placement only; the production dispatch and gate remain real.
    review_db.execute(text("UPDATE jobs SET status='scheduled' WHERE id=:id"), {'id': job}); review_db.commit()
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: False)
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda runtime: None)
    monkeypatch.setattr(dispatch.time, 'sleep', lambda *args: None)
    events = []
    def acquired(config, **options):
        events.append('fresh_read')
        assert events[0] == 'upload'
        assert options['selected_slots'] == (4,)
        snapshot = RawAMSSnapshot(options['operation_id'], 'c'*32, 1, time.monotonic(),
            options['configuration_key'], 1,
            (RawTrayFact(4, 0, 3, 'PETG' if matches else 'PLA', 'reported'),), True)
        return RuntimeBoundAMSResult(True, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)
    def uploaded(staged, remote):
        events.append('upload')
        assert Path(staged).read_bytes() == payload
        return True
    def started(*args, **options):
        events.append('start')
        assert options['ams_mapping'] == [-1] * (source_id - 1) + [3]
        return True
    monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kwargs: SimpleNamespace(
        upload_file=uploaded, connect=lambda: True, start_print=started, disconnect=lambda: None))
    result = dispatch.dispatch_job(59, job)
    assert result[0] is matches, result
    assert events[:2] == ['upload', 'fresh_read']
    assert events.count('start') == int(matches)
    if not matches:
        assert any('code=material_mismatch' in record.message for record in caplog.records)
