"""Elegoo dispatch: Experimental opt-in on a Supported type, explicit routing, upload then start,
unknown outcomes, and the resume gate (pause and stop never gated)."""
import pytest
from sqlalchemy import text

from modules.printers import support_levels
from modules.printers.models import Printer
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_spool_slot_prevention import graph, review_db  # noqa: F401

HOST = '192.0.2.70'


def opt_in(db, printer_id=59, enabled=True):
    support_levels.set_experimental_enabled(db, db.get(Printer, printer_id), enabled, 3)
    db.commit()


def test_table_keeps_elegoo_supported_and_gates_only_dispatch_resume_and_speed():
    assert support_levels.support_level('elegoo') == support_levels.SUPPORTED
    gated = support_levels.gated_capabilities('Elegoo')
    assert gated == {'dispatch', 'resume', 'speed'}
    assert 'pause' not in gated and 'stop' not in gated
    assert support_levels.gated_capabilities('bambu') == frozenset()
    assert support_levels.opt_in_applies('elegoo') and support_levels.opt_in_applies('octoprint')
    assert not support_levels.opt_in_applies('moonraker') and not support_levels.opt_in_applies(None)


def test_dispatch_refusal_for_a_supported_gated_type(graph):
    printer = graph.get(Printer, 9)
    printer.api_type = 'elegoo'
    graph.commit()
    assert support_levels.experimental_refusal(graph, 9, 'elegoo') is None  # monitoring/scheduling unchanged
    assert support_levels.dispatch_refusal(graph, 9, 'elegoo') == support_levels.EXPERIMENTAL_REFUSAL
    assert support_levels.control_refusal(graph, 9, 'elegoo', 'resume') == support_levels.EXPERIMENTAL_REFUSAL
    assert support_levels.control_refusal(graph, 9, 'elegoo', 'speed') == support_levels.EXPERIMENTAL_REFUSAL
    for safety in ('pause', 'stop', 'plate-cleared'):
        assert support_levels.control_refusal(graph, 9, 'elegoo', safety) is None
    assert support_levels.enabled_for_printer(printer) is False
    support_levels.set_experimental_enabled(graph, printer, True, 3)
    graph.commit()
    assert support_levels.dispatch_refusal(graph, 9, 'elegoo') is None
    assert support_levels.control_refusal(graph, 9, 'elegoo', 'resume') is None
    assert support_levels.enabled_for_printer(printer) is True
    # an opt-in never carries over to another type
    assert support_levels.dispatch_refusal(graph, 9, 'moonraker') is None
    printer.api_type = 'moonraker'
    graph.commit()
    assert support_levels.enabled_for_printer(printer) is False
    printer.api_type = 'elegoo'
    graph.commit()
    assert support_levels.enabled_for_printer(printer) is True


def test_ungated_types_are_untouched_by_dispatch_refusal(graph):
    for api_type in ('bambu', 'moonraker', 'prusalink', 'anycubic_lan'):
        assert support_levels.dispatch_refusal(graph, 9, api_type) is None
    assert support_levels.dispatch_refusal(graph, 9, None) is None
    assert support_levels.dispatch_refusal(graph, 9, 'made_up') == support_levels.UNSUPPORTED_REFUSAL


