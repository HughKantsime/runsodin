"""Actual daemon process must have Education lifecycle authority before packets."""
import os
import subprocess
import sys
import pytest
from pathlib import Path

BACKEND=Path(__file__).resolve().parents[2]/'backend'


def test_fresh_mqtt_daemon_has_education_policy_before_connections(tmp_path):
    env=dict(os.environ)
    env['PYTHONPATH']=str(BACKEND)
    env['BACKEND_PATH']=str(BACKEND)
    env['DATABASE_URL']='sqlite:///'+str(tmp_path/'owned-daemon.db')
    code='''from modules.printers.monitors.mqtt_monitor import MQTTMonitorDaemon
from core.registry import registry
daemon=MQTTMonitorDaemon()
assert registry.get_provider("EducationPolicyProvider") is not None, "Fresh monitor has no Education authority"
from modules.orders.production import OrderFulfillmentService
assert isinstance(registry.get_provider("OrderFulfillmentProvider"), OrderFulfillmentService)
assert not daemon.monitors, "Bootstrap connected real printers"
'''
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr[-1600:]


def test_bootstrap_reuses_authority_without_api_or_database_startup(tmp_path):
    env=dict(os.environ, PYTHONPATH=str(BACKEND), BACKEND_PATH=str(BACKEND),
             DATABASE_URL='sqlite:///'+str(tmp_path/'no-write.db'))
    code='''import sys
from unittest.mock import patch
import core.db
from core.monitor_bootstrap import bootstrap_monitor_authority
from core.registry import registry
with patch.object(core.db.engine, "connect", side_effect=AssertionError("Bootstrap opened database")):
    bootstrap_monitor_authority()
    policy=registry.get_provider("EducationPolicyProvider")
    production=registry.get_provider("OrderFulfillmentProvider")
    bootstrap_monitor_authority()
assert registry.get_provider("EducationPolicyProvider") is policy
assert registry.get_provider("OrderFulfillmentProvider") is production
assert "modules.orders.routes" not in sys.modules
assert "core.app" not in sys.modules
assert "main" not in sys.modules
'''
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr[-1600:]


def test_bootstrap_failure_stops_daemon_before_loading_printers(tmp_path):
    env=dict(os.environ, PYTHONPATH=str(BACKEND), BACKEND_PATH=str(BACKEND),
             DATABASE_URL='sqlite:///'+str(tmp_path/'no-write.db'))
    code='''from unittest.mock import patch
from modules.printers.monitors.mqtt_monitor import MQTTMonitorDaemon
import modules.organizations as organizations
with patch.object(organizations, "register_providers", side_effect=ValueError("private diagnostic")):
    try:
        MQTTMonitorDaemon()
    except RuntimeError as exc:
        assert str(exc)=="Monitor Education authority initialization failed"
        assert exc.__suppress_context__
    else:
        raise AssertionError("Daemon accepted missing authority")
'''
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr[-1600:]


def _run_owned_process(tmp_path, code):
    env=dict(os.environ, PYTHONPATH=str(BACKEND), BACKEND_PATH=str(BACKEND),
             DATABASE_URL='sqlite:///'+str(tmp_path/'owned.db'))
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr[-1600:]


def test_explicit_valid_provider_is_preserved(tmp_path):
    _run_owned_process(tmp_path, '''from core.registry import registry
from modules.organizations.services import EducationPolicyService
from core.monitor_bootstrap import bootstrap_monitor_authority
policy=EducationPolicyService()
registry.register_provider("EducationPolicyProvider", policy)
bootstrap_monitor_authority()
assert registry.get_provider("EducationPolicyProvider") is policy
''')


def test_invalid_existing_provider_is_refused(tmp_path):
    _run_owned_process(tmp_path, '''from core.registry import registry
from core.monitor_bootstrap import bootstrap_monitor_authority
policy=object()
registry.register_provider("EducationPolicyProvider", policy)
try:
    bootstrap_monitor_authority()
except RuntimeError as exc:
    assert str(exc)=="Monitor Education authority initialization failed"
else:
    raise AssertionError("Invalid authority accepted")
assert registry.get_provider("EducationPolicyProvider") is policy
''')


