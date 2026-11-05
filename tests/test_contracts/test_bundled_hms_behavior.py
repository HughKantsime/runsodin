"""Real persistence boundaries for bundled HMS severity and repeat suppression."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_hms_observation_boundaries import hms_boundary, monitor_db


@pytest.mark.parametrize('level,alerts', [(1,1),(2,1),(3,0),(4,0)])
def test_each_severity_alerts_without_failing_job(hms_boundary, level, alerts):
    db, handler, _, outgoing = hms_boundary
    handler.process_hms_errors(9,[{'attr':0x05010400,'code':(level<<16)|2}],observed_filename='current.3mf')
    assert len(outgoing) == alerts
    assert all(a['alert_type'] == 'printer_error' for a in outgoing)
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one() == 'running'
    active=json.loads(db.execute(text('SELECT hms_errors FROM printers WHERE id=9')).scalar_one())
    assert len(active)==1


def test_reconnect_and_restart_repeat_persists_until_explicit_clear(hms_boundary):
    db, handler, _, outgoing = hms_boundary
    event=[{'attr':0x03003100,'code':0x00010001}]
    handler.process_hms_errors(9,event)
    assert len(outgoing)==1
    # No monitor object or timestamp is involved. Separate DB connections and a
    # changed filename cannot reset identity; this outlives the 6-hour policy.
    db.execute(text("UPDATE printers SET last_error_at='2000-01-01' WHERE id=9")); db.commit()
    handler.process_hms_errors(9,event,observed_filename='other.3mf')
    handler.process_hms_errors(9,None)
    assert len(outgoing)==1
    handler.process_hms_errors(9,[])
    db.expire_all()
    assert json.loads(db.execute(text('SELECT hms_errors FROM printers WHERE id=9')).scalar_one()) == []
    assert db.execute(text('SELECT last_error_code FROM printers WHERE id=9')).scalar_one() == '03003100_00010001'
    handler.process_hms_errors(9,event)
    assert len(outgoing)==2


def test_common_spaghetti_has_no_override_and_no_repeat(hms_boundary):
    db, handler, _, outgoing = hms_boundary
    for _ in range(3):
        handler.process_hms_errors(9,[{'attr':0x0C000300,'code':0x00030006}])
    assert outgoing == []
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one() == 'running'
    assert len(json.loads(db.execute(text('SELECT hms_errors FROM printers WHERE id=9')).scalar_one())) == 1


from tests.test_telemetry.test_bundled_hms import lists


def test_alert_copy_uses_same_model_list_for_hms_and_print_errors(hms_boundary,lists,monkeypatch):
    from core import crypto
    from modules.notifications.alert_copy import humanize
    db, _, _, _ = hms_boundary
    monkeypatch.setattr(crypto,'decrypt',lambda _: '094SYNTHETIC|synthetic')
    db.execute(text("UPDATE printers SET api_type='bambu',api_key='encrypted' WHERE id=9"));db.commit()
    for code,expected in [('05010400_00030002','Lubricate threaded rods'),(0x03004002,'Synthetic print action')]:
        _,message,_=humanize(db,'print_paused','Paused','',9,None,{'code':code})
        assert expected in message
