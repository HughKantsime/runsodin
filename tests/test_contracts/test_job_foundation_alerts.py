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
    paths=list(Path('backend/modules/printers/monitors').glob('*.py'))+[Path('backend/modules/notifications/job_events.py')]
    count=0
    for path in paths:
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call):continue
            name=getattr(node.func,'attr',getattr(node.func,'id',''))
            if not any(k.arg=='alert_type' for k in node.keywords):continue
            for keyword in node.keywords:
                if keyword.arg not in ('alert_type','severity'):continue
                expected=AlertType if keyword.arg=='alert_type' else AlertSeverity
                assert isinstance(keyword.value,ast.Attribute), (path,node.lineno,keyword.arg)
                assert isinstance(keyword.value.value,ast.Name) and keyword.value.value.id==expected.__name__
                assert keyword.value.attr in expected.__members__, (path,node.lineno,keyword.value.attr)
                count+=1
    assert count>10


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
    monkeypatch.setattr(alert_dispatcher,'_deliver_staged_alert',lambda db,plan:None)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'drain','unused','operator',TRUE)"))
        c.execute(text("INSERT INTO jobs(id,item_name,status,submitted_by) VALUES(2,'drain','pending',1)"))
    with Session(engine) as db:
        identifier=alert_outbox.enqueue(db,2,'queue_added',1);db.commit()
    assert alert_outbox.dispatch(engine,identifier)
    assert len(called)==1 and called[0].job_id==2
    assert not alert_outbox.dispatch(engine,identifier)
    assert len(called)==1