def test_moonraker_failure_precedes_database_enumeration_and_transport(tmp_path):
    _run_owned_process(tmp_path, '''from unittest.mock import patch
from modules.printers.monitors import moonraker_monitor as monitor
import modules.organizations as organizations
with patch.object(organizations, "register_providers", side_effect=ValueError("private diagnostic")), patch.object(monitor.engine, "connect") as connect, patch.object(monitor, "MoonrakerMonitor") as transport:
    try:
        monitor.start_moonraker_monitors()
    except RuntimeError as exc:
        assert str(exc)=="Monitor Education authority initialization failed"
    else:
        raise AssertionError("Missing authority accepted")
    connect.assert_not_called()
    transport.assert_not_called()
''')


@pytest.mark.parametrize("kind,class_name", [("prusalink","PrusaLinkMonitorThread"),("elegoo","ElegooMonitorThread")])
def test_shared_job_monitor_failure_precedes_enumeration(tmp_path, kind, class_name):
    _run_owned_process(tmp_path, f'''from unittest.mock import patch
from modules.printers.monitors import {kind}_monitor as monitor
import modules.organizations as organizations
with patch.object(organizations, "register_providers", side_effect=ValueError("private diagnostic")), patch.object(monitor.engine, "connect") as connect, patch.object(monitor, "{class_name}") as transport:
    try:
        monitor.start_{kind}_monitors()
    except RuntimeError as exc:
        assert str(exc)=="Monitor Education authority initialization failed"
    else:
        raise AssertionError("Missing authority accepted")
    connect.assert_not_called()
    transport.assert_not_called()
''')


def test_valid_production_subclass_and_subscribers_preserved(tmp_path):
    _run_owned_process(tmp_path, '''from core.registry import registry
from modules.orders.production import OrderFulfillmentService
from core.monitor_bootstrap import bootstrap_monitor_authority
from core import event_bus, events
from core.event_bus import InMemoryEventBus
class ValidProduction(OrderFulfillmentService):
    pass
provider=ValidProduction()
registry.register_provider("OrderFulfillmentProvider",provider)
event_bus._bus=InMemoryEventBus()
bootstrap_monitor_authority()
bootstrap_monitor_authority()
assert registry.get_provider("OrderFulfillmentProvider") is provider
assert len(event_bus._bus._handlers[events.JOB_STARTED])==1
assert len(event_bus._bus._handlers[events.JOB_COMPLETED])==1
''')


@pytest.mark.parametrize('invalid', ['object', 'missing_method'])
def test_invalid_production_is_refused_without_overwrite(tmp_path,invalid):
    _run_owned_process(tmp_path, f'''from core.registry import registry
from modules.orders.production import OrderFulfillmentService
from core.monitor_bootstrap import bootstrap_monitor_authority
from core import event_bus
from core.event_bus import InMemoryEventBus
provider=object() if {invalid!r}=='object' else OrderFulfillmentService()
if {invalid!r}=='missing_method':provider.recheck_parent_graph=None
registry.register_provider("OrderFulfillmentProvider",provider)
event_bus._bus=InMemoryEventBus()
try:
    bootstrap_monitor_authority()
except RuntimeError as exc:
    assert str(exc)=="Monitor production authority initialization failed"
    assert exc.__suppress_context__
else:
    raise AssertionError("Invalid production authority accepted")
assert registry.get_provider("OrderFulfillmentProvider") is provider
assert not registry.has_provider("EducationPolicyProvider")
assert not any(event_bus._bus._handlers.values())
''')


