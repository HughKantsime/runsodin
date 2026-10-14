"""Actual dispatch ordering with substituted power and printer transports."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


@pytest.mark.parametrize('power_result', [True, False, None])
def test_configured_automatic_on_precedes_any_upload(monkeypatch, tmp_path, power_result,review_db):
    from modules.printers import dispatch, smart_plug
    from modules.printers import plug_power_acquisition
    engine,payload=configure_dispatch(review_db,tmp_path,monkeypatch)
    calls=[]
    with engine.begin() as conn:conn.execute(text("UPDATE printers SET plug_type='tasmota',plug_host='127.0.0.1',plug_auto_on=TRUE WHERE id=59"))
    monkeypatch.setattr(plug_power_acquisition,'bounded_power_on',lambda *args:calls.append('power_on') or plug_power_acquisition.PowerAttempt('accepted' if power_result is True else 'uncertain',power_result))
    # An adapter rejection avoids any job-status persistence and still proves
    # whether a file-transfer-capable boundary was entered prematurely.
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', lambda *args: calls.append('adapter') or (False, 'Synthetic adapter rejection'))
    success, reason = dispatch.dispatch_job(59,53)
    assert not success
    assert calls and calls[0] == 'power_on'
    if power_result is not True:
        assert 'adapter' not in calls
