"""Elegoo dispatch: Experimental opt-in on a Supported type, explicit routing, upload then start,
unknown outcomes, and that the controls that already shipped are never gated."""
import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from modules.printers import support_levels
from modules.printers.models import Printer
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_spool_slot_prevention import graph, review_db  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401
from tests.test_contracts.test_dispatch_final_admission_behavior import setup  # noqa: F401

HOST = '192.0.2.70'
SEND_REFUSAL = 'experimental_not_enabled: ' + support_levels.SEND_JOBS_MESSAGE


def opt_in(db, printer_id=59, enabled=True):
    support_levels.set_experimental_enabled(db, db.get(Printer, printer_id), enabled, 3)
    db.commit()


def test_table_keeps_elegoo_supported_and_gates_only_dispatch():
    assert support_levels.support_level('elegoo') == support_levels.SUPPORTED
    gated = support_levels.gated_capabilities('Elegoo')
    assert gated == {'dispatch'}   # resume and speed shipped before job sending: never newly gated
    assert not support_levels.gated_capabilities('bambu')
    assert support_levels.opt_in_applies('elegoo') and support_levels.opt_in_applies('octoprint')
    assert not support_levels.opt_in_applies('moonraker') and not support_levels.opt_in_applies(None)


def test_dispatch_refusal_for_a_supported_gated_type(graph):
    printer = graph.get(Printer, 9)
    printer.api_type = 'elegoo'
    graph.commit()
    assert support_levels.experimental_refusal(graph, 9, 'elegoo') is None  # monitoring/scheduling unchanged
    assert support_levels.dispatch_refusal(graph, 9, 'elegoo') == SEND_REFUSAL
    assert support_levels.command_refusal(printer) == SEND_REFUSAL
    for safety in ('pause', 'stop', 'plate-cleared', 'resume', 'speed'):
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
    assert dispatch.dispatch_job(59, 53) == (False, SEND_REFUSAL)

    opt_in(review_db)
    reached = []
    monkeypatch.setattr(dispatch_orchestration, 'dispatch_prepared',
                        lambda job, creds, *a, **k: reached.append(creds['api_type']) or (True, 'reached'))
    assert dispatch.dispatch_job(59, 53) == (True, 'reached') and reached == ['elegoo']

    opt_in(review_db, enabled=False)
    monkeypatch.setattr(dispatch_orchestration, 'dispatch_prepared', forbidden)
    assert dispatch.dispatch_job(59, 53) == (False, SEND_REFUSAL)


def test_elegoo_send_refusal_names_the_capability_not_the_printer_type(review_db, tmp_path, monkeypatch, token_client):
    from core.base import AlertType
    from core import job_timeline
    from modules.notifications.models import AlertPreference
    from modules.notifications import alert_dispatcher
    from modules.jobs.routes import jobs_lifecycle, dispatch_status
    from modules.printers import dispatch, dispatch_acquisition, maintenance_gate, smart_plug
    from modules.printers.dispatch_background import result_key
    from modules.printers.start_next import load

    notify = dispatch._notify_start_outcome
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='elegoo')
    expected = (
        "Sending jobs is Experimental on Elegoo printers and is off for this printer. "
        "A platform administrator can turn it on in this printer's Experimental features. "
        "Monitoring, pause, resume, speed and stop work without it."
    )
    assert support_levels.SEND_JOBS_MESSAGE == expected
    assert support_levels.experimental_capability_message(' Elegoo ', support_levels.DISPATCH) == expected

    def forbidden(*a, **kw):
        pytest.fail('A refused dispatch must not reach device I/O')
    monkeypatch.setattr(dispatch, '_dispatch_elegoo', forbidden)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', forbidden)
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', forbidden)
    assert dispatch.dispatch_job(59, 53) == (False, SEND_REFUSAL)
    assert support_levels.experimental_refusal_for_printer(review_db, 59) == SEND_REFUSAL
    assert maintenance_gate.experimental_refusal(engine, 59) == SEND_REFUSAL

    client, issue = token_client
    client.app.include_router(jobs_lifecycle.router)
    client.app.include_router(dispatch_status.router)
    headers = issue(['write:jobs', 'read:jobs'])
    for wait in ('true', 'false'):
        response = client.post(f'/jobs/53/dispatch?wait={wait}', headers=headers)
        assert response.status_code == 400, response.text
        assert response.json() == {'detail': SEND_REFUSAL}
        assert support_levels.EXPERIMENTAL_MESSAGE not in response.text
    record = load(review_db, result_key(53))
    response = client.get(f"/jobs/53/dispatch-status?attempt_id={record['attempt_id']}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['phase'] == 'refused'
    assert response.json()['message'] == SEND_REFUSAL
    assert support_levels.EXPERIMENTAL_MESSAGE not in response.text

    review_db.execute(text('UPDATE jobs SET submitted_by=3 WHERE id=53'))
    review_db.add(AlertPreference(user_id=3, alert_type=AlertType.QUEUE_FAILED_START,
                                 in_app=True, email=False, browser_push=False))
    review_db.commit()
    reports, events = [], []

    def report(job, reason, **kwargs):
        reports.append((reason, kwargs))
        notify(job, reason, **kwargs)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', report)
    monkeypatch.setattr(job_timeline, 'enqueue', events.append)
    monkeypatch.setattr(alert_dispatcher, 'deliver_staged_alert', lambda *a: None)
    job = dispatch._load_job(53)
    with maintenance_gate.capture_refusal() as captured, maintenance_gate.command_context(engine, job):
        with pytest.raises(maintenance_gate.ExperimentalNotEnabled) as error:
            dispatch._submit_print_command(forbidden)
    assert str(error.value) == SEND_REFUSAL
    assert captured == {'code': 'experimental_not_enabled'}
    assert len(reports) == 1
    reason, report = reports[0]
    assert reason == 'adapter_rejected'
    assert report['message'] == expected
    assert report['gate_refusal'].code == 'experimental_not_enabled'
    assert report['gate_refusal'].detail == expected
    alert = review_db.execute(text("SELECT message,metadata_json FROM alerts WHERE job_id=53 AND alert_type='queue_failed_start'")).one()
    assert alert.message == expected
    assert json.loads(alert.metadata_json)['gate_refusal_code'] == 'experimental_not_enabled'
    assert support_levels.EXPERIMENTAL_MESSAGE not in str(reports) + str(alert)
    assert len(events) == 1 and events[0]['code'] == 'EXPERIMENTAL_NOT_ENABLED'


def test_experimental_type_keeps_the_generic_sentence(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, maintenance_gate
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='octoprint')
    assert dispatch.dispatch_job(59, 53) == (False, support_levels.EXPERIMENTAL_REFUSAL)
    assert maintenance_gate.experimental_refusal(engine, 59) == support_levels.EXPERIMENTAL_REFUSAL
    reports = []
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: reports.append(k))
    with maintenance_gate.command_context(engine, {'id': 53, 'printer_id': 59}):
        with pytest.raises(maintenance_gate.ExperimentalNotEnabled) as error:
            maintenance_gate.check_command()
    assert str(error.value) == support_levels.EXPERIMENTAL_REFUSAL
    assert reports[0]['message'] == support_levels.EXPERIMENTAL_MESSAGE
    assert reports[0]['gate_refusal'].code == 'experimental_not_enabled'
    assert reports[0]['gate_refusal'].detail == support_levels.EXPERIMENTAL_MESSAGE
    # A whole Experimental type never receives the capability-only wording.
    monkeypatch.setitem(support_levels.SUPPORT_TABLE, 'elegoo', (support_levels.EXPERIMENTAL, 'test'))
    assert support_levels.experimental_capability_message('elegoo', support_levels.DISPATCH) == support_levels.EXPERIMENTAL_MESSAGE
    assert support_levels.dispatch_refusal(review_db, 59, 'ultimaker') == support_levels.MONITOR_ONLY_REFUSAL


