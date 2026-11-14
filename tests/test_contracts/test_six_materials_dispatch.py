"""Real ordinary dispatch, final material gates and restrictive durable reads; no hardware."""
import hashlib
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from modules.printers import dispatch
from modules.printers.nozzle_capability import installed_nozzles
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_six_materials import SIX, NEGATIVES

@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('material', SIX)
def test_six_actual_dispatch_without_durable_evidence(review_db, tmp_path, monkeypatch,
                                                               protocol, material):
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    from modules.printers import dispatch_ams_gate as gate
    from modules.printers.telemetry import feature_flag
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    source = Path(__file__).parents[1] / 'fixtures/telemetry/a1-petg-source-sanitized.3mf'
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(path, 'w') as changed:
        for name in original.namelist():
            changed.writestr(name, original.read(name).replace(b'PETG', material.encode()))
    payload = path.read_bytes()
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET payload_sha256=:sha,ams_mapping='[0]' WHERE id=53"),
                     {'sha': hashlib.sha256(payload).hexdigest()})
        conn.execute(text("UPDATE print_files SET filaments_json=:facts WHERE id=52"), {"facts": json.dumps([{ "slot":1, "type":material}])})
        conn.execute(text("UPDATE filament_slots SET filament_type=:material WHERE printer_id=59"), {"material":material.replace("-", "_")})
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda runtime: None)
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: protocol == 'v2')
    monkeypatch.setattr(dispatch.time, 'sleep', lambda *args: None)
    events = []
    def acquired(config, **options):
        events.append('fresh_read')
        assert events[0] == 'upload'
        assert engine.pool.checkedout() == 0
        snapshot = RawAMSSnapshot(options['operation_id'], 'c'*32, 1, time.monotonic(),
            options['configuration_key'], 1,
            (RawTrayFact(1, 0, 0, material, 'reported'),), True, nozzles=tuple(installed_nozzles(
                {'nozzle_type': 'hardened_steel', 'nozzle_diameter': .4})))
        return RuntimeBoundAMSResult(True, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)
    def uploaded(staged, remote):
        events.append('upload')
        assert Path(staged) != path and Path(staged).read_bytes() == payload
        return True
    def started(*args, **options):
        events.append('start')
        assert options['ams_mapping'] == [0]
        assert 'before_start' not in options
        return True
    if protocol == 'legacy':
        from modules.printers.adapters import bambu
        monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kwargs: SimpleNamespace(
            upload_file=uploaded, connect=lambda: True, start_print=started,
            disconnect=lambda: events.append('cleanup')))
    else:
        from modules.printers.telemetry.bambu import ftp_upload, session
        monkeypatch.setattr(ftp_upload, 'upload_file', lambda host, code, staged, remote: uploaded(staged, remote))
        monkeypatch.setattr(session, 'BambuCommandAdapter', lambda config:SimpleNamespace(
            start=lambda:None, is_connected=lambda:True, start_print=started, stop=lambda:None))
    from modules.printers.telemetry.bambu import ams_evidence_transactions
    monkeypatch.setattr(ams_evidence_transactions, '_persist_report', lambda *a, **k: pytest.fail('Ordinary dispatch must not write durable AMS evidence'))
    result = dispatch.dispatch_job(59, 53)
    assert result[0] is True, result
    assert events[:2] == ['upload', 'fresh_read']
    assert events.count('start') == 1
    with engine.connect() as conn:
        assert conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one_or_none() == 'accepted'
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == 'printing'

        assert conn.execute(text('SELECT count(*) FROM ams_report_trays')).scalar_one() == 0


def test_bambu_final_gate_exact_and_cross_family_both_directions(review_db, monkeypatch):
    from modules.printers import dispatch_ams_gate as gate
    engine = review_db.get_bind()
    configuration = SimpleNamespace(owner_org_id=1, configuration_key='b'*64, transport=SimpleNamespace())
    monkeypatch.setattr(gate, 'load_ams_configuration', lambda *a: configuration)
    monkeypatch.setattr(gate, 'recheck_admitted', lambda *a, **k: SimpleNamespace(admitted=True))
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda _: None)
    for source, reported in [(m,m) for m in SIX] + NEGATIVES:
        def acquired(*a, **k):
            snapshot = RawAMSSnapshot('a'*32, 'c'*32, 1, time.monotonic(), 'b'*64, 1,
                (RawTrayFact(1,0,0,reported,'reported'),), True)
            return RuntimeBoundAMSResult(True,snapshot,RUNTIME.incarnation,RUNTIME.clock_domain,True,None)
        monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)
        checked = gate.create_ams_start_gate(engine,
            admission_inputs=dict(job={'printer_id':9,'ams_mapping':[0]}, creds={'org_id':1}, token='a'*32),
            staged_path='unused', mapping=[0], target_handle=SimpleNamespace(held=lambda:True),
            source_facts={'sources':[{'source_index':0,'material':source}]})
        assert checked() is (source == reported), (source,reported)
        if source != reported: assert checked.refusal.code == 'material_mismatch'
