"""Owned broker and controlled callbacks; never a physical plug."""
import time
import threading
from types import SimpleNamespace
import pytest
from core.mqtt_connection_snapshot import MqttConnectionSnapshot
from core.mqtt_power_protocol import send_owned_mqtt_on


@pytest.mark.parametrize('mode',['ack','no_ack','bad_connack','bad_publish','cleanup_error','disconnect_before_publish','disconnect_during_ack'])
def test_owned_command_receipt_requires_connack_and_puback(monkeypatch,mode):
    calls=[]
    class Client:
        def __init__(self,*args,**kwargs):assert kwargs['reconnect_on_failure'] is False
        def username_pw_set(self,*args):calls.append('auth')
        def tls_set(self):calls.append('tls')
        def connect(self,*args,**kwargs):calls.append('connect');return 0
        def loop_start(self):
            self.on_connect(self,None,None,5 if mode=='bad_connack' else 0)
            if mode=='disconnect_before_publish':self.on_disconnect(self,None,None,128)
            return 0
        def publish(self,*args,**kwargs):
            calls.append((args,kwargs))
            def wait(**kwargs):
                if mode=='disconnect_during_ack':self.on_disconnect(self,None,None,128)
            return SimpleNamespace(rc=4 if mode=='bad_publish' else 0,wait_for_publish=wait,is_published=lambda:mode in ('ack','cleanup_error'))
        def disconnect(self):
            calls.append('disconnect')
            if mode=='cleanup_error':raise RuntimeError('synthetic private marker')
        def loop_stop(self):calls.append('stop')
    monkeypatch.setattr('paho.mqtt.client.Client',Client)
    monkeypatch.setattr('core.itar.enforce_host_destination',lambda *args,**kwargs:None)
    result=send_owned_mqtt_on(MqttConnectionSnapshot('localhost',1883,'synthetic','synthetic',True),'synthetic/command',time.monotonic()+1)
    assert result==('accepted' if mode=='ack' else 'refused' if mode in ('bad_connack','disconnect_before_publish') else 'uncertain')
    assert calls[-2:]==['disconnect','stop']
    assert sum(isinstance(call,tuple) for call in calls)==(0 if mode in ('bad_connack','disconnect_before_publish') else 1)
    if mode not in ('bad_connack','disconnect_before_publish'):assert (('synthetic/command','ON'),{'qos':1,'retain':False}) in calls


@pytest.mark.parametrize('topic',['','bad/+','bad/#','bad\ncommand',None])
def test_invalid_topic_never_acquires_connection(monkeypatch,topic):
    monkeypatch.setattr('paho.mqtt.client.Client',lambda *args,**kwargs:pytest.fail('must not connect'))
    assert send_owned_mqtt_on(MqttConnectionSnapshot('localhost',1883,'','',False),topic,time.monotonic()+1)=='refused'


def test_actual_owned_broker_receipt_keeps_physical_state_unknown(monkeypatch):
    import multiprocessing
    import paho.mqtt.client as mqtt
    from modules.printers.telemetry.live_replay import LocalBroker
    from modules.printers.plug_power_acquisition import bounded_power_on,PowerAttempt
    monkeypatch.setenv('ODIN_ITAR_MODE','0')
    broker=LocalBroker();broker.start();subscriber=None;ready=threading.Event();received=threading.Event();messages=[]
    before={process.pid for process in multiprocessing.active_children()}
    try:
        subscriber=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,protocol=mqtt.MQTTv311)
        subscriber.on_subscribe=lambda *args:ready.set()
        def message(client,userdata,msg):
            messages.append((msg.topic,msg.payload,msg.qos,msg.retain));received.set()
        subscriber.on_message=message
        subscriber.connect(broker.host,broker.port);subscriber.subscribe('synthetic/command',qos=1);subscriber.loop_start()
        assert ready.wait(3)
        snapshot=MqttConnectionSnapshot(broker.host,broker.port,'','',False)
        assert bounded_power_on({'type':'mqtt','entity_id':'synthetic/command'},time.monotonic()+5,snapshot)==PowerAttempt('accepted',None)
        assert received.wait(3)
        assert messages==[('synthetic/command',b'ON',1,False)]
        assert {process.pid for process in multiprocessing.active_children()}==before
    finally:
        if subscriber is not None:subscriber.disconnect();subscriber.loop_stop()
        broker.stop()


def test_owned_tcp_peer_without_connack_is_bounded_and_reaped(monkeypatch):
    import multiprocessing
    import socketserver
    from modules.printers.plug_power_acquisition import bounded_power_on, PowerAttempt
    monkeypatch.setenv('ODIN_ITAR_MODE','0')
    connected=threading.Event();release=threading.Event()
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if self.request.recv(4096):connected.set()
            release.wait(timeout=10)
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    before={process.pid for process in multiprocessing.active_children()}
    started=time.monotonic()
    try:
        snapshot=MqttConnectionSnapshot('127.0.0.1',server.server_address[1],'','',False)
        assert bounded_power_on({'type':'mqtt','entity_id':'synthetic/command'},started+1.5,snapshot)==PowerAttempt('uncertain')
        assert connected.is_set()
        assert time.monotonic()-started<4.5
        assert {process.pid for process in multiprocessing.active_children()}==before
    finally:
        release.set();server.shutdown();server.server_close();thread.join(timeout=3)
        assert not thread.is_alive()