@pytest.mark.parametrize('kind,class_name', [('mqtt','PrinterMonitor'),('moonraker','MoonrakerMonitor'),('prusalink','PrusaLinkMonitorThread'),('elegoo','ElegooMonitorThread')])
@pytest.mark.parametrize('failure',['registration','construction'])
def test_production_failure_precedes_all_actual_daemon_connections(tmp_path,kind,class_name,failure):
    entry='monitor.MQTTMonitorDaemon()' if kind=='mqtt' else f'monitor.start_{kind}_monitors()'
    target='orders.register_providers' if failure=='registration' else 'production.OrderFulfillmentService'
    _run_owned_process(tmp_path, f'''from unittest.mock import patch
from modules.printers.monitors import {kind}_monitor as monitor
import modules.orders as orders
import modules.orders.production as production
from core import event_bus
from core.event_bus import InMemoryEventBus
from core.registry import registry
registry._providers={{}}
event_bus._bus=InMemoryEventBus()
with patch({target!r}.replace("orders.","modules.orders.",1) if {failure!r}=='registration' else "modules.orders.production.OrderFulfillmentService",side_effect=ValueError("private diagnostic")), patch.object(monitor.engine,"connect") as connect, patch.object(monitor,"{class_name}") as transport:
    try:
        {entry}
    except RuntimeError as exc:
        assert str(exc)=="Monitor production authority initialization failed"
        assert exc.__suppress_context__
        assert "private diagnostic" not in str(exc)
    else:
        raise AssertionError("Daemon accepted production initialization failure")
    connect.assert_not_called()
    transport.assert_not_called()
assert not registry.has_provider("EducationPolicyProvider")
assert not any(event_bus._bus._handlers.values())
''')


@pytest.mark.parametrize('kind,class_name', [('moonraker','MoonrakerMonitor'),('prusalink','PrusaLinkMonitorThread'),('elegoo','ElegooMonitorThread')])
def test_real_production_provider_precedes_owned_daemon_discovery(tmp_path,kind,class_name):
    _run_owned_process(tmp_path, f'''from unittest.mock import patch
from sqlalchemy import event, text
from modules.printers.monitors import {kind}_monitor as monitor
from modules.orders.production import OrderFulfillmentService
from core.registry import registry
with monitor.engine.begin() as connection:
    connection.execute(text("CREATE TABLE printers(id INTEGER PRIMARY KEY,name TEXT,api_host TEXT,api_key TEXT,api_type TEXT,is_active BOOLEAN)"))
registry._providers={{}}
seen=[]
def before_query(connection,cursor,statement,parameters,context,executemany):
    assert isinstance(registry.get_provider("OrderFulfillmentProvider"),OrderFulfillmentService)
    seen.append(statement)
event.listen(monitor.engine,"before_cursor_execute",before_query)
with patch.object(monitor,"{class_name}") as transport:
    result=monitor.start_{kind}_monitors()
    assert result==[]
    transport.assert_not_called()
assert len(seen)==1 and "FROM printers" in seen[0]
''')


@pytest.mark.parametrize('kind,class_name', [('mqtt','PrinterMonitor'),('moonraker','MoonrakerMonitor'),('prusalink','PrusaLinkMonitorThread'),('elegoo','ElegooMonitorThread')])
def test_production_import_failure_precedes_all_daemon_connections(tmp_path,kind,class_name):
    entry='monitor.MQTTMonitorDaemon()' if kind=='mqtt' else f'monitor.start_{kind}_monitors()'
    _run_owned_process(tmp_path, f'''import builtins
from unittest.mock import patch
from modules.printers.monitors import {kind}_monitor as monitor
from core.registry import registry
registry._providers={{}}
original_import=builtins.__import__
def fail_production_import(name,*args,**kwargs):
    if name=="modules.orders.production":raise ImportError("private import diagnostic")
    return original_import(name,*args,**kwargs)
with patch.object(builtins,"__import__",fail_production_import), patch.object(monitor.engine,"connect") as connect, patch.object(monitor,"{class_name}") as transport:
    try:
        {entry}
    except RuntimeError as exc:
        assert str(exc)=="Monitor production authority initialization failed" and exc.__suppress_context__
    else:
        raise AssertionError("Daemon accepted missing production import")
    connect.assert_not_called()
    transport.assert_not_called()
assert not registry.has_provider("EducationPolicyProvider")
''')
