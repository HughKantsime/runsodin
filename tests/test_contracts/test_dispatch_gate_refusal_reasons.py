"""Refusal diagnostics preserve fail-closed decisions without exposing source values."""
from contextlib import nullcontext
from types import SimpleNamespace as NS
import time
import pytest
from modules.printers import dispatch_ams_gate as gate
from modules.printers import dispatch


@pytest.fixture
def gate_case(monkeypatch):
    configuration = NS(owner_org_id=1, transport=None, configuration_key='private-fingerprint')
    state = NS(mapping=[0], held=True, admitted=True, runtime=True, present=configuration)
    snapshot = NS(incarnation='private-token', configuration_key='private-fingerprint', owner_org_id=1,
                  idle_without_error=True, received_monotonic=time.monotonic(),
                  trays=[NS(slot_number=1, material_state='reported', material='PETG', mechanically_ready=None)])
    result = NS(success=True, cleanup_confirmed=True, runtime_incarnation='runtime', clock_domain='clock', snapshot=snapshot)
    monkeypatch.setattr(gate, 'preflight_ams_source', lambda *a, **k: {'material': 'PETG', 'source_index': 0})
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: NS(incarnation='runtime', clock_domain='clock'))
    def revalidate(*a):
        if not state.runtime: raise ValueError('private-access-code private-serial private-token private-fingerprint')
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', revalidate)
    monkeypatch.setattr(gate, 'load_ams_configuration', lambda *a: state.present)
    monkeypatch.setattr(gate, 'recheck_admitted', lambda *a, **k: NS(admitted=state.admitted))
    monkeypatch.setattr(gate, 'read_current_selected_ams', lambda *a, **k: result)
    checked = gate.create_ams_start_gate(NS(connect=lambda: nullcontext(None)),
        admission_inputs={'job': {'printer_id': 9, 'ams_mapping': [0]}, 'creds': {'org_id': 1, 'access_code': 'private-access-code',
        'serial': 'private-serial'}, 'payload_sha256': '', 'token': 'private-token'},
        staged_path='', mapping=state.mapping, target_handle=NS(held=lambda: state.held))
    return checked, state, result, snapshot


@pytest.mark.parametrize('code', [
    'mapping_changed', 'target_lock_lost', 'runtime_changed', 'admission_changed',
    'ams_configuration_changed', 'ams_owner_changed', 'ams_read_failed', 'ams_read_cleanup_unconfirmed',
    'ams_read_runtime_mismatch', 'snapshot_missing', 'snapshot_identity_mismatch',
    'printer_not_idle_or_error', 'tray_count', 'tray_slot_mismatch', 'tray_material_unreported',
    'material_mismatch', 'tray_not_mechanically_ready', 'snapshot_stale', 'already_used', 'exception:ValueError'])
def test_each_refusal_is_retained_and_safe(gate_case, monkeypatch, code):
    checked, state, result, snapshot = gate_case
    changes = {
        'mapping_changed': lambda: state.mapping.append(1),
        'target_lock_lost': lambda: setattr(state, 'held', False),
        'runtime_changed': lambda: setattr(state, 'runtime', False),
        'admission_changed': lambda: setattr(state, 'admitted', False),
        'ams_configuration_changed': lambda: setattr(state, 'present', NS(owner_org_id=1)),
        'ams_owner_changed': lambda: setattr(state, 'present', NS(owner_org_id=2)),
        'ams_read_failed': lambda: setattr(result, 'success', False),
        'ams_read_cleanup_unconfirmed': lambda: setattr(result, 'cleanup_confirmed', False),
        'ams_read_runtime_mismatch': lambda: setattr(result, 'clock_domain', 'other'),
        'snapshot_missing': lambda: setattr(result, 'snapshot', None),
        'snapshot_identity_mismatch': lambda: setattr(snapshot, 'incarnation', 'other'),
        'printer_not_idle_or_error': lambda: setattr(snapshot, 'idle_without_error', False),
        'tray_count': lambda: setattr(snapshot, 'trays', []),
        'tray_slot_mismatch': lambda: setattr(snapshot.trays[0], 'slot_number', 2),
        'tray_material_unreported': lambda: setattr(snapshot.trays[0], 'material_state', 'unknown'),
        'material_mismatch': lambda: setattr(snapshot.trays[0], 'material', 'private-serial'),
        'tray_not_mechanically_ready': lambda: setattr(snapshot.trays[0], 'mechanically_ready', False),
        'snapshot_stale': lambda: setattr(snapshot, 'received_monotonic', time.monotonic()-20),
        'already_used': lambda: checked(),
        'exception:ValueError': lambda: monkeypatch.setattr(gate, 'read_current_selected_ams',
            lambda *a, **k: (_ for _ in ()).throw(ValueError('private-access-code private-serial private-token private-fingerprint'))),
    }
    changes[code]()
    assert checked() is False
    assert checked.refusal.code == code
    first = checked.refusal
    assert checked.prepare() is False
    assert checked.refusal is first
    message = dispatch._gate_refusal_message(checked)
    assert code in message
    for secret in ('private-access-code', 'private-serial', 'private-token', 'private-fingerprint'):
        assert secret not in message


