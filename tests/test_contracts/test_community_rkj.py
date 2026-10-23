"""Offline regressions for the scoped community reports, on the bootstrap schema."""
from urllib.parse import unquote
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

USER = {'id': 3, 'role': 'admin', 'group_id': 1, 'is_superadmin': True}


@pytest.mark.parametrize('material,expected', [('PLA+', 'PLA'), ('custom blend', 'OTHER')])
@pytest.mark.parametrize('existing', [False, True])
def test_scan_material_fresh_printer_serialization(review_db, material, expected, existing):
    from modules.inventory.routes.spool_ops import scan_assign_spool
    from modules.inventory.routes._helpers import ScanAssignRequest
    from modules.printers.models import Printer
    from modules.printers.schemas import PrinterResponse
    db = review_db
    db.execute(text("UPDATE printers SET slot_count=4,tags='[]',timelapse_enabled=0,shared=0"))
    db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(199,'Test','Test',:material)"), {'material': material})
    db.execute(text("INSERT INTO spools(id,filament_id,qr_code,org_id) VALUES(199,199,'rkj',1)"))
    if not existing:
        db.execute(text('DELETE FROM filament_slots WHERE printer_id=9'))
    db.commit()
    assert scan_assign_spool(ScanAssignRequest(qr_code='rkj', printer_id=9, slot=1), USER, db).success
    with Session(db.get_bind()) as fresh:
        printers = fresh.query(Printer).all()
        serialized = [PrinterResponse.model_validate(p).model_dump(mode='json') for p in printers]
        assert next(p for p in serialized if p['id'] == 9)['filament_slots'][0]['filament_type'] == expected
        assert fresh.execute(text('SELECT material FROM filament_library WHERE id=199')).scalar_one() == material


@pytest.mark.parametrize('material,expected', [('PLA+', 'PLA'), ('custom blend', 'OTHER')])
def test_startup_repairs_invalid_material_once(review_db, material, expected):
    from core.schema import bootstrap_database
    from modules.printers.models import Printer
    from modules.printers.schemas import PrinterResponse
    db = review_db
    db.execute(text("UPDATE printers SET slot_count=4,tags='[]',timelapse_enabled=0,shared=0"))
    db.execute(text('UPDATE filament_slots SET filament_type=:material WHERE printer_id=9'), {'material': material})
    db.commit()
    bootstrap_database(db.get_bind())
    bootstrap_database(db.get_bind())
    with Session(db.get_bind()) as fresh:
        assert PrinterResponse.model_validate(fresh.get(Printer, 9)).filament_slots[0].filament_type.value == expected
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='slot-material.normalize'")).scalar_one() == 1


@pytest.mark.parametrize('route', ['normal', 'setup'])
@pytest.mark.parametrize('credential,authorized', [('synthetic-key', True), ('maker|synthetic-pass', True), ('wrong', False), ('maker|wrong', False)])
def test_prusa_routes_authenticated_pinned_transport(monkeypatch, route, credential, authorized):
    from modules.printers import route_utils, routes_crud
    from modules.system import routes_setup
    from starlette.requests import Request
    from contextlib import contextmanager
    import core.webhook_utils as destinations
    pinned = []
    @contextmanager
    def pin(url):
        pinned.append(url)
        yield
    monkeypatch.setattr(destinations, 'pinned_device_destination', pin)
    monkeypatch.setattr(route_utils, '_check_ssrf_blocklist', lambda host: None)
    monkeypatch.setattr(routes_crud, '_check_ssrf_blocklist', lambda host: None)
    monkeypatch.setattr(routes_setup, '_validate_setup_phase', lambda db: None)
    requests = []
    def transport(request):
        requests.append(request)
        if '|' in credential:
            if not request.headers.get('Authorization'):
                return httpx.Response(401, headers={'WWW-Authenticate': 'Digest realm="Prusa", nonce="test-nonce", qop="auth"'})
            assert request.headers['Authorization'].startswith('Digest ')
            assert 'username="maker"' in request.headers['Authorization']
        else:
            assert request.headers['X-Api-Key'] == credential
        return httpx.Response(200 if authorized else 401)
    def get(url, **kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
            kwargs.pop('trust_env')
            return client.get(url, **kwargs)
    monkeypatch.setattr(httpx, 'get', get)
    args = dict(api_type='prusalink', api_host='192.168.1.44', api_key=credential)
    if route == 'normal':
        result = routes_crud.test_printer_connection(route_utils.TestConnectionRequest(**args), USER)
    else:
        result = routes_setup.setup_test_printer(routes_setup.SetupTestPrinterRequest(**args), Request({'type': 'http'}), None)
    assert result['success'] is authorized
    assert pinned == ['http://192.168.1.44/api/version']
    assert credential not in str(result)


def test_moonraker_sensor_objects_are_encoded(monkeypatch):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    printer = MoonrakerPrinter('192.168.1.44')
    names = ['temperature_sensor chamber', 'temperature_sensor a&b', 'temperature_sensor a#b']
    printer._temperature_sensors = names
    paths = []
    monkeypatch.setattr(printer, '_get', lambda path: paths.append(path) or None)
    printer.get_status()
    query = paths[0].split('?', 1)[1]
    assert ' ' not in query and '#' not in query
    assert [unquote(part) for part in query.split('&')][-3:] == names


def test_moonraker_completion_dispatch_uses_real_module(monkeypatch):
    from modules.printers.monitors import moonraker_monitor
    from modules.printers import dispatch
    attempt = Mock()
    monkeypatch.setattr(dispatch, 'attempt_dispatch', attempt)
    monkeypatch.setattr(moonraker_monitor.time, 'sleep', lambda _: None)
    monitor = moonraker_monitor.MoonrakerMonitor(9, 'test', '192.168.1.44')
    monitor._try_dispatch()
    attempt.assert_called_once_with(9)


@pytest.fixture
def legacy_monitor(review_db, monkeypatch):
    from core import db as core_db
    from core.registry import registry
    from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor
    from modules.organizations import register_providers
    register_providers(registry)
    db = review_db
    # A separate generic graph; the fixture's Education graph stays protected.
    db.execute(text("INSERT INTO models(id,name,org_id) VALUES(199,'legacy',1)"))
    db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(199,199,'legacy.gcode','legacy.gcode','/test/legacy.gcode',1)"))
    db.execute(text("INSERT INTO jobs(id,model_id,item_name,status,printer_id,charged_to_org_id) VALUES(199,199,'legacy','printing',9,1)"))
    db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,status,started_at) VALUES(199,9,'legacy.gcode','running',CURRENT_TIMESTAMP)"))
    db.commit()
    monkeypatch.setattr(core_db, 'engine', db.get_bind())
    monitor = MoonrakerMonitor(9, 'test', '192.168.1.44')
    monitor._current_job_db_id = 199
    return db, monitor


