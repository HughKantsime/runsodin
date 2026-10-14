"""Real admitted SQLite attempt with staged source facts and simulated owned read."""
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_dispatch_final_admission_behavior import setup, review_db, education_inputs
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from modules.printers.dispatch_admission import admit
from modules.printers import dispatch
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult


@pytest.mark.parametrize('change', ['none', 'material', 'busy', 'expired', 'cleanup', 'target',
                                    'configuration', 'job', 'grant', 'claim', 'runtime', 'runtime_replaced', 'mapping_mismatch', 'mapping_mutated'])
def test_post_upload_gate_rechecks_same_attempt_after_read(setup, monkeypatch, change):
    from core import dispatch_claims
    from core.dispatch_identity import build_identity
    from modules.printers import dispatch_ams_gate as gate
    engine, provider, old = setup
    inputs = education_inputs(setup, monkeypatch)
    source = Path(__file__).parents[1] / 'fixtures/telemetry/a1-petg-source-sanitized.3mf'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert dispatch_claims.release_preparing(engine, inputs['token'])
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET plate_index=0,ams_mapping='[0]',payload_sha256=:sha,print_file_id=11 WHERE id=13"), {'sha': digest})
    job = dispatch._load_job(13); creds = dispatch._get_printer_info(9)
    identity = build_identity(creds, inputs['key'])
    claim = dispatch_claims.acquire(engine, 9, 13, identity.target_key, identity.configuration_key, digest, 1)
    inputs.update(token=claim.token, job=job, creds=creds, payload_sha256=digest)
    assert admit(engine, **inputs).admitted
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    replaced = [False]
    def runtime_valid(runtime):
        if replaced[0]: raise ValueError('Synthetic runtime replacement')
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', runtime_valid)
    original_recheck = gate.recheck_admitted
    checks = [0]
    def rechecked(*args, **kwargs):
        result = original_recheck(*args, **kwargs)
        checks[0] += 1
        if change == 'runtime_replaced' and checks[0] == 2: replaced[0] = True
        return result
    monkeypatch.setattr(gate, 'recheck_admitted', rechecked)
    held = [True]; events = []
    target = SimpleNamespace(held=lambda: held[0])
    def acquired(config, **options):
        events.append('owned_read')
        assert options['operation_id'] == inputs['token']
        assert options['selected_slots'] == (1,)
        assert engine.pool.checkedout() == 0
        statements = {
            'configuration': "UPDATE printers SET api_host='changed.example.test' WHERE id=9",
            'job': "UPDATE jobs SET hold=1 WHERE id=13",
            'grant': "UPDATE education_cost_center_printers SET state='revoked' WHERE printer_id=9",
            'claim': "UPDATE printer_dispatch_claims SET state='uncertain'",
        }
        if change in statements:
            with engine.begin() as conn: conn.execute(text(statements[change]))
        if change == 'target': held[0] = False
        snapshot = RawAMSSnapshot(inputs['token'], 'c'*32, 1,
            time.monotonic() - (20 if change == 'expired' else 0),
            options['configuration_key'], 1,
            (RawTrayFact(1, 0, 0, 'PLA' if change == 'material' else 'PETG', 'reported'),),
            idle_without_error=change != 'busy')
        return RuntimeBoundAMSResult(True, snapshot,
            'd'*32 if change == 'runtime' else RUNTIME.incarnation, RUNTIME.clock_domain,
            change != 'cleanup', None)
    monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)
    if change == 'mapping_mismatch':
        with pytest.raises(ValueError, match='differs from the admitted job'):
            gate.create_ams_start_gate(engine, admission_inputs=inputs, staged_path=str(source),
                                      mapping=[1], target_handle=target)
        assert events == []
        return
    mapping = [0]
    checked = gate.create_ams_start_gate(engine, admission_inputs=inputs, staged_path=str(source),
                                         mapping=mapping, target_handle=target)
    if change == 'mapping_mutated':
        mapping[0] = 1
    assert checked() is (change == 'none')
    expected_events = [] if change == 'mapping_mutated' else ['owned_read']
    assert events == expected_events
    assert checked() is False
    assert events == expected_events
    with engine.connect() as conn:
        assert conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one() in ('adapter_entered', 'uncertain')


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('material_matches', [True, False])
def test_actual_dispatch_upload_read_and_single_start_sequence(review_db, tmp_path, monkeypatch,
                                                               protocol, material_matches):
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    from modules.printers import dispatch_ams_gate as gate
    from modules.printers.telemetry import feature_flag
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    source = Path(__file__).parents[1] / 'fixtures/telemetry/a1-petg-source-sanitized.3mf'
    payload = source.read_bytes(); path.write_bytes(payload)
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET payload_sha256=:sha,ams_mapping='[0]' WHERE id=53"),
                     {'sha': hashlib.sha256(payload).hexdigest()})
        conn.execute(text("UPDATE print_files SET filaments_json='[{\"type\":\"PETG\"}]' WHERE id=52"))
        conn.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=59"))
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
            (RawTrayFact(1, 0, 0, 'PETG' if material_matches else 'PLA', 'reported'),), True)
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
        monkeypatch.setattr(session, 'run_command', started)
    result = dispatch.dispatch_job(59, 53)
    assert result[0] is material_matches
    assert events[:2] == ['upload', 'fresh_read']
    assert events.count('start') == (1 if material_matches else 0)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one() == (
            'accepted' if material_matches else 'uncertain')
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == (
            'printing' if material_matches else 'scheduled')



def test_readonly_preflight_accepts_actual_qualified_single_source_without_db_or_device():
    import hashlib
    from modules.printers.dispatch_ams_gate import preflight_ams_source
    source=Path(__file__).parents[1]/'fixtures/telemetry/a1-petg-source-sanitized.3mf'
    result=preflight_ams_source({'plate_index':0,'ams_mapping':[0]},mapping=[0],staged_path=str(source),payload_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    assert result['material']=='PETG'
