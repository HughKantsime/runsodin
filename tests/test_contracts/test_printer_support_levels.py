"""Printer support levels: the table, the superadmin opt-in, and fail-closed enforcement.

OctoPrint is the only Experimental type. Tests mark another by monkeypatching
support_levels.SUPPORT_TABLE (TEST-ONLY; there is no production flag).
"""
import re
from pathlib import Path

import pytest
from fastapi import Depends
from sqlalchemy import text

from modules.printers import support_levels
from modules.printers.models import Printer
from tests.test_contracts.test_spool_slot_prevention import graph, review_db, USER
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_dispatch_final_admission_behavior import setup
from tests.test_contracts.dispatch_test_support import configure_dispatch

ROOT = Path(__file__).resolve().parents[2]
# Refused without the opt-in. pause and stop are safety actions and are never refused (SAFETY_ACTIONS).
CONTROL_ACTIONS = ['resume', 'speed', 'fan', 'clear-errors', 'skip-objects', 'lights']
SAFETY_ACTIONS = ['pause', 'stop']
CONTROL_BODIES = {'speed': {'speed': 2}, 'fan': {'fan': 'part_cooling', 'speed': 10}, 'skip-objects': {'object_ids': [0]}}


def experimental(monkeypatch, api_type):
    monkeypatch.setitem(support_levels.SUPPORT_TABLE, api_type, (support_levels.EXPERIMENTAL, 'test-only entry'))


def opt_in(db, printer_id=9, enabled=True):
    support_levels.set_experimental_enabled(db, db.get(Printer, printer_id), enabled, 3)
    db.commit()


# ---- the table ----

def test_table_levels_sources_and_current_types():
    assert all(level in support_levels.LEVELS and source for level, source in support_levels.SUPPORT_TABLE.values())
    assert {t: v[0] for t, v in support_levels.SUPPORT_TABLE.items()} == {
        'bambu': 'supported', 'moonraker': 'supported', 'prusalink': 'supported',
        'elegoo': 'supported', 'anycubic_lan': 'supported', 'octoprint': 'experimental'}


def test_unknown_manual_and_case_handling():
    assert support_levels.support_level(None) is None and support_levels.support_level('  ') is None
    assert support_levels.support_level('Bambu') == 'supported'
    assert support_levels.support_level('made_up') == 'unsupported'


def test_frontend_table_matches_backend():
    source = (ROOT / 'frontend/src/utils/supportLevels.ts').read_text()
    block = re.search(r'export const SUPPORT_LEVELS[^=]*= \{(.*?)\n\}', source, re.S).group(1)
    frontend = dict(re.findall(r"^\s*(\w+): '(\w+)',?\s*$", block, re.M))
    assert frontend == {t: v[0] for t, v in support_levels.SUPPORT_TABLE.items()}


# ---- the store ----

def test_supported_types_are_never_refused():
    assert support_levels.experimental_refusal(None, 9, 'moonraker') is None
    assert support_levels.experimental_refusal(None, 9, None) is None


def test_opt_in_store_fails_closed(graph, monkeypatch):
    experimental(monkeypatch, 'bambu')
    assert support_levels.experimental_refusal(graph, 9, 'bambu') == support_levels.EXPERIMENTAL_REFUSAL
    opt_in(graph)
    assert support_levels.experimental_refusal(graph, 9, 'bambu') is None
    assert support_levels.experimental_refusal(graph, 9, 'moonraker') is None  # not experimental
    # an opt-in recorded for one type never covers another type
    experimental(monkeypatch, 'moonraker')
    assert support_levels.experimental_refusal(graph, 9, 'moonraker') == support_levels.EXPERIMENTAL_REFUSAL
    # a malformed store enables nothing
    graph.execute(text("UPDATE system_config SET value='[1]' WHERE key=:k"), {'k': support_levels.CONFIG_KEY}); graph.commit()
    assert support_levels.experimental_refusal(graph, 9, 'bambu') == support_levels.EXPERIMENTAL_REFUSAL
    opt_in(graph, enabled=True)
    assert support_levels.experimental_refusal(graph, 9, 'bambu') is None
    opt_in(graph, enabled=False)
    assert support_levels.experimental_refusal(graph, 9, 'bambu') == support_levels.EXPERIMENTAL_REFUSAL