def test_elegoo_with_sending_on_is_not_refused_by_this_gate(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, maintenance_gate
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='elegoo')
    opt_in(review_db)
    assert support_levels.dispatch_refusal(review_db, 59, 'elegoo') is None
    assert maintenance_gate.experimental_refusal(engine, 59) is None
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: pytest.fail('No refusal notification expected'))
    sent = []
    with maintenance_gate.command_context(engine, {'id': 53, 'printer_id': 59}):
        assert dispatch._submit_print_command(lambda: sent.append(True) or True) is True
    assert sent == [True]


@pytest.mark.parametrize('boundary', ['admit', 'recheck_admitted'])
def test_elegoo_admission_preserves_the_capability_sentence(setup, review_db, boundary):
    from modules.printers import dispatch_admission
    engine, _, inputs = setup
    review_db.execute(text("UPDATE printers SET api_type='elegoo' WHERE id=59"))
    review_db.commit()
    result = getattr(dispatch_admission, boundary)(engine, **inputs)
    assert not result.admitted and result.reason == SEND_REFUSAL


def test_experimental_refusal_sentences_are_owned_by_support_levels():
    from modules.printers.dispatch_support import S1_SENDING_OFF_DETAIL

    backend = Path(__file__).resolve().parents[2] / 'backend'
    offenders = []
    for path in backend.rglob('*.py'):
        if any(part.startswith('preserved') for part in path.parts) or path == Path(support_levels.__file__):
            continue
        tree = ast.parse(path.read_text())
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                value = node.value.lower()
                if 'experimental' in value and any(word in value for word in (' off', 'turn', 'until')):
                    offenders.append((str(path.relative_to(backend)), node.value))
    # Kobra S1 has a separate server-wide environment switch, not the per-printer opt-in.
    # Keep its existing sentence and require all per-printer refusal wording to be centralized.
    assert offenders == [('modules/printers/dispatch_support.py', S1_SENDING_OFF_DETAIL)]


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

def test_existing_controls_need_no_opt_in(graph, monkeypatch):
    from modules.printers import route_utils
    printer = graph.get(Printer, 9)
    monkeypatch.setattr(printer, 'api_type', 'elegoo', raising=False)
    sent = []
    monkeypatch.setattr(route_utils, '_elegoo_command', lambda p, action: sent.append(action) or True)
    monkeypatch.setattr(route_utils, '_bambu_command', lambda *a: pytest.fail('elegoo must never reach the Bambu fallback'))
    for safety in ('pause_print', 'stop_print', 'cancel_print', 'resume_print'):
        assert route_utils._send_printer_command(printer, safety) is True, safety
    assert sent == ['pause_print', 'stop_print', 'cancel_print', 'resume_print']


def test_a_printer_with_no_board_id_is_refused_before_any_upload(fake_elegoo, tmp_path):
    from modules.printers import dispatch
    log, _ = fake_elegoo
    for missing in ({**CREDS, 'api_key': ''}, {**CREDS, 'api_key': '  '}, {k: v for k, v in CREDS.items() if k != 'api_key'}):
        ok, message = dispatch._dispatch_elegoo(1, str(tmp_path / 'a.gcode'), 'a.gcode', missing)
        assert ok is False and 'no file or print command was sent' in message
    assert log == []