def test_dispatch_refused_without_opt_in_and_reaches_the_pipeline_with_it(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, dispatch_acquisition, dispatch_orchestration, smart_plug
    configure_dispatch(review_db, tmp_path, monkeypatch, protocol='elegoo')
    review_db.execute(text("UPDATE jobs SET status='pending' WHERE id=53")); review_db.commit()

    def forbidden(*a, **kw):
        pytest.fail('An unopted Elegoo printer must not reach device I/O')
    monkeypatch.setattr(dispatch, '_dispatch_elegoo', forbidden)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', forbidden)
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', forbidden)
    assert dispatch.dispatch_job(59, 53) == (False, support_levels.EXPERIMENTAL_REFUSAL)

    opt_in(review_db)
    reached = []
    monkeypatch.setattr(dispatch_orchestration, 'dispatch_prepared',
                        lambda job, creds, *a, **k: reached.append(creds['api_type']) or (True, 'reached'))
    assert dispatch.dispatch_job(59, 53) == (True, 'reached') and reached == ['elegoo']

    opt_in(review_db, enabled=False)
    monkeypatch.setattr(dispatch_orchestration, 'dispatch_prepared', forbidden)
    assert dispatch.dispatch_job(59, 53) == (False, support_levels.EXPERIMENTAL_REFUSAL)


def test_final_command_boundary_refuses_when_the_opt_in_is_switched_off(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from modules.printers.maintenance_gate import ExperimentalNotEnabled, command_context
    configure_dispatch(review_db, tmp_path, monkeypatch, protocol='elegoo')
    sent = []
    with command_context(review_db.get_bind(), {'id': 53, 'printer_id': 59}):
        with pytest.raises(ExperimentalNotEnabled):
            dispatch._submit_print_command(lambda: sent.append(1) or True)
        opt_in(review_db)
        assert dispatch._submit_print_command(lambda: sent.append(1) or True) is True
    assert sent == [1]


@pytest.mark.parametrize('loader', ['dispatch_loading', 'dispatch_records'])
def test_mainboard_and_default_port_load_from_the_stored_credential(review_db, tmp_path, monkeypatch, loader):
    import importlib
    from core import crypto
    configure_dispatch(review_db, tmp_path, monkeypatch, protocol='elegoo')
    module = importlib.import_module(f'modules.printers.{loader}')
    review_db.execute(text('UPDATE printers SET api_key=:k, api_host=:h WHERE id=59'),
                      {'k': crypto.encrypt('MB-123'), 'h': HOST}); review_db.commit()
    info = module._get_printer_info(59)
    assert (info['api_type'], info['ip'], info['port'], info['api_key']) == ('elegoo', HOST, 3030, 'MB-123')
    review_db.execute(text('UPDATE printers SET api_key=:k, api_host=:h WHERE id=59'), {'k': 'PLAIN-ID', 'h': HOST + ':3031'}); review_db.commit()
    info = module._get_printer_info(59)
    assert (info['port'], info['api_key']) == (3031, 'PLAIN-ID')
    # Another type keeps its own default (the Elegoo default of 3030 does not leak).
    review_db.execute(text("UPDATE printers SET api_type='moonraker', api_host=:h WHERE id=59"), {'h': HOST}); review_db.commit()
    assert module._get_printer_info(59)['port'] == 80


def test_an_elegoo_identity_and_sender_status_inputs_are_accepted():
    from core.dispatch_identity import build_identity
    from core.sender_operation_protocol import _status_inputs
    base = {'api_type': 'elegoo', 'ip': HOST, 'port': 3030, 'api_key': 'MB-123', 'is_active': True}
    assert build_identity(base, b'k' * 32) is not None
    assert build_identity({**base, 'api_type': 'anycubic_lan'}, b'k' * 32) is None
    _status_inputs({'api_type': 'elegoo', 'ip': HOST, 'port': 3030, 'api_key': 'MB-123'})


NAMES = ('bambu', 'moonraker', 'prusalink', 'octoprint', 'elegoo')


@pytest.mark.parametrize('api_type', NAMES)
def test_routing_is_explicit_per_type(monkeypatch, api_type):
    from modules.printers import dispatch
    calls = []
    for name in NAMES:
        monkeypatch.setattr(dispatch, f'_dispatch_{name}',
                            (lambda n: lambda *a, **k: calls.append(n) or (False, 'stub'))(name))
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: None)
    job = {'id': 1, 'printer_id': 2, 'item_name': 'x'}
    assert dispatch._run_admitted_dispatch(job, {'api_type': api_type, 'ip': HOST}, '/tmp/x.gcode', 'x.gcode', {}, None, None) == (False, 'stub')
    assert calls == [api_type]


def test_an_unlisted_type_reaches_no_adapter(monkeypatch):
    from modules.printers import dispatch
    for name in NAMES:
        monkeypatch.setattr(dispatch, f'_dispatch_{name}', lambda *a, **k: pytest.fail('no adapter for an unknown type'))
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: None)
    ok, message = dispatch._run_admitted_dispatch({'id': 1, 'printer_id': 2, 'item_name': 'x'},
                                                  {'api_type': 'anycubic_lan', 'ip': HOST}, '/tmp/x.gcode', 'x.gcode', {}, None, None)
    assert ok is False and 'not supported' in message


# ---- _dispatch_elegoo ----

CREDS = {'ip': HOST, 'port': 3030, 'api_key': 'MB-123'}


@pytest.fixture
def fake_elegoo(monkeypatch):
    from modules.printers import dispatch
    from modules.printers.adapters import elegoo as adapter
    log = []
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr('modules.printers.maintenance_gate.check_command', lambda: None)
    state = {'upload': True, 'connect': True, 'start': True}

    def run(name, key, *flag):
        def call(self, *a, **k):
            log.append(name)
            outcome = state[key]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return call
    monkeypatch.setattr(adapter.ElegooPrinter, 'upload_file', run('upload', 'upload'))
    monkeypatch.setattr(adapter.ElegooPrinter, 'connect', run('connect', 'connect'))
    monkeypatch.setattr(adapter.ElegooPrinter, 'start_print', run('start', 'start'))
    monkeypatch.setattr(adapter.ElegooPrinter, 'disconnect', lambda self: log.append('disconnect'))
    return log, state