def test_deleting_a_printer_drops_its_opt_in(graph, monkeypatch):
    from modules.printers import routes_experimental  # noqa: F401  (registers the delete listener)
    experimental(monkeypatch, 'bambu')
    printer = Printer(name='Disposable experimental printer', org_id=1, api_type='bambu')
    graph.add(printer); graph.commit()
    pid = printer.id
    opt_in(graph, pid)
    graph.delete(printer); graph.commit()
    raw = graph.execute(text('SELECT value FROM system_config WHERE key=:k'), {'k': support_levels.CONFIG_KEY}).scalar_one()
    assert str(pid) not in (raw if isinstance(raw, dict) else __import__('json').loads(raw))


def test_forget_printer_locks_shared_store_before_reading(graph):
    """Compile the real read for PostgreSQL; exercise its mutation on local SQLite."""
    from sqlalchemy.dialects import postgresql

    opt_in(graph, 9)
    opt_in(graph, 10)
    reads = []
    connection = graph.connection()

    class CheckedConnection:
        def execute(self, statement, *args, **kwargs):
            compiled = str(statement.compile(dialect=postgresql.dialect()))
            if compiled.lstrip().upper().startswith('SELECT'):
                reads.append(compiled)
                assert 'FOR UPDATE' in compiled, 'Deletion must lock before reading the shared opt-in store'
            return connection.execute(statement, *args, **kwargs)

    support_levels.forget_printer(CheckedConnection(), 9)
    assert len(reads) == 1
    assert not support_levels.experimental_enabled(graph, 9, 'bambu')
    assert support_levels.experimental_enabled(graph, 10, 'bambu')


# ---- printer list and detail ----

def test_printer_list_and_detail_report_level_and_opt_in(graph, monkeypatch):
    from modules.printers.routes_crud import list_printers, get_printer
    from modules.printers.schemas import PrinterResponse
    graph.execute(text("UPDATE printers SET shared=FALSE,tags='[]',timelapse_enabled=FALSE,slot_count=COALESCE(slot_count,4) WHERE id=9")); graph.commit()

    def views():
        row = next(p for p in list_printers(current_user=USER, db=graph) if p.id == 9)
        return PrinterResponse.model_validate(row), PrinterResponse.model_validate(get_printer(9, USER, USER, graph))

    for item in views():
        assert (item.support_level, item.experimental_actions_enabled) == ('supported', False)
        assert item.model_dump()['support_level'] == 'supported'
    experimental(monkeypatch, 'bambu')
    for item in views():
        assert (item.support_level, item.experimental_actions_enabled) == ('experimental', False)
    opt_in(graph)
    for item in views():
        assert (item.support_level, item.experimental_actions_enabled) == ('experimental', True)


# ---- dispatch ----

def test_dispatch_job_refuses_before_any_device_step_then_proceeds_after_opt_in(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, dispatch_acquisition, smart_plug, dispatch_orchestration
    configure_dispatch(review_db, tmp_path, monkeypatch)
    review_db.execute(text("UPDATE jobs SET status='pending' WHERE id=53")); review_db.commit()
    experimental(monkeypatch, 'moonraker')

    def forbidden(*a, **kw):
        pytest.fail('An unopted Experimental printer must not reach device I/O')
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', forbidden)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', forbidden)
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', forbidden)
    assert dispatch.dispatch_job(59, 53) == (False, support_levels.EXPERIMENTAL_REFUSAL)
    assert 'experimental_not_enabled' in support_levels.EXPERIMENTAL_REFUSAL
    opt_in(review_db, 59)
    monkeypatch.setattr(dispatch_orchestration, 'dispatch_prepared', lambda *a, **k: (True, 'reached'))
    assert dispatch.dispatch_job(59, 53) != (False, support_levels.EXPERIMENTAL_REFUSAL)


def test_admission_refuses_then_admits_and_recheck_refuses_after_revocation(setup, review_db, monkeypatch):
    from modules.printers.dispatch_admission import admit, recheck_admitted
    engine, provider, inputs = setup
    experimental(monkeypatch, 'moonraker')
    refused = admit(engine, **inputs)
    assert not refused.admitted and refused.reason == support_levels.EXPERIMENTAL_REFUSAL
    opt_in(review_db, 59)
    assert admit(engine, **inputs).admitted
    opt_in(review_db, 59, enabled=False)
    rechecked = recheck_admitted(engine, **inputs)
    assert not rechecked.admitted and rechecked.reason.startswith('experimental_not_enabled')


