"""Configured MQTT publication and actual authenticated route receipts; no devices."""
from enum import IntEnum
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_printer_control_behavior import control_client,token_client,review_db

@pytest.mark.parametrize('outcome',['accepted','timeout','reject','exception'])
def test_public_publisher_distinguishes_broker_ack_from_uncertain_send(monkeypatch,outcome):
    from modules.notifications import mqtt_republish as module
    calls=[]
    class Code(IntEnum):SUCCESS=0
    def wait(timeout):
        assert timeout==5
        if outcome=='exception':raise RuntimeError('Synthetic private marker')
    def publish(topic,payload,**kwargs):
        calls.append((topic,payload,kwargs))
        return SimpleNamespace(rc=Code.SUCCESS if outcome!='reject' else 4,wait_for_publish=wait,is_published=lambda:outcome=='accepted')
    client=SimpleNamespace(publish=publish)
    monkeypatch.setattr(module,'_get_client',lambda:client);monkeypatch.setattr(module,'_client',client)
    assert module.publish_power_command('synthetic/command','TOGGLE')=={'accepted':'accepted','timeout':'unconfirmed','reject':'unconfirmed','exception':'unconfirmed'}[outcome]
    assert calls==[('synthetic/command','TOGGLE',{'qos':1,'retain':False})]


@pytest.mark.parametrize('topic',['','synthetic/+','synthetic/#','synthetic\ncommand','x'*513,None])
def test_invalid_topic_refused_before_connection(monkeypatch,topic):
    from modules.notifications import mqtt_republish as module
    def forbidden():raise AssertionError('Connection must not be acquired')
    monkeypatch.setattr(module,'_get_client',forbidden)
    assert module.publish_power_command(topic,'ON')=='rejected'


@pytest.fixture
def mqtt_client(control_client,review_db,monkeypatch):
    from modules.printers import routes_smart_plug,smart_plug
    client,issue,calls=control_client
    client.app.include_router(routes_smart_plug.router)
    review_db.execute(text("UPDATE printers SET plug_type='mqtt',plug_entity_id='synthetic/command',plug_power_state=1 WHERE id=299"));review_db.commit()
    monkeypatch.setattr(smart_plug,'engine',review_db.get_bind())
    return client,issue,calls


def test_retired_client_refused_before_publication(monkeypatch):
    from modules.notifications import mqtt_republish as module
    def forbidden(*args, **kwargs):
        raise AssertionError('Retired client must not publish')
    selected = SimpleNamespace(publish=forbidden)
    replacement = SimpleNamespace(publish=forbidden)
    monkeypatch.setattr(module, '_client', replacement)
    monkeypatch.setattr(module, '_get_client', lambda: selected)
    assert module.publish_power_command('synthetic/command', 'ON') == 'rejected'


def test_retirement_waits_for_inflight_confirmation(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from modules.notifications import mqtt_republish as module
    waiting, release, retiring, stopped = Event(), Event(), Event(), Event()
    def wait(timeout):
        assert timeout == 5
        waiting.set()
        assert release.wait(3)
        assert not stopped.is_set()
    receipt = SimpleNamespace(rc=0, wait_for_publish=wait, is_published=lambda: True)
    client = SimpleNamespace(publish=lambda *args, **kwargs: receipt)
    monkeypatch.setattr(module, '_client', client)
    monkeypatch.setattr(module, '_get_client', lambda: client)
    monkeypatch.setattr(module, '_stop_client', lambda previous: stopped.set())
    def retire():
        retiring.set()
        module.disconnect()
    with ThreadPoolExecutor(max_workers=2) as pool:
        command = pool.submit(module.publish_power_command, 'synthetic/command', 'ON')
        try:
            assert waiting.wait(3)
            retirement = pool.submit(retire)
            assert retiring.wait(3)
            assert not stopped.wait(0.05)
        finally:
            release.set()
        assert command.result(timeout=3) == 'accepted'
        retirement.result(timeout=3)
    assert stopped.is_set()
    assert module._client is None


@pytest.mark.parametrize('action', ['on', '', None, 'RESTART'])
def test_invalid_action_refused_before_connection(monkeypatch, action):
    from modules.notifications import mqtt_republish as module
    def forbidden():
        raise AssertionError('Connection must not be acquired')
    monkeypatch.setattr(module, '_get_client', forbidden)
    assert module.publish_power_command('synthetic/command', action) == 'rejected'


@pytest.mark.parametrize('action',['on','off','toggle'])
@pytest.mark.parametrize('outcome,status', [('accepted',200),('unconfirmed',409),('rejected',400)])
def test_mqtt_manual_route_preserves_publication_receipt_with_nonce(mqtt_client,review_db,monkeypatch,action,outcome,status):
    from modules.notifications import mqtt_republish
    client,issue,calls=mqtt_client;published=[]
    monkeypatch.setattr(mqtt_republish,'publish_power_command',lambda topic,payload:(published.append((topic,payload)) or outcome))
    response=client.post(f'/printers/299/plug/{action}',headers={**issue(['write:printers']),'X-Power-Request-Nonce':'a'*32})
    assert response.status_code==status
    if outcome=='accepted':
        assert response.json()=={'accepted': True, 'power_state': None}
    elif outcome=='unconfirmed':
        assert response.json()['detail']=='Power command may have been sent; confirmation unavailable. Check before retrying.'
    else:
        assert response.json()['detail']=='Power command was not accepted'
    assert published==[('synthetic/command', {'on':'ON','off':'OFF','toggle':'TOGGLE'}[action])]
    assert review_db.execute(text('SELECT plug_power_state FROM printers WHERE id=299')).scalar_one()==1

    assert review_db.execute(text('SELECT count(*) FROM power_target_operations')).scalar_one()==0