def test_dispatch_uploads_then_gates_then_starts(fake_elegoo, tmp_path):
    from modules.printers import dispatch
    log, _ = fake_elegoo
    gate = lambda: log.append('gate') or True
    assert dispatch._dispatch_elegoo(1, str(tmp_path / 'a.gcode'), 'a.gcode', CREDS, before_start=gate) == (True, 'Print started')
    assert log == ['upload', 'gate', 'connect', 'start', 'disconnect']


def test_dispatch_refuses_non_gcode_before_any_device_io(fake_elegoo, tmp_path):
    from modules.printers import dispatch
    log, _ = fake_elegoo
    for name in ('a.3mf', 'a.bgcode'):
        ok, message = dispatch._dispatch_elegoo(1, str(tmp_path / name), name, CREDS)
        assert ok is False and '.gcode' in message and 'no print command was sent' in message
    assert log == []


def test_gate_refusal_after_the_upload_sends_no_start(fake_elegoo, tmp_path):
    from modules.printers import dispatch
    log, _ = fake_elegoo
    ok, message = dispatch._dispatch_elegoo(1, str(tmp_path / 'a.gcode'), 'a.gcode', CREDS, before_start=lambda: False)
    assert ok is False and 'no print command was sent' in message and log == ['upload']


def test_a_refused_upload_is_a_clean_no_start(fake_elegoo, tmp_path):
    from modules.printers import dispatch
    log, state = fake_elegoo
    state['upload'] = False
    ok, message = dispatch._dispatch_elegoo(1, str(tmp_path / 'a.gcode'), 'a.gcode', CREDS)
    assert ok is False and 'no print command was sent' in message and log == ['upload']


def test_an_upload_timeout_is_an_unknown_outcome(fake_elegoo, tmp_path):
    from modules.printers import dispatch
    from modules.printers.adapters.elegoo import ElegooOutcomeUnknown
    log, state = fake_elegoo
    state['upload'] = ElegooOutcomeUnknown('no answer')
    with pytest.raises(dispatch.DispatchOutcomeUncertain):
        dispatch._dispatch_elegoo(1, str(tmp_path / 'a.gcode'), 'a.gcode', CREDS)
    assert log == ['upload']


def test_no_websocket_after_a_good_upload_is_a_clean_no_start(fake_elegoo, tmp_path):
    from modules.printers import dispatch
    log, state = fake_elegoo
    state['connect'] = False
    ok, message = dispatch._dispatch_elegoo(1, str(tmp_path / 'a.gcode'), 'a.gcode', CREDS)
    assert ok is False and 'no print command was sent' in message and log == ['upload', 'connect', 'disconnect']


def test_a_refused_or_unanswered_start_takes_the_shared_uncertain_outcome_and_always_disconnects(fake_elegoo, tmp_path):
    from modules.printers import dispatch
    from modules.printers.adapters.elegoo import ElegooOutcomeUnknown
    log, state = fake_elegoo
    state['start'] = False
    with pytest.raises(dispatch.DispatchOutcomeUncertain):
        dispatch._dispatch_elegoo(1, str(tmp_path / 'a.gcode'), 'a.gcode', CREDS)
    assert log[-1] == 'disconnect'
    log.clear()
    state['start'] = ElegooOutcomeUnknown('timeout')
    with pytest.raises(dispatch.DispatchOutcomeUncertain):
        dispatch._dispatch_elegoo(1, str(tmp_path / 'a.gcode'), 'a.gcode', CREDS)
    assert log == ['upload', 'connect', 'start', 'disconnect']


# ---- controls ----

def test_resume_needs_the_opt_in_but_pause_and_stop_never_do(graph, monkeypatch):
    from modules.printers import route_utils
    printer = graph.get(Printer, 9)
    monkeypatch.setattr(printer, 'api_type', 'elegoo', raising=False)
    sent = []
    monkeypatch.setattr(route_utils, '_elegoo_command', lambda p, action: sent.append(action) or True)
    monkeypatch.setattr(route_utils, '_bambu_command', lambda *a: pytest.fail('elegoo must never reach the Bambu fallback'))
    assert route_utils._send_printer_command(printer, 'resume_print') is False
    for safety in ('pause_print', 'stop_print', 'cancel_print'):
        assert route_utils._send_printer_command(printer, safety) is True, safety
    support_levels.set_experimental_enabled(graph, printer, True, 3)
    graph.commit()
    assert route_utils._send_printer_command(printer, 'resume_print') is True
    assert sent == ['pause_print', 'stop_print', 'cancel_print', 'resume_print']