def test_final_command_boundary_rechecks_the_opt_in(setup, review_db, monkeypatch):
    from modules.printers import dispatch
    from modules.printers.maintenance_gate import command_context, ExperimentalNotEnabled
    engine, provider, inputs = setup
    experimental(monkeypatch, 'moonraker')
    sent, reports = [], []
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: reports.append(k))
    with command_context(engine, inputs['job']):
        with pytest.raises(ExperimentalNotEnabled):
            dispatch._submit_print_command(lambda: sent.append(1) or True)
        assert sent == [] and reports[0]['gate_refusal'].code == 'experimental_not_enabled'
        opt_in(review_db, 59)
        assert dispatch._submit_print_command(lambda: sent.append(1) or True) and sent == [1]


def test_internal_command_helper_refuses_without_opt_in_but_never_a_safety_command(graph, monkeypatch):
    from modules.printers import route_utils
    experimental(monkeypatch, 'bambu')
    monkeypatch.setattr(route_utils, '_bambu_command', lambda printer, action: True)
    printer = graph.get(Printer, 9)
    assert route_utils._send_printer_command(printer, 'resume_print') is False
    for safety in ('pause_print', 'stop_print', 'cancel_print'):  # Vigil auto-pause, operator stop
        assert route_utils._send_printer_command(printer, safety) is True, safety
    opt_in(graph)
    assert route_utils._send_printer_command(printer, 'resume_print') is True


# ---- HTTP: controls, opt-in route, monitoring ----

@pytest.fixture
def experimental_app(token_client, review_db, monkeypatch):
    from modules.printers import routes_controls, routes_experimental, routes_crud
    experimental(monkeypatch, 'bambu')
    review_db.execute(text("UPDATE printers SET shared=FALSE,tags='[]',timelapse_enabled=FALSE,slot_count=COALESCE(slot_count,4) WHERE id=9")); review_db.commit()
    client, issue = token_client
    # Same wiring as modules/printers/routes.py (asserted separately below).
    client.app.include_router(routes_controls.router, dependencies=[Depends(routes_experimental.experimental_controls_guard)])
    client.app.include_router(routes_experimental.router)
    client.app.include_router(routes_crud.router)
    sent = []
    monkeypatch.setattr(routes_controls, '_send_printer_command', lambda printer, action: sent.append(action) or True)
    monkeypatch.setattr(routes_controls, '_bambu_command_direct', lambda printer, name, *a, **k: sent.append(name) or True)
    return client, issue(['admin', 'write:printers', 'read:printers']), sent


def make_superadmin(db):
    db.execute(text('UPDATE users SET group_id=NULL WHERE id=3')); db.commit()


@pytest.mark.parametrize('action', CONTROL_ACTIONS)
def test_each_control_is_refused_without_opt_in(experimental_app, action):
    client, headers, sent = experimental_app
    response = client.post(f'/printers/9/{action}', headers=headers, json=CONTROL_BODIES.get(action))
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'experimental_not_enabled'
    assert 'Monitoring still works' in response.json()['detail']['message']
    assert sent == []


@pytest.mark.parametrize('action', SAFETY_ACTIONS)
def test_pause_and_stop_always_work_on_an_experimental_printer(experimental_app, action):
    client, headers, sent = experimental_app
    response = client.post(f'/printers/9/{action}', headers=headers, json=CONTROL_BODIES.get(action))
    assert response.status_code == 200, response.text
    assert sent == [{'pause': 'pause_print', 'stop': 'stop_print'}[action]]


def test_opt_in_route_is_superadmin_only_audited_and_idempotent(experimental_app, review_db):
    client, headers, sent = experimental_app
    path = '/printers/9/experimental-actions'
    assert client.put(path, headers=headers, json={'enabled': True}).status_code == 403  # org admin
    make_superadmin(review_db)
    assert client.put(path, headers=headers, json={'enabled': 'yes'}).status_code == 422
    assert client.put('/printers/9999/experimental-actions', headers=headers, json={'enabled': True}).status_code == 404
    review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=10")); review_db.commit()
    assert client.put('/printers/10/experimental-actions', headers=headers, json={'enabled': True}).status_code == 409
    for _ in range(2):
        response = client.put(path, headers=headers, json={'enabled': True})
        assert response.status_code == 200, response.text
        assert response.json() == {'printer_id': 9, 'support_level': 'experimental', 'experimental_actions_enabled': True}
    assert client.put(path, headers=headers, json={'enabled': False}).json()['experimental_actions_enabled'] is False
    assert client.put(path, headers=headers, json={'enabled': False}).status_code == 200
    actions = review_db.execute(text("SELECT action FROM audit_logs WHERE action LIKE 'printer.experimental_actions_%' ORDER BY id")).scalars().all()
    assert actions == ['printer.experimental_actions_enabled', 'printer.experimental_actions_disabled']