@pytest.mark.parametrize('by_layers', [False, True])
@pytest.mark.parametrize('status', ['printing', 'scheduled'])
def test_moonraker_legacy_null_pointer_link(legacy_monitor, status, by_layers):
    db, monitor = legacy_monitor
    db.execute(text('UPDATE jobs SET status=:status WHERE id=199'), {'status': status})
    # A queued copy must not win over the already-printing legacy job.
    if status == 'printing':
        db.execute(text("INSERT INTO jobs(id,model_id,item_name,status,printer_id,charged_to_org_id) VALUES(198,199,'legacy','scheduled',9,1)"))
    db.commit()
    if by_layers:
        db.execute(text('UPDATE print_files SET layer_count=100 WHERE id=199'))
        db.commit()
    monitor._try_auto_link('unmatched.gcode' if by_layers else 'legacy.gcode', 100 if by_layers else 0)
    db.expire_all()
    assert db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=199')).scalar_one() == 199
    assert db.execute(text('SELECT status FROM jobs WHERE id=199')).scalar_one() == 'printing'


@pytest.mark.parametrize('blocked', ['hold', 'tenant', 'inactive', 'education', 'dispatch-name', 'education-name', 'already-linked', 'wrong-explicit-file', 'claim'])
def test_moonraker_legacy_link_exclusions(legacy_monitor, blocked):
    db, monitor = legacy_monitor
    filename = 'legacy.gcode'
    if blocked == 'hold':
        db.execute(text('UPDATE jobs SET hold=1 WHERE id=199'))
    elif blocked == 'tenant':
        db.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=199'))
    elif blocked == 'inactive':
        db.execute(text('UPDATE printers SET is_active=0 WHERE id=9'))
    elif blocked == 'education':
        db.execute(text('UPDATE jobs SET model_id=12 WHERE id=199'))
    elif blocked == 'wrong-explicit-file':
        db.execute(text('UPDATE jobs SET print_file_id=11 WHERE id=199'))
    elif blocked in ('dispatch-name', 'education-name'):
        filename = ('odin-dispatch-' if blocked == 'dispatch-name' else 'odin-') + 'a'*32 + '.gcode'
        db.execute(text('UPDATE print_jobs SET filename=:name WHERE id=199'), {'name': filename})
    elif blocked == 'already-linked':
        db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,status,started_at,scheduled_job_id) VALUES(198,9,'legacy.gcode','running',CURRENT_TIMESTAMP,199)"))
    elif blocked == 'claim':
        db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,state) VALUES(9,199,:key,:key,:key,:token,'preparing')"), {'key': 'a'*64, 'token': 'b'*32})
    db.commit()
    with db.get_bind().begin() as connection:
        assert monitor._link_job(connection, 199) is False
    monitor._try_auto_link(filename, 0)
    assert db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=199')).scalar_one() is None