def test_monitor_warning_throttle_survives_report_reset():
    from modules.printers.monitors.mqtt_printer import PrinterMonitor
    monitor = PrinterMonitor.__new__(PrinterMonitor)
    assert monitor._ams_sync_warning_due('same', 100)
    monitor._reset_pending_ams()
    assert not monitor._ams_sync_warning_due('same', 110)
    assert monitor._ams_sync_warning_due('changed', 111)
    assert not monitor._ams_sync_warning_due('changed', 170)
    assert monitor._ams_sync_warning_due('changed', 171)


@pytest.mark.parametrize('reason', ['contradictory', 'unavailable', 'changed', 'ambiguous'])
def test_inventory_failure_paths_include_local_ids(monkeypatch, reason):
    from modules.printers.monitors import ams_inventory as inventory
    class Rows:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows
        def fetchone(self): return None
    class Connection:
        def execute(self, query, args=()):
            if query.startswith('SELECT id,assigned_spool_id,printer_id,slot_number'):
                return Rows([(7, 8, 9, 1)])
            return Rows([])
    conn = Connection()
    if reason in ('contradictory', 'unavailable'):
        monkeypatch.setattr(inventory, '_rows', lambda *a, **k:
            [(8, 1, 9, 1000, 500, 1)] if reason == 'contradictory' else [])
        call = lambda: inventory._graph(conn, 9, [])
    else:
        from modules.printers.telemetry.bambu import ams_identity
        trays = [(1, {'tag_uid': 'private-token', 'tray_type': 'PLA'})]
        if reason == 'ambiguous': trays *= 2
        monkeypatch.setattr(ams_identity, 'ams_trays_by_slot', lambda *a, **k: trays)
        monkeypatch.setattr(ams_identity, 'is_rfid_tag', lambda *a: True)
        graphs = iter([({7: (8, 9, 1)}, {8: (9, 1)}, {}, {}), ({}, {}, {}, {})])  # _graph also returns stale-location repairs
        monkeypatch.setattr(inventory, '_graph', lambda *a: next(graphs))
        monkeypatch.setattr(inventory, '_rows', lambda conn, table, *a, **k:
            [(9, 1)] if table == 'printers' else [(8, 1, 9, 1000, 500, 1)])
        call = lambda: inventory.sync_ams_inventory(conn, 9, [])
    with pytest.raises(inventory.AMSInventoryError) as error:
        call()
    message = str(error.value)
    assert reason in message and 'printer_id=9' in message
    assert ('slot_numbers=[1' in message if reason == 'ambiguous' else 'spool_ids=[8]' in message)
    assert 'private-token' not in message


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
def test_final_command_boundary_refusal_has_code(monkeypatch, protocol):
    from modules.printers.telemetry.bambu import ftp_upload, session
    from modules.printers.adapters import bambu
    from modules.printers.telemetry.bambu import broker_policy
    def checked():
        return gate.record_refusal(checked, 'target_lock_lost', 'Printer target lock is no longer held')
    checked.prepare = lambda: True
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(ftp_upload, 'upload_file', lambda *a: True)
    monkeypatch.setattr(broker_policy, 'resolve_bambu_broker_config', lambda *a, **k:
                        NS(host='localhost', port=8883, use_tls=True))
    commands = []
    monkeypatch.setattr(session, 'run_command', lambda *a, **k: k['before_command']())
    monkeypatch.setattr(bambu, 'BambuPrinter', lambda **k: NS(upload_file=lambda *a: True,
        connect=lambda: True, start_print=lambda *a, **k: commands.append(True), disconnect=lambda: None))
    method = dispatch._dispatch_bambu_v2 if protocol == 'v2' else dispatch._dispatch_bambu_legacy
    creds = {'ip': 'localhost', 'serial': 'private-serial', 'access_code': 'private-access-code'}
    if protocol == 'v2':
        with pytest.raises(dispatch.DispatchOutcomeUncertain) as error:
            method(1, '', 'inert.3mf', creds, before_start=checked)
        message = str(error.value)
    else:
        ok, message = method(1, '', 'inert.3mf', creds, before_start=checked)
        assert not ok
    assert 'target_lock_lost' in message
    assert not commands
    assert 'private-serial' not in message and 'private-access-code' not in message