def test_controls_work_after_opt_in_and_monitoring_never_needed_it(experimental_app, review_db):
    client, headers, sent = experimental_app
    make_superadmin(review_db)
    detail = client.get('/printers/9', headers=headers).json()  # monitoring read path, before any opt-in
    assert (detail['support_level'], detail['experimental_actions_enabled']) == ('experimental', False)
    assert client.post('/printers/9/plate-cleared', headers=headers).status_code == 200  # queue bookkeeping, exempt
    assert client.put('/printers/9/experimental-actions', headers=headers, json={'enabled': True}).status_code == 200
    assert client.get('/printers/9', headers=headers).json()['experimental_actions_enabled'] is True
    for action in ('pause', 'stop', 'speed'):
        assert client.post(f'/printers/9/{action}', headers=headers, json=CONTROL_BODIES.get(action)).status_code == 200, action
    assert sent == ['pause_print', 'stop_print', 'set_print_speed']
    client.put('/printers/9/experimental-actions', headers=headers, json={'enabled': False})
    assert client.post('/printers/9/resume', headers=headers).status_code == 409
    refused = review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='printer.experimental_control_refused'")).scalar()
    assert refused >= 1


def test_production_router_guards_every_control_route():
    from fastapi.routing import APIRoute
    from modules.printers import routes as aggregate, routes_controls
    from modules.printers.routes_experimental import experimental_controls_guard
    keys = {(r.path, tuple(sorted(r.methods))) for r in routes_controls.router.routes if isinstance(r, APIRoute)}
    assert {path.rsplit('/', 1)[-1] for path, _ in keys} >= set(CONTROL_ACTIONS) | set(SAFETY_ACTIONS)
    seen = set()
    for route in aggregate.router.routes:
        key = (getattr(route, 'path', None), tuple(sorted(getattr(route, 'methods', ()))))
        if isinstance(route, APIRoute) and key in keys:
            seen.add(key)
            assert any(dep.call is experimental_controls_guard for dep in route.dependant.dependencies), route.path
    assert seen == keys


# ---- RBAC registration ----

def test_opt_in_route_is_registered_as_superadmin_only():
    from core.rbac_routes import write_exemption, _SUPERADMIN
    assert write_exemption('PUT', '/api/printers/{printer_id}/experimental-actions') == _SUPERADMIN


# ---- review round 1: fail closed on unknown types; opt-in bound to the printer; every control route keyed by printer ----

def test_a_type_missing_from_the_table_is_refused_but_pause_and_stop_work(graph, monkeypatch):
    from modules.printers import route_utils, support_levels
    monkeypatch.setattr(route_utils, '_bambu_command', lambda printer, action: True)
    printer = graph.get(Printer, 9)
    monkeypatch.setattr(printer, 'api_type', 'mystery_lan', raising=False)
    assert support_levels.support_level('mystery_lan') == 'unsupported'
    assert support_levels.command_refusal(printer).startswith('printer_type_unsupported')
    assert route_utils._send_printer_command(printer, 'resume_print') is False
    assert support_levels.experimental_refusal(graph, 9, None) is None  # a manual printer is not a device


def test_an_opt_in_does_not_follow_a_recycled_id_to_another_printer(graph, monkeypatch):
    from modules.printers import support_levels
    experimental(monkeypatch, 'bambu')
    opt_in(graph)
    assert support_levels.experimental_refusal(graph, 9, 'bambu') is None
    graph.execute(text("UPDATE printers SET name='A different printer' WHERE id=9")); graph.commit()
    assert support_levels.experimental_refusal(graph, 9, 'bambu') is not None


def test_every_controls_route_is_keyed_by_printer_id():
    """The guard finds the printer from {printer_id}; a control route without it would skip the guard."""
    from fastapi.routing import APIRoute
    from modules.printers import routes_controls
    for route in routes_controls.router.routes:
        if isinstance(route, APIRoute) and route.methods - {'GET', 'HEAD', 'OPTIONS'}:
            assert '{printer_id}' in route.path, route.path
