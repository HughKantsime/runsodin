"""Monitor alert enum wire values and tenant-safe mobile delivery."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.base import AlertType, AlertSeverity
from tests.test_contracts.test_post027_migration_040 import engine


def test_every_monitor_alert_call_uses_enum_members():
    # Frozen bundles and test fixtures are not production monitor callers.
    paths = [p for p in Path('backend').rglob('*.py')
             if 'tests' not in p.parts and not any(part.startswith('preserved') for part in p.parts)]
    checked = set()
    for path in paths:
        tree = ast.parse(path.read_text())
        assignments = {}
        annotations = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments.setdefault(target.id, []).append(node.value)
            if isinstance(node, ast.arg) and isinstance(node.annotation, ast.Name):
                annotations[node.arg] = node.annotation.id

        def is_enum(value, expected, seen=frozenset()):
            if isinstance(value, ast.Attribute):
                return (isinstance(value.value, ast.Name)
                        and value.value.id == expected.__name__
                        and value.attr in expected.__members__)
            if isinstance(value, ast.Call):
                return isinstance(value.func, ast.Name) and value.func.id == expected.__name__
            if isinstance(value, ast.IfExp):
                return is_enum(value.body, expected, seen) and is_enum(value.orelse, expected, seen)
            if isinstance(value, ast.Dict):
                return all(is_enum(v, expected, seen) for v in value.values)
            if isinstance(value, ast.Subscript):
                return is_enum(value.value, expected, seen)
            if isinstance(value, ast.Name) and value.id not in seen:
                if annotations.get(value.id) == expected.__name__:
                    return True  # Typed forwarding wrapper; its callers are checked too.
                values = assignments.get(value.id, [])
                return bool(values) and all(is_enum(v, expected, seen | {value.id}) for v in values)
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'attr', getattr(node.func, 'id', ''))
            if name not in ('dispatch_alert', 'create_alert', '_dispatch_alert',
                            '_dispatch_alert_b', 'dispatch_terminal_alert'):
                continue
            values = {k.arg: k.value for k in node.keywords}
            # Raw dispatch wrappers also accept enum values positionally.
            if name in ('dispatch_alert', 'create_alert', '_dispatch_alert', '_dispatch_alert_b'):
                offset = 1 if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in ('db', 'session') else 0
                for i, key in enumerate(('alert_type', 'severity')):
                    if key not in values and len(node.args) > offset + i:
                        values[key] = node.args[offset + i]
            for key, expected in (('alert_type', AlertType), ('severity', AlertSeverity)):
                assert key in values, (path, node.lineno, 'unvalidated forwarding call')
                assert is_enum(values[key], expected), (path, node.lineno, key)
            checked.add(path)
    assert Path('backend/modules/vision/detection_thread.py') in checked
    assert len(checked) >= 8


def test_mobile_recipient_isolation_preferences_and_allowlist(engine, monkeypatch):
    from modules.push import fanout
    from modules.push.models import PushDevice
    with engine.begin() as c:
        c.execute(text("INSERT INTO groups(id,name) VALUES(1,'one'),(2,'two')"))
        c.execute(text("INSERT INTO users(id,username,password_hash,role,group_id,is_active) VALUES(11,'one','unused','operator',1,TRUE),(22,'two','unused','operator',2,TRUE)"))
        c.execute(text("INSERT INTO printers(id,name,org_id) VALUES(1,'one',1),(2,'two',2)"))
        c.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app,browser_push,email) VALUES(11,'print_complete',TRUE,TRUE,FALSE),(22,'print_complete',TRUE,TRUE,FALSE)"))
    with Session(engine) as db:
        db.add_all([PushDevice(user_id=11,device_id='one',platform='apns',token='test-device-one'),PushDevice(user_id=22,device_id='two',platform='apns',token='test-device-two')]);db.commit()
        provider=Mock();monkeypatch.setattr(fanout,'get_provider',lambda **kw:provider)
        plan=SimpleNamespace(alert_type=AlertType.PRINT_COMPLETE,severity=AlertSeverity.INFO,org_id=1,
            printer_id=1,job_id=None,spool_id=None,in_app_users=(11,22),suppress_external=False)
        fanout.deliver_alert(db,plan)
        assert provider.send_push.call_count==1
        assert provider.send_push.call_args.kwargs['device_token']=='test-device-one'
        assert set(provider.send_push.call_args.kwargs['data'])<= {'odin_event','alert_type','severity','job_id','printer_id','spool_id'}
        db.execute(text('UPDATE alert_preferences SET in_app=FALSE WHERE user_id=11'));db.commit()
        provider.reset_mock();fanout.deliver_alert(db,plan)
        provider.send_push.assert_not_called()


def test_mobile_callback_is_registered_by_the_durable_drain(engine,monkeypatch):
    from modules.notifications import alert_outbox, alert_dispatcher
    from modules.push import fanout
    called=[]
    monkeypatch.setattr(alert_outbox,'_deliveries',[])
    monkeypatch.setattr(fanout,'deliver_alert',lambda db,plan:called.append(plan))
    from modules.notifications import channels
    monkeypatch.setattr(channels, 'send_webhook', lambda *a, **k: None)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'drain','unused','operator',TRUE)"))
        c.execute(text("INSERT INTO jobs(id,item_name,status,submitted_by) VALUES(2,'drain','pending',1)"))
    with Session(engine) as db:
        identifier=alert_outbox.enqueue(db,2,'queue_added',1);db.commit()
    assert alert_outbox.dispatch(engine,identifier)
    assert len(called)==1 and called[0].job_id==2
    assert not alert_outbox.dispatch(engine,identifier)
    assert len(called)==1


def test_plate_ready_detection_finishes_alert_and_websocket(monkeypatch):
    from modules.vision import detection_thread as vision
    from modules.notifications import event_dispatcher
    thread=object.__new__(vision.PrinterVisionThread)
    thread._last_alert={};thread.printer_id=1;thread.printer_name='Printer'
    thread.print_job_id=None;thread.settings={}
    monkeypatch.setattr(vision.frame_storage,'save_detection_frame',lambda *a:'frame')
    monkeypatch.setattr(thread,'_insert_detection',lambda *a:17)
    alerts=[];events=[]
    def dispatch(**payload):
        AlertType(payload['alert_type']);AlertSeverity(payload['severity'])
        alerts.append(payload)
    monkeypatch.setattr(event_dispatcher,'dispatch_alert',dispatch)
    monkeypatch.setattr(vision,'ws_push',lambda *a:events.append(a))
    thread._on_detection('build_plate_empty',{'confidence':.99,'bbox':None},None)
    assert alerts[0]['alert_type']==AlertType.BED_COOLED
    assert alerts[0]['severity']==AlertSeverity.INFO
    assert alerts[0]['title']=='Plate Ready: Printer'
    assert events[0][0]=='vision_detection'


def test_legacy_cancellation_callback_uses_supported_storage_enum(engine,monkeypatch):
    from modules.notifications import alert_dispatch
    monkeypatch.setattr(alert_dispatch,'engine',engine)
    monkeypatch.setattr(alert_dispatch,'get_event_bus',lambda:SimpleNamespace(publish=lambda *a:None))
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(88,'cancel-owner','unused','admin',TRUE)"))
    alert_dispatch.dispatch_alert('print_cancelled',AlertSeverity.WARNING,'Print Cancelled: synthetic')
    with engine.connect() as conn:
        assert conn.execute(text("SELECT alert_type,title FROM alerts WHERE user_id=88")).one()==('print_failed','Print Cancelled: synthetic')
