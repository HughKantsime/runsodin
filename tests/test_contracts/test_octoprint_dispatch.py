"""OctoPrint dispatch: refused without the Experimental opt-in, allowed with it, explicit routing
(never the PrusaLink fallthrough), the gate before the single upload request."""
import pytest
from sqlalchemy import text

from modules.printers import support_levels
from modules.printers.models import Printer
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_spool_slot_prevention import graph, review_db  # noqa: F401

HOST = '192.0.2.60'


def opt_in(db, printer_id=59, enabled=True):
    support_levels.set_experimental_enabled(db, db.get(Printer, printer_id), enabled, 3)
    db.commit()


def test_octoprint_is_experimental_with_its_source():
    assert support_levels.support_level('octoprint') == support_levels.EXPERIMENTAL
    assert 'docs.octoprint.org' in support_levels.support_source('octoprint')


def test_dispatch_refused_without_opt_in_and_reaches_the_pipeline_with_it(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, dispatch_acquisition, dispatch_orchestration, smart_plug
    configure_dispatch(review_db, tmp_path, monkeypatch, protocol='octoprint')
    review_db.execute(text("UPDATE jobs SET status='pending' WHERE id=53")); review_db.commit()

    def forbidden(*a, **kw):
        pytest.fail('An unopted Experimental printer must not reach device I/O')
    monkeypatch.setattr(dispatch, '_dispatch_octoprint', forbidden)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', forbidden)
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', forbidden)
    assert dispatch.dispatch_job(59, 53) == (False, support_levels.EXPERIMENTAL_REFUSAL)

    opt_in(review_db)
    reached = []
    monkeypatch.setattr(dispatch_orchestration, 'dispatch_prepared',
                        lambda job, creds, *a, **k: reached.append(creds['api_type']) or (True, 'reached'))
    result = dispatch.dispatch_job(59, 53)
    assert reached == ['octoprint'], result
    assert result == (True, 'reached')

    opt_in(review_db, enabled=False)
    monkeypatch.setattr(dispatch_orchestration, 'dispatch_prepared', forbidden)
    assert dispatch.dispatch_job(59, 53) == (False, support_levels.EXPERIMENTAL_REFUSAL)


def test_application_key_loads_from_the_stored_credential(review_db, tmp_path, monkeypatch):
    from core import crypto
    from modules.printers import dispatch
    configure_dispatch(review_db, tmp_path, monkeypatch, protocol='octoprint')
    review_db.execute(text('UPDATE printers SET api_key=:k, api_host=:h WHERE id=59'),
                      {'k': crypto.encrypt('synthetic-application-key'), 'h': HOST + ':5000'})
    review_db.commit()
    info = dispatch._get_printer_info(59)
    assert (info['api_type'], info['ip'], info['port'], info['api_key']) == ('octoprint', HOST, 5000, 'synthetic-application-key')
    assert 'username' not in info and 'password' not in info


@pytest.mark.parametrize('api_type', ['octoprint', 'prusalink', 'moonraker', 'bambu'])
def test_routing_is_explicit_per_type(monkeypatch, api_type):
    from modules.printers import dispatch
    calls = []
    for name in ('bambu', 'moonraker', 'prusalink', 'octoprint'):
        monkeypatch.setattr(dispatch, f'_dispatch_{name}',
                            (lambda n: lambda *a, **k: calls.append(n) or (False, 'stub'))(name))
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: None)
    job = {'id': 1, 'printer_id': 2, 'item_name': 'x'}
    result = dispatch._run_admitted_dispatch(job, {'api_type': api_type, 'ip': HOST}, '/tmp/x.gcode', 'x.gcode', {}, None, None)
    assert result == (False, 'stub') and calls == [api_type]


def test_an_unknown_type_reaches_no_adapter(monkeypatch):
    from modules.printers import dispatch
    for name in ('bambu', 'moonraker', 'prusalink', 'octoprint'):
        monkeypatch.setattr(dispatch, f'_dispatch_{name}', lambda *a, **k: pytest.fail('no adapter for an unknown type'))
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: None)
    job = {'id': 1, 'printer_id': 2, 'item_name': 'x'}
    ok, message = dispatch._run_admitted_dispatch(job, {'api_type': 'made_up', 'ip': HOST}, '/tmp/x.gcode', 'x.gcode', {}, None, None)
    assert ok is False and 'not supported' in message


