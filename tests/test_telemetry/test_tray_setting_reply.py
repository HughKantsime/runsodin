import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from modules.printers.telemetry.bambu.tray_setting import TraySettingAdapter, set_tray
from tests.test_telemetry.test_telemetry_command_adapter import FakeMqttClient, config


@pytest.fixture
def client(config, monkeypatch):
    fake = FakeMqttClient()
    fake.subscribe = MagicMock(side_effect=lambda *args: fake.on_subscribe(fake, None, 1, [0]))
    fake.loop_start.side_effect = lambda: fake.on_connect(fake, None, {}, 0)
    monkeypatch.setattr(TraySettingAdapter, '_client_factory', staticmethod(lambda: fake))
    monkeypatch.setattr('core.webhook_utils.device_connect_ip', lambda host, port: host)
    return fake


def reply(fake, **fields):
    fake.on_message(fake, None, SimpleNamespace(retain=False, payload=json.dumps({'print': {
        'command':'ams_filament_setting', 'ams_id':2, 'tray_id':1, 'result':'success', 'reason':None, **fields}})))


@pytest.mark.parametrize('result,reason', [('success', None), ('fail','busy')])
def test_waits_for_matching_printer_reply_and_closes(config, client, result, reason):
    original = client.publish.side_effect
    def publish(*args):
        sent = original(*args)
        reply(client, ams_id=0, result='wrong')
        reply(client, result=result, reason=reason)
        return sent
    client.publish.side_effect = publish
    assert set_tray(config, 2, 1, 'GFG02','PETG','FFFFFF',230,260,timeout=.1) == {'result':result,'reason':reason}
    client.disconnect.assert_called_once()
    client.loop_stop.assert_called_once()


def test_broker_ack_without_printer_reply_times_out(config, client):
    assert set_tray(config, 2, 1, 'GFG02','PETG','FFFFFF',230,260,timeout=.001) == {'result':None,'reason':'reply_timeout'}
    client.disconnect.assert_called_once()


def test_no_subscription_ack_never_publishes(config, client):
    client.subscribe.side_effect = None
    assert set_tray(config, 2, 1, 'GFG02','PETG','FFFFFF',230,260,timeout=.001)['reason'] == 'subscription_timeout'
    client.publish.assert_not_called()


def test_retained_and_malformed_reply_are_ignored(config):
    adapter = TraySettingAdapter(config)
    adapter.target = (2, 1)
    for payload in ('bad', '[]', '{"print":null}', json.dumps({'print': {'command':'ams_filament_setting','ams_id':2,'tray_id':1,'result':'success'}})):
        adapter._on_message(None,None,SimpleNamespace(payload=payload,retain=True))
    assert not adapter.replied.is_set()


def test_rejected_subscription_never_publishes(config, client):
    client.subscribe.side_effect = lambda *args: client.on_subscribe(client,None,1,[128])
    assert set_tray(config,2,1,'GFG02','PETG','FFFFFF',230,260,timeout=.001)['reason'] == 'subscription_timeout'
    client.publish.assert_not_called()
