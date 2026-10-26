"""Fresh Bambu acquisition with controlled callbacks and owned broker."""
import json
import time
import threading
from types import SimpleNamespace
import pytest


@pytest.fixture(autouse=True)
def _loopback_device_peers(monkeypatch):
    # Brokers/peers in this file run on loopback, which the production device
    # destination policy blocks; exempt exactly 127.0.0.1 and ::1 here (also in
    # spawned workers). test_cert_A_destination_policy covers the policy.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
from modules.printers import dispatch_bambu_readiness as module


@pytest.mark.parametrize('mode', ['idle','finished','partial_then_idle','retained','other_topic','busy','malformed','oversize','bad_connect','bad_subscribe','bad_publish','disconnect_after_idle','cleanup_disconnect_error','cleanup_busy'])
def test_session_requires_ack_topic_fresh_report_and_normal_cleanup(monkeypatch,mode):
    calls = []
    class Client:
        def __init__(self,*args,**kwargs):self.delivery=None
        def username_pw_set(self,*args):calls.append('auth')
        def connect(self,*args,**kwargs):calls.append('connect')
        def subscribe(self,topic,qos):calls.append(('subscribe',topic,qos));return (0,7)
        def publish(self,topic,payload,**kwargs):
            calls.append(('publish',topic,json.loads(payload),kwargs))
            return SimpleNamespace(rc=1 if mode=='bad_publish' else 0)
        def loop_start(self):
            self.on_connect(self,None,None,5 if mode=='bad_connect' else 0)
            self.on_subscribe(self,None,7,[128 if mode=='bad_subscribe' else 0])
            payload = {'print':{'command':'push_status','gcode_state':'FINISH' if mode=='finished' else 'IDLE','print_error':0}}
            if mode=='busy':payload['print']['gcode_state']='RUNNING'
            if mode=='malformed':payload['print']['print_error']=False
            raw = b'x'*65537 if mode=='oversize' else json.dumps(payload).encode()
            if mode=='partial_then_idle':
                self.on_message(self,None,SimpleNamespace(topic='device/synthetic/report',retain=False,payload=b'{"print":{"command":"push_status","bed_temper":25}}'))
            def deliver():
                if mode=='partial_then_idle':time.sleep(0.05)
                self.on_message(self,None,SimpleNamespace(topic='device/other/report' if mode=='other_topic' else 'device/synthetic/report',retain=mode=='retained',payload=raw))
            if mode=='partial_then_idle':
                self.delivery=threading.Thread(target=deliver);self.delivery.start()
            else:deliver()
            if mode=='disconnect_after_idle':self.on_disconnect(self,None,None,128)
        def disconnect(self):
            calls.append('disconnect')
            if mode=='cleanup_busy':
                self.on_message(self,None,SimpleNamespace(topic='device/synthetic/report',retain=False,payload=b'{"print":{"command":"push_status","gcode_state":"RUNNING","print_error":0}}'))
            self.on_disconnect(self,None,None,128 if mode=='cleanup_disconnect_error' else 0)
        def loop_stop(self):
            if self.delivery:self.delivery.join()
            calls.append('stop')
    monkeypatch.setattr('paho.mqtt.client.Client',Client)
    monkeypatch.setattr('core.itar.enforce_host_destination',lambda *args,**kwargs:None)
    monkeypatch.setattr('modules.printers.telemetry.bambu.broker_policy.resolve_bambu_broker_config',lambda host:SimpleNamespace(host=host,port=1,use_tls=False))
    result = module.fresh_bambu_status_is_idle({'ip':'127.0.0.1','serial':'synthetic','access_code':'synthetic'},time.monotonic()+1)
    assert result is (mode in ('idle','finished','partial_then_idle'))
    assert calls[-2:] == ['disconnect','stop']
    if mode in ('idle','finished','partial_then_idle'):
        assert ('subscribe','device/synthetic/report',0) in calls
        assert ('publish','device/synthetic/request',{'pushing':{'sequence_id':'0','command':'pushall'}},{'qos':0,'retain':False}) in calls


@pytest.mark.parametrize('mode',['idle','busy','malformed','oversize','other_topic','retained_only'])
def test_actual_owned_broker_fresh_report_through_spawned_worker(monkeypatch,mode):
    import paho.mqtt.client as mqtt
    from modules.printers.telemetry.live_replay import LocalBroker
    from modules.printers.dispatch_acquisition import bounded_status_is_idle
    broker = LocalBroker();broker.start();responder = None
    try:
        monkeypatch.setenv('ODIN_ALLOW_INSECURE_BAMBU_BROKER','1')
        monkeypatch.setenv('ODIN_BAMBU_INSECURE_BROKER_HOSTS','127.0.0.1')
        monkeypatch.setenv('ODIN_BAMBU_INSECURE_BROKER_PORT',str(broker.port))
        monkeypatch.setenv('ODIN_ITAR_MODE','0')
        responder = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,protocol=mqtt.MQTTv311)
        def on_message(client,userdata,message):
            request = json.loads(message.payload)
            if request == {'pushing':{'sequence_id':'0','command':'pushall'}}:
                if mode=='retained_only':return
                payload = {'print':{'command':'push_status','gcode_state':'RUNNING' if mode=='busy' else 'IDLE','print_error':False if mode=='malformed' else 0}}
                raw = b'x'*65537 if mode=='oversize' else json.dumps(payload)
                client.publish('device/other/report' if mode=='other_topic' else 'device/synthetic/report',raw,qos=1,retain=False)
        responder.on_message = on_message
        responder.connect(broker.host,broker.port)
        responder.subscribe('device/synthetic/request',qos=1);responder.loop_start()
        if mode=='retained_only':
            receipt=responder.publish('device/synthetic/report',b'{"print":{"command":"push_status","gcode_state":"IDLE","print_error":0}}',qos=1,retain=True)
            receipt.wait_for_publish(timeout=1)
            assert receipt.is_published()
        assert bounded_status_is_idle({'api_type':'bambu','ip':broker.host,'serial':'synthetic','access_code':''},time.monotonic()+3) is (mode=='idle')
    finally:
        # Stop the network loop before disconnecting: a disconnect lets paho's thread
        # exit and clear itself, racing loop_stop()'s join.
        if responder is not None:responder.loop_stop();responder.disconnect()
        broker.stop()