def test_dispatch_wrapper_exceptions_have_safe_reasons():
    def checked(): raise RuntimeError('private-token')
    assert dispatch._post_upload_gate(checked) is False
    assert checked.refusal.code == 'exception:RuntimeError'
    checked.prepare = lambda: (_ for _ in ()).throw(ValueError('private-serial'))
    assert dispatch._prepare_post_upload_gate(checked) is False
    assert checked.refusal.code == 'exception:RuntimeError'
    assert 'private-' not in dispatch._gate_refusal_message(checked)
    def refused(): return False
    assert 'gate_rejected' in dispatch._gate_refusal_message(refused)


@pytest.mark.parametrize('kind', ['bound', 'slots', 'builtin'])
def test_non_attribute_gate_clean_refusal(monkeypatch, kind):
    from modules.printers.adapters import bambu
    class Gate:
        __slots__ = ()
        def __call__(self): return False
        def refuse(self): return False
    obj = Gate()
    checked = {'bound': obj.refuse, 'slots': obj, 'builtin': bool}[kind]
    assert gate.record_refusal(checked, 'example', 'safe') is False
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(bambu, 'BambuPrinter', lambda **k: NS(upload_file=lambda *a: True,
        connect=lambda: True, start_print=lambda *a, **k: pytest.fail('command sent'), disconnect=lambda: None))
    ok, message = dispatch._dispatch_bambu_legacy(1, '', 'inert.3mf',
        {'ip': 'localhost', 'serial': 'private-serial', 'access_code': 'private-access-code'}, before_start=checked)
    assert ok is False
    assert 'gate_rejected' in message
    outcomes = []
    monkeypatch.setattr(dispatch, '_dispatch_bambu', dispatch._dispatch_bambu_legacy)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda job, outcome, **k: outcomes.append(outcome))
    ok, message = dispatch._run_admitted_dispatch(
        {'id': 1, 'printer_id': 9, 'item_name': 'test'},
        {'api_type': 'bambu', 'ip': 'localhost', 'serial': 'private-serial', 'access_code': 'private-access-code'},
        '', 'inert.3mf', {}, None, None, before_start=checked)
    assert not ok and 'gate_rejected' in message
    assert 'reconciliation' not in message and outcomes == ['adapter_rejected']


def test_v2_not_ok_final_refusal(monkeypatch):
    from modules.printers.telemetry.bambu import ftp_upload, broker_policy
    def checked():
        return gate.record_refusal(checked, 'target_lock_lost', 'Printer target lock is no longer held')
    checked.prepare = lambda: True
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(ftp_upload, 'upload_file', lambda *a: True)
    monkeypatch.setattr(broker_policy, 'resolve_bambu_broker_config', lambda *a, **k:
        NS(host='localhost', port=8883, use_tls=True))
    monkeypatch.setattr(dispatch, '_submit_print_command', lambda *a, **k: k['before_command']())
    ok, message = dispatch._dispatch_bambu_v2(1, '', 'inert.3mf',
        {'ip': 'localhost', 'serial': 'private-serial', 'access_code': 'private-access-code'}, before_start=checked)
    assert not ok and 'target_lock_lost' in message


@pytest.mark.parametrize('change,expected', [
    (lambda s: setattr(s.trays[0], 'material', 'PLA'), 'Reported material=PLA; required material=PETG'),
    (lambda s: setattr(s.trays[0], 'slot_number', 2), 'Reported slot=2; expected slot=1'),
    (lambda s: setattr(s, 'trays', []), 'count=0'),
    (lambda s: setattr(s, 'received_monotonic', float('nan')), 'age seconds=invalid'),
    (lambda s: setattr(s, 'received_monotonic', 80), 'age seconds=20.0'),
])
def test_actionable_details(gate_case, monkeypatch, change, expected):
    checked, _, _, snapshot = gate_case
    monkeypatch.setattr(gate.time, 'monotonic', lambda: 100)
    change(snapshot)
    assert checked() is False
    assert expected in checked.refusal.detail


def test_printer_observation_detail(gate_case):
    checked, _, _, snapshot = gate_case
    snapshot.idle_without_error = False
    snapshot.gcode_state = 'RUNNING'
    snapshot.print_error = 42
    snapshot.active_hms_codes = ('00000001_00000002',)
    assert checked() is False
    assert 'gcode_state=RUNNING; print_error=42' in checked.refusal.detail
    assert '00000001_00000002' in checked.refusal.detail