def test_octoprint_dispatch_runs_the_gate_before_the_single_upload_request(monkeypatch, tmp_path):
    from modules.printers import dispatch
    from modules.printers.adapters import octoprint as adapter
    order = []
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch, '_submit_print_command', lambda operation, *a, **k: operation(*a, **k) is True)
    monkeypatch.setattr(adapter.OctoPrintPrinter, 'upload_and_print',
                        lambda self, path, name=None: order.append(('upload', self.base_url, name)) or True)
    creds = {'ip': HOST, 'port': 5000, 'api_key': 'k'}
    ok = dispatch._dispatch_octoprint(1, str(tmp_path / 'a.gcode'), 'a.gcode', creds,
                                      before_start=lambda: order.append('gate') or True)
    assert ok == (True, 'Print started')
    assert order == ['gate', ('upload', f'http://{HOST}:5000', 'a.gcode')]

    order.clear()
    ok, message = dispatch._dispatch_octoprint(1, str(tmp_path / 'a.gcode'), 'a.gcode', creds, before_start=lambda: False)
    assert ok is False and 'no print command was sent' in message and order == []


def test_octoprint_dispatch_refuses_non_gcode_and_unconfirmed_uploads(monkeypatch, tmp_path):
    from modules.printers import dispatch
    from modules.printers.adapters import octoprint as adapter
    uploads = []
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr(dispatch, '_submit_print_command', lambda operation, *a, **k: operation(*a, **k) is True)
    monkeypatch.setattr(adapter.OctoPrintPrinter, 'upload_and_print', lambda self, path, name=None: uploads.append(name) or False)
    creds = {'ip': HOST, 'port': 80, 'api_key': 'k'}
    for name in ('a.3mf', 'a.bgcode'):
        ok, message = dispatch._dispatch_octoprint(1, str(tmp_path / name), name, creds)
        assert ok is False and '.gcode' in message
    assert uploads == []
    ok, message = dispatch._dispatch_octoprint(1, str(tmp_path / 'a.gcode'), 'a.gcode', creds)
    assert ok is False and 'not confirmed' in message and uploads == ['a.gcode']


def test_octoprint_upload_with_no_answer_takes_the_shared_unconfirmed_outcome(monkeypatch, tmp_path):
    from modules.printers import dispatch
    from modules.printers.adapters import octoprint as adapter
    monkeypatch.setattr(dispatch, '_ws', lambda *a: None)
    monkeypatch.setattr('modules.printers.maintenance_gate.check_command', lambda: None)

    def no_answer(self, path, name=None):
        raise adapter.OctoPrintOutcomeUnknown('no answer')
    monkeypatch.setattr(adapter.OctoPrintPrinter, 'upload_and_print', no_answer)
    with pytest.raises(dispatch.DispatchOutcomeUncertain):
        dispatch._dispatch_octoprint(1, str(tmp_path / 'a.gcode'), 'a.gcode',
                                     {'ip': HOST, 'port': 80, 'api_key': 'k'})


def test_resume_needs_the_opt_in_but_pause_and_stop_never_do(graph, monkeypatch):
    from modules.printers import route_utils
    printer = graph.get(Printer, 9)
    monkeypatch.setattr(printer, 'api_type', 'octoprint', raising=False)
    sent = []
    monkeypatch.setattr(route_utils, '_octoprint_command', lambda p, action: sent.append(action) or True)
    monkeypatch.setattr(route_utils, '_bambu_command', lambda *a: pytest.fail('octoprint must never reach the Bambu fallback'))
    assert route_utils._send_printer_command(printer, 'resume_print') is False
    for safety in ('pause_print', 'stop_print', 'cancel_print'):
        assert route_utils._send_printer_command(printer, safety) is True, safety
    support_levels.set_experimental_enabled(graph, printer, True, 3)
    graph.commit()
    assert route_utils._send_printer_command(printer, 'resume_print') is True
    assert sent == ['pause_print', 'stop_print', 'cancel_print', 'resume_print']
