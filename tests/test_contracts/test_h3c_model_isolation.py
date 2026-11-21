"""Rack telemetry must leave fixed-extruder Bambu printers on their existing paths."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import review_db
from tests.test_contracts.test_printer_offline_behavior import offline_store, v2_offline_monitor


def fixed_report(model):
    name = 'bambu-x1c-ams-swap' if model == 'X1C' else 'bambu-h2d-recovery'
    path = Path(__file__).parents[1] / 'fixtures/telemetry' / (name + '.jsonl')
    for line in path.read_text().splitlines():
        packet = json.loads(line)['payload'].get('print', {})
        if packet.get('ams', {}).get('ams') and packet.get('device', {}).get('nozzle'):
            nozzle = packet['device']['nozzle']
            if model == 'H2C':
                nozzle['info'].extend({'id': n, 'addr_id': n - 16} for n in range(16, 22))
                nozzle['exist'] |= sum(1 << n for n in range(16, 22))
            return {'device': {'nozzle': nozzle}, 'ams': packet['ams'],
                    'vir_slot': [{'id': '255', 'tray_type': 'PLA'}]}
    pytest.fail('Missing fixed-extruder telemetry fixture')


@pytest.mark.parametrize('model', ['H2D', 'X1C', 'H2C'])
def test_fixed_extruder_monitor_keeps_nozzles_and_syncs_ams(model, v2_offline_monitor, offline_store, monkeypatch):
    from modules.printers import nozzle_capability
    from modules.printers.monitors import nozzle_observations
    packet = fixed_report(model)
    monitor, _, runtime = v2_offline_monitor
    offline_store.execute(text('UPDATE printers SET model=:model WHERE id=199'), {'model': model})
    offline_store.commit()
    parse = nozzle_capability.installed_nozzles
    stamp = '2026-11-21T00:00:00+00:00'
    monkeypatch.setattr(nozzle_observations, 'installed_nozzles',
                        lambda *a, **kw: parse(*a, **kw, observed_at=stamp))
    monitor._last_heartbeat = runtime.time.time()
    monitor._on_status(SimpleNamespace(raw_data={'print': packet}))
    expected = parse(packet, dual=model != 'X1C', observed_at=stamp)
    assert list(monitor._pending_installed_nozzles.values()) == expected
    assert not getattr(monitor, '_pending_rack_filaments', {})
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {}}))
    stored = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one()
    # Exact serialized rows at 656f70be, with only the observation time fixed.
    if model == 'X1C':
        baseline = [dict(extruder=0, side='single', diameter=0.4, material='Hardened steel', wear=0.0,
                         present=True, type='HX01', observed_at=stamp)]
    else:
        baseline = [dict(extruder=i, side=side, diameter=0.4, flow='High Flow', material='Tungsten carbide',
                         wear=0.0, present=True, type='HH05', observed_at=stamp)
                    for i, side in enumerate(('right', 'left'))]
    assert stored == json.dumps(baseline)
    from core.base import slot_filament_value
    from modules.printers.telemetry.bambu.ams_identity import ams_trays_by_slot, reported_color
    trays = ams_trays_by_slot(packet['ams']['ams'])
    expected_slots = [(n, slot_filament_value(tray['tray_type']), reported_color(tray))
                      for n, tray in sorted(trays)]
    actual = offline_store.execute(text('SELECT slot_number,filament_type,color_hex FROM filament_slots '
                                        'WHERE printer_id=199 ORDER BY slot_number')).all()
    assert actual == expected_slots


@pytest.mark.parametrize('model', ['H2D', 'H2C'])
def test_partial_fixed_nozzle_before_heartbeat_replaces_pending_row(model, v2_offline_monitor, offline_store):
    monitor, _, runtime = v2_offline_monitor
    monitor._last_heartbeat = runtime.time.time()
    monitor._reported_model = model
    monitor._on_status(SimpleNamespace(raw_data={'print': fixed_report(model)}))
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {'device': {'nozzle': {'info': [{'id': 0, 'wear': 2}]}}}}))
    raw = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one()
    right = next(r for r in json.loads(raw) if r.get('extruder') == 0)
    assert right['wear'] == 2 and 'diameter' not in right


def test_external_slot_without_nozzle_report_is_never_queued(v2_offline_monitor):
    monitor, _, runtime = v2_offline_monitor
    monitor._last_heartbeat = runtime.time.time()
    monitor._on_status(SimpleNamespace(raw_data={'print': {'vir_slot': [
        {'id': str(n), 'tray_type': 'PLA'} for n in (254, 255)]}}))
    assert not getattr(monitor, '_pending_rack_filaments', {})


@pytest.mark.parametrize('payload', [
    {'pushing': {'command': 'pushall', 'sequence_id': '0', 'version': 1, 'push_target': 1}},
    {'info': {'command': 'get_version', 'sequence_id': '0', 'version': 1}},
])
def test_status_requests_skip_unavailable_database(payload, monkeypatch):
    from core import db
    from modules.printers.printer_models import stored_rack_refusal
    monkeypatch.setattr(db, 'engine', SimpleNamespace(connect=lambda: pytest.fail('status queried database')))
    assert stored_rack_refusal(199, payload=payload) is None


@pytest.mark.parametrize('raw', ['{', 'null', '[null, {}, 7]', '[{"extruder":0},{"nozzle_id":16},{"nozzle_id":17}]'])
def test_bad_or_mixed_stored_evidence_cannot_refuse(raw):
    from modules.printers.printer_models import rack_printer_refusal
    assert rack_printer_refusal(dict(model='P1S', installed_nozzles=raw)) is None


@pytest.mark.parametrize('model', ['P1S', 'H2D', 'X1C', 'rack'])
def test_slot_display_uses_parent_identity(model):
    from modules.printers.schemas import FilamentSlotResponse
    rack = model == 'rack'
    parent = SimpleNamespace(api_type=None if rack else 'bambu', model=model,
                             installed_nozzles=[{'nozzle_id': i, 'side': 'rack'} for i in (16, 17, 18)] if rack else [])
    data = SimpleNamespace(id=1, printer_id=199, slot_number=1, assigned_spool=None, remaining=None,
                           printer=parent, tray_observation=json.dumps({'id': 'tray', 'pending': True}))
    slot = FilamentSlotResponse.model_validate(data)
    assert (slot.observed_state, slot.spool_change_pending) == ((None, False) if rack else ('tray', True))


def test_h2c_mixed_report_leaves_commands_and_dispatch_available(offline_store, v2_offline_monitor, monkeypatch):
    from core import db
    from modules.printers.dispatch_support import dispatch_unsupported_reason
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    monitor, _, _ = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET model='H2C' WHERE id=199"))
    offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print': fixed_report('H2C')}))
    monkeypatch.setattr(db, 'engine', offline_store.get_bind())
    target = offline_store.execute(text('SELECT model,api_type,installed_nozzles FROM printers WHERE id=199')).mappings().one()
    assert dispatch_unsupported_reason(target) is None
    adapter = BambuCommandAdapter(BambuAdapterConfig('199', 'synthetic', monitor.ip, ''))
    adapter._client, adapter._connected = Mock(), True
    adapter._client.publish.return_value.rc = 0
    adapter._client.publish.return_value.is_published.return_value = True
    for send in (adapter.pause_print, adapter.stop_print, adapter.turn_light_on, adapter.refresh_ams_rfid, lambda: adapter.start_print('file.3mf')):
        assert send() is True
    assert adapter._client.publish.call_count == 5


def test_standalone_adapter_distinguishes_reused_host(offline_store, monkeypatch):
    from core import db
    from modules.printers.adapters.bambu import BambuPrinter
    monkeypatch.setattr(db, 'engine', offline_store.get_bind())
    offline_store.execute(text("UPDATE printers SET model='H3C',api_host='127.0.0.1',api_key='original|fixture' WHERE id=199"))
    offline_store.commit()
    for serial, allowed in [('original', False), ('replacement', True)]:
        sender = BambuPrinter('127.0.0.1', serial, '')
        sender._client, sender._connected = Mock(), True
        sender._client.publish.return_value.rc = 0
        sender._client.publish.return_value.is_published.return_value = True
        assert sender.pause_print() is allowed
        assert sender._client.publish.called is allowed


@pytest.mark.parametrize('direct', [True, False])
def test_command_config_keeps_session_label_and_row_id(monkeypatch, direct):
    from modules.printers import route_utils
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import session
    run = Mock(return_value=True)
    monkeypatch.setattr(session, 'run_command', run)
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: True)
    target = SimpleNamespace(id=199, api_host='127.0.0.1', model='P1S', api_key='synthetic|fixture')
    send = route_utils._bambu_command_direct if direct else route_utils._bambu_command_v2
    assert send(target, 'pause_print')
    config = run.call_args.args[0]
    assert config.printer_id == ('cmd-direct-199' if direct else 'cmd-199')
    assert config.printer_row_id == 199


@pytest.mark.parametrize('model', ['P1S', 'H3C'])
def test_dispatch_without_start_gate_uses_loaded_identity(model, monkeypatch):
    from modules.printers import dispatch
    from modules.printers.telemetry.bambu import session, ftp_upload
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(ftp_upload, 'upload_file', lambda *a: True)
    run = Mock(return_value=True)
    monkeypatch.setattr(session, 'run_command', run)
    ok, reason = dispatch._dispatch_bambu_v2(4, 'file', 'remote.3mf',
        dict(printer_id=199, ip='127.0.0.1', serial='synthetic', access_code='', model=model))
    if model == 'H3C':
        assert not ok and reason.startswith('unsupported_rack_printer:')
        run.assert_not_called()
    else:
        assert ok and 'before_command' not in run.call_args.kwargs
        assert run.call_args.args[0].printer_id == 'dispatch-synthetic'


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('change', ['fixed', 'rack_during_connect', 'rack_without_gate', 'fixed_without_store'])
def test_final_start_uses_existing_checks_and_reports_confirmed_refusal(protocol, change, offline_store, monkeypatch):
    from sqlalchemy import event
    from core import db
    from modules.printers import dispatch, maintenance_gate
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu import commands, ftp_upload

    engine = offline_store.get_bind()
    monkeypatch.setattr(db, 'engine', engine)
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch, 'time', SimpleNamespace(sleep=lambda _: None, time=lambda: 2000))
    monkeypatch.setattr(ftp_upload, 'upload_file', lambda *a: True)
    monkeypatch.setattr(BambuPrinter, 'upload_file', lambda *a: True)
    wire = Mock()
    wire.publish.return_value.rc = 0
    wire.publish.return_value.is_published.return_value = True

    def connect(sender):
        if change == 'fixed_without_store':
            def unavailable():
                raise RuntimeError('Synthetic store unavailable after connect')
            monkeypatch.setattr(db, 'engine', SimpleNamespace(connect=unavailable))
        elif change != 'fixed':
            offline_store.execute(text("UPDATE printers SET machine_type='H3C' WHERE id=199"))
            offline_store.commit()
        sender._client, sender._connected = wire, True
        return True

    monkeypatch.setattr(BambuPrinter, 'connect', connect)
    monkeypatch.setattr(BambuPrinter, 'disconnect', lambda self: None)
    monkeypatch.setattr(commands.BambuCommandAdapter, 'start', connect)
    monkeypatch.setattr(commands.BambuCommandAdapter, 'stop', lambda self: None)
    target = dict(printer_id=199, model='P1S', ip='127.0.0.1', serial='synthetic', access_code='')
    method = dispatch._dispatch_bambu_v2 if protocol == 'v2' else dispatch._dispatch_bambu_legacy
    gate = None if change in ('rack_without_gate', 'fixed_without_store') else maintenance_gate.start_gate(engine, 199)
    selects = []
    def count(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith('SELECT'):
            selects.append(statement)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        from contextlib import nullcontext
        context = (nullcontext() if change == 'fixed_without_store' else
                   maintenance_gate.command_context(engine, {'id': 1, 'printer_id': 199}))
        with context:
            ok, reason = method(1, 'file', 'remote.3mf', target, before_start=gate)
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    if change == 'fixed_without_store':
        assert ok and not selects
        wire.publish.assert_called_once()
    elif change == 'fixed':
        # At 656f70be: maintenance prepare, maintenance final, check_command's
        # maintenance and Experimental checks. No separate rack-only lookup.
        assert ok and len(selects) == 4
        wire.publish.assert_called_once()
    else:
        assert ok, reason
        wire.publish.assert_called_once()
        # The next admission sees the identity committed during the first connection.
        target.update(offline_store.execute(text(
            'SELECT model,machine_type FROM printers WHERE id=199')).mappings().one())
        ok, reason = method(2, 'file', 'next.3mf', target, before_start=gate)
        assert not ok and reason.startswith('unsupported_rack_printer:')
        assert isinstance(reason, dispatch.NoStartMessage)
        if gate is not None:
            assert gate.refusal.code == 'unsupported_rack_printer'
        wire.publish.assert_called_once()


def test_fixed_nozzle_presence_matches_baseline_for_signed_mask():
    from modules.printers.nozzle_capability import installed_nozzles
    packet = fixed_report('H2D')
    packet['device']['nozzle']['exist'] = -1
    assert [r['present'] for r in installed_nozzles(packet)] == [True, True]


def test_rack_only_printer_serializes_prefetched_slots_without_sql(offline_store):
    from sqlalchemy import event
    from modules.inventory.catalogue_slots import enrich_catalogue_slots
    from modules.printers.models import Printer
    from modules.printers.schemas import FilamentSlotResponse

    rows = [{'side': 'rack', 'nozzle_id': n} for n in (16, 17, 18)]
    offline_store.execute(text("UPDATE printers SET model='P1S', installed_nozzles=:rows WHERE id=199"),
                          {'rows': json.dumps(rows)})
    offline_store.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,tray_observation) "
                               "VALUES(199,:n,'empty',:obs)"),
                          [{'n': n, 'obs': json.dumps({'id': 'old-tray', 'pending': True})} for n in range(1, 10)])
    offline_store.commit()
    printer = offline_store.get(Printer, 199)
    enrich_catalogue_slots(offline_store, [printer], {'org_id': 1})
    engine = offline_store.get_bind()
    def unexpected(*args):
        pytest.fail('Slot display issued SQL after the route prefetched its parents')
    event.listen(engine, 'before_cursor_execute', unexpected)
    try:
        slots = [FilamentSlotResponse.model_validate(slot) for slot in printer.filament_slots]
    finally:
        event.remove(engine, 'before_cursor_execute', unexpected)
    assert len(slots) == 9
    assert all(slot.observed_state is None and not slot.spool_change_pending for slot in slots)


def test_dual_printer_with_only_primary_fallback_persists_empty_list(offline_store, v2_offline_monitor):
    monitor, _, _ = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET model='H2D',installed_nozzles=NULL WHERE id=199"))
    offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print': {'nozzle_diameter': '0.4'}}))
    assert offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one() == '[]'


def test_mixed_h2c_clears_unconfirmed_rack_filaments_without_resizing_slots(offline_store, v2_offline_monitor):
    from modules.printers.printer_models import rack_printer_refusal
    monitor, _, _ = v2_offline_monitor
    packet = fixed_report('H2C')
    mixed = packet['device']['nozzle']['info']
    only_rack = [r for r in mixed if r['id'] >= 16]
    monitor._on_status(SimpleNamespace(raw_data={'print': {
        'device': {'nozzle': {'info': only_rack}}, 'vir_slot': packet['vir_slot']}}))
    assert offline_store.execute(text('SELECT slot_count FROM printers WHERE id=199')).scalar_one() == 4
    monitor._on_status(SimpleNamespace(raw_data={'print': packet}))
    assert not getattr(monitor, '_pending_rack_filaments', {})
    assert set(monitor._pending_installed_nozzles) == {0, 1}
    current = offline_store.execute(text('SELECT model,installed_nozzles FROM printers WHERE id=199')).mappings().one()
    assert rack_printer_refusal(current) is None


@pytest.mark.parametrize('mask', [None, (1 << 16) | (1 << 20), -1])
def test_incomplete_rack_list_cannot_establish_identity(mask, offline_store, v2_offline_monitor):
    from modules.printers.printer_models import rack_printer_refusal
    monitor, _, _ = v2_offline_monitor
    nozzle = {'info': [{'id': n, 'addr_id': n - 16} for n in (16, 17, 18)]}
    if mask is not None:
        nozzle['exist'] = mask
    monitor._on_status(SimpleNamespace(raw_data={'print': {'device': {'nozzle': nozzle}}}))
    row = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).mappings().one()
    assert rack_printer_refusal(row) is None


def test_h2c_rack_delta_keeps_fixed_rows_and_pending_ams(offline_store, v2_offline_monitor, monkeypatch):
    from core import db
    from modules.printers.dispatch_support import dispatch_unsupported_reason
    from modules.printers.printer_models import stored_rack_refusal
    monitor, _, runtime = v2_offline_monitor
    packet = fixed_report('H2C')
    offline_store.execute(text("UPDATE printers SET machine_type='O1C2' WHERE id=199"))
    offline_store.commit()
    # Hold the full packet's fixed nozzles and AMS inventory until the delta's heartbeat.
    monitor._last_heartbeat = runtime.time.time()
    monitor._on_status(SimpleNamespace(raw_data={'print': packet}))
    expected = list(monitor._pending_installed_nozzles.values())
    delta = {'info': [r for r in packet['device']['nozzle']['info'] if r['id'] >= 16]}
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {'device': {'nozzle': delta}}}))
    row = offline_store.execute(text('SELECT model,machine_type,installed_nozzles,slot_count,api_type '
                                     'FROM printers WHERE id=199')).mappings().one()
    assert json.loads(row['installed_nozzles']) == expected
    from modules.printers.telemetry.bambu.ams_identity import ams_trays_by_slot
    trays = ams_trays_by_slot(packet['ams']['ams'])
    from core.base import slot_filament_value
    from modules.printers.telemetry.bambu.ams_identity import reported_color
    slots = offline_store.execute(text('SELECT slot_number,filament_type,color_hex FROM filament_slots '
                                       'WHERE printer_id=199 ORDER BY slot_number')).all()
    assert slots == [(n, slot_filament_value(tray['tray_type']), reported_color(tray)) for n, tray in sorted(trays)]
    assert row['slot_count'] == 4
    monkeypatch.setattr(db, 'engine', offline_store.get_bind())
    assert stored_rack_refusal(199) is None
    assert dispatch_unsupported_reason(row) is None


@pytest.mark.parametrize('source', ['product_name', 'printer_type'])
def test_identification_clears_previously_stored_rack(source, v2_offline_monitor, offline_store):
    from modules.printers.printer_models import rack_printer_refusal
    monitor, _, _ = v2_offline_monitor
    ids = (16, 17, 18)
    monitor._on_status(SimpleNamespace(raw_data={'print': {'device': {'nozzle': {
        'info': [{'id': i, 'addr_id': i - 16} for i in ids], 'exist': sum(1 << i for i in ids)}}}}))
    def stored():
        return offline_store.execute(text('SELECT model,machine_type,installed_nozzles FROM printers WHERE id=199')).mappings().one()
    assert rack_printer_refusal(stored())
    if source == 'product_name':
        monitor._on_product_name('Bambu Lab H2C', monitor._connection_generation)
    else:
        monitor._on_status(SimpleNamespace(raw_data={'print': {'printer_type': 'O1C2'}}))
    assert not rack_printer_refusal(stored())
    assert json.loads(stored()['installed_nozzles']) == []


@pytest.mark.parametrize('protocol', ['v2', 'legacy'])
def test_direct_rack_print_start_is_refused(protocol, offline_store, monkeypatch):
    from core import db
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    monkeypatch.setattr(db, 'engine', offline_store.get_bind())
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    sender = (BambuPrinter('127.0.0.1', 'synthetic', '', printer_id=199) if protocol == 'legacy'
              else BambuCommandAdapter(BambuAdapterConfig('199', 'synthetic', '127.0.0.1', '')))
    sender._client, sender._connected = Mock(), True
    sender._client.publish.return_value.rc = 0
    sender._client.publish.return_value.is_published.return_value = True
    assert not sender.start_print('file.3mf')
    sender._client.publish.assert_not_called()


def test_monitor_control_survives_identity_lookup_failure(v2_offline_monitor, monkeypatch):
    import time
    from core import db
    from modules.printers import monitor_commands
    monitor, _, _ = v2_offline_monitor
    monitor._v2_adapter._client, monitor._v2_adapter._connected = Mock(), True
    monitor._v2_adapter._client.publish.return_value.rc = 0
    monkeypatch.setattr(db, 'engine', Mock(connect=Mock(side_effect=RuntimeError('store unavailable'))))
    assert monitor_commands.execute({199: monitor}, dict(printer_id=199, action='light', on=True,
                                    deadline=time.monotonic() + 3)) == 'accepted'
    monitor._v2_adapter._client.publish.assert_called_once()


@pytest.mark.parametrize('model,expected_rack', [('Unidentified', True), ('P1S', False), ('O1C2', False)])
def test_only_unidentified_printer_uses_complete_rack_evidence(model, expected_rack, offline_store, v2_offline_monitor):
    from modules.printers.printer_models import is_h3c_printer
    monitor, _, _ = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET model=:model WHERE id=199"), {'model': model})
    offline_store.commit()
    ids = (16, 17, 18)
    monitor._on_status(SimpleNamespace(raw_data={'print': {'device': {'nozzle': {
        'info': [{'id': i, 'addr_id': i - 16} for i in ids], 'exist': sum(1 << i for i in ids)}}}}))
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {}}))
    row = offline_store.execute(text('SELECT model,installed_nozzles FROM printers WHERE id=199')).mappings().one()
    assert is_h3c_printer(row) is expected_rack
    positions = [n['position'] for n in json.loads(row['installed_nozzles'] or '[]') if n['side'] == 'rack']
    assert positions == ([1, 2, 3] if expected_rack else [])
