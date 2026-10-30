"""One bounded post-upload session supplies both nozzle and selected tray facts."""
from types import SimpleNamespace
import pytest
from modules.printers.nozzle_capability import installed_nozzles
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
from tests.test_contracts.test_dispatch_capability import REPORT
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from tests.test_contracts.test_ams_acquisition_session import CONFIG, KEY, INC, Client, arm, message


@pytest.mark.parametrize('combined,seconds,start_delay,code', [
    (False, 6, 0, 'snapshot_stale'), (True, 6, 0, None),
    (True, 10.1, 0, 'snapshot_stale'), (True, 6, 4, 'snapshot_stale'),
])
def test_one_slow_session_fits_original_window(monkeypatch, combined, seconds, start_delay, code):
    from modules.printers import dispatch_ams_gate as ams, dispatch_capability_gate as capability, dispatch_acquisition
    clock = [100.0]
    monkeypatch.setattr(capability.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(ams, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(ams, 'revalidate_runtime_identity', lambda runtime: None)
    monkeypatch.setattr(ams, 'preflight_ams_source', lambda *a, **kw: {'source_index': 0, 'material': 'PETG'})
    monkeypatch.setattr(ams, 'recheck_admitted', lambda *a, **kw: SimpleNamespace(admitted=True))
    config = SimpleNamespace(transport=CONFIG, configuration_key=KEY, owner_org_id=1)
    monkeypatch.setattr(ams, 'load_ams_configuration', lambda *a: config)
    monkeypatch.setattr(capability, 'inspect_payload', lambda *a: {'requirements': []})
    monkeypatch.setattr(capability, 'geometry_refusal', lambda *a: None)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    engine = SimpleNamespace(connect=Connection)
    target = SimpleNamespace(held=lambda: True)
    calls = []
    def nozzle(*a):
        calls.append('nozzle')
        clock[0] += seconds
        return REPORT
    def shared(*a, **kw):
        calls.append('ams')
        assert kw.get('nozzle_dual') is (False if combined else None)
        clock[0] += seconds
        snapshot = RawAMSSnapshot(INC, 'c'*32, 1, clock[0], KEY, 1,
            (RawTrayFact(1, 0, 0, 'PETG', 'reported'),), True,
            nozzles=tuple(installed_nozzles(REPORT)))
        return RuntimeBoundAMSResult(True, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(ams, 'read_current_selected_ams', shared)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', nozzle)
    job = {'id': 1, 'printer_id': 2, 'ams_mapping': [0]}
    creds = {'org_id': 1, 'model': 'X1C'}
    previous = ams.create_ams_start_gate(engine, admission_inputs={
        'job': job, 'creds': creds, 'token': INC, 'payload_sha256': 'e'*64},
        staged_path='synthetic', mapping=[0], target_handle=target)
    if not combined:
        del previous.prepare_report  # Reproduce the former sequential-read path.
    checked = capability.create_capability_gate(job=job, creds=creds, staged_path='synthetic',
        plate_number=1, target_handle=target, previous=previous)
    prepared = checked.prepare()
    clock[0] += start_delay
    assert (prepared and checked()) is (code is None)
    assert calls == (['ams'] if combined else ['nozzle', 'ams'])
    if code:
        assert checked.refusal.code == code
        assert f'report_read_ms={round(seconds * 1000)}' in checked.refusal.detail
    else:
        assert not checked()  # Neither gate can be reused or renew evidence.


@pytest.mark.parametrize('dual', [False, True])
def test_shared_controller_requires_same_packet_nozzle_and_selected_tray(dual):
    from modules.printers.telemetry.bambu.ams_acquisition_session import AMSSessionController
    controller = AMSSessionController(CONFIG, KEY, 1, [1], INC, allow_unreported=True, nozzle_dual=dual)
    client = Client(controller)
    controller.on_connect(client, None, {}, 0)
    arm(controller, client)
    status = {'command': 'push_status', 'gcode_state': 'IDLE', 'print_error': 0, 'hms': []}
    tray = {'ams': {'ams': [{'id': 0, 'tray': [{'id': 0, 'tray_type': 'PETG'}]}]}}
    message(controller, client, {**status, **REPORT})
    assert not controller.done.is_set()
    message(controller, client, {**status, **tray})
    assert not controller.done.is_set()  # Cannot reuse the earlier nozzle fields.
    message(controller, client, {**status, **REPORT, **tray})
    snapshot = controller.freeze()
    assert snapshot.idle_without_error and snapshot.trays[0].material == 'PETG'
    assert snapshot.nozzles[0]['diameter'] == .4


@pytest.mark.parametrize('change', ['retained', 'foreign', 'partial', 'busy'])
def test_shared_controller_preserves_collector_invalidation(change):
    from modules.printers.telemetry.bambu.ams_acquisition_session import AMSSessionController
    controller = AMSSessionController(CONFIG, KEY, 1, [1, 2], INC, allow_unreported=True, nozzle_dual=False)
    client = Client(controller)
    controller.on_connect(client, None, {}, 0)
    arm(controller, client)
    packet = {**REPORT, 'command': 'push_status', 'gcode_state': 'IDLE', 'print_error': 0, 'hms': [],
              'ams': {'ams': [{'id': 0, 'tray': [{'id': i, 'tray_type': 'PETG'} for i in (0, 1)]}]}}
    message(controller, client, packet)
    if change == 'partial':
        packet['ams']['ams'][0]['tray'].pop()
    elif change == 'busy':
        packet = {'gcode_state': 'RUNNING'}
    message(controller, client, packet, **({'retain': True} if change == 'retained' else
        {'topic': 'device/foreign/report'} if change == 'foreign' else {}))
    snapshot = controller.freeze()
    if change == 'busy':
        assert snapshot is not None and not snapshot.idle_without_error
    else:
        assert snapshot is None
