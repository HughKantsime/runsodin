"""Fresh Bambu observation for an owned acquisition worker, not print start."""
import json
import math
import re
import ssl
import threading
import time


def fresh_bambu_status_is_idle(creds: dict, deadline: float) -> bool:
    client = None
    state = {'connected':False,'subscribed':False,'ready':False,'failed':False,'finishing':False,'mid':None}
    lock, done = threading.Lock(), threading.Event()
    try:
        if not isinstance(creds, dict) or isinstance(deadline, bool) or not isinstance(deadline, (int,float)):
            return False
        deadline = float(deadline)
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            return False
        host, serial, code = creds.get('ip'), creds.get('serial'), creds.get('access_code')
        if not isinstance(host,str) or not host or not isinstance(serial,str) or re.fullmatch(r'[A-Za-z0-9_-]+',serial) is None or not isinstance(code,str):
            return False
        from core.itar import enforce_host_destination
        from modules.printers.telemetry.bambu.broker_policy import resolve_bambu_broker_config
        from modules.printers.telemetry.bambu.raw import BambuReport
        from modules.printers.dispatch_readiness import raw_status_is_idle
        import paho.mqtt.client as mqtt
        enforce_host_destination(host, scheme='mqtt')
        endpoint = resolve_bambu_broker_config(host)
        report_topic, request_topic = f'device/{serial}/report', f'device/{serial}/request'
        def value(reason):
            return getattr(reason,'value',reason)
        def fail():
            with lock:state['failed'] = True
            done.set()
        def on_connect(current,userdata,flags,reason,properties=None):
            if type(value(reason)) is not int or value(reason) != 0:
                fail();return
            with lock:state['connected'] = True
            result, mid = current.subscribe(report_topic,qos=0)
            if result != mqtt.MQTT_ERR_SUCCESS:
                fail();return
            with lock:state['mid'] = mid
        def on_subscribe(current,userdata,mid,reasons,properties=None):
            with lock:matches = state['connected'] and state['mid'] == mid
            if not matches or len(reasons) != 1 or type(value(reasons[0])) is not int or value(reasons[0]) not in (0,1,2):
                fail();return
            with lock:state['subscribed'] = True
            receipt = current.publish(request_topic,json.dumps({'pushing':{'sequence_id':'0','command':'pushall'}}),qos=0,retain=False)
            if receipt.rc != mqtt.MQTT_ERR_SUCCESS:fail()
        def on_disconnect(current,userdata,flags,reason,properties=None):
            with lock:
                state['connected'] = False
                if not state['finishing'] or value(reason) != 0 or getattr(flags,'is_disconnect_packet_from_server',False):state['failed'] = True
            done.set()
        def on_message(current,userdata,message):
            with lock:eligible = state['connected'] and state['subscribed'] and not state['failed']
            if not eligible or message.topic != report_topic or message.retain:
                return
            if time.monotonic() >= deadline or len(message.payload) > 65536:
                fail();return
            try:
                report = BambuReport.model_validate_json(message.payload)
                section = report.print
                # Incremental telemetry can arrive before the requested full
                # report. Never merge packets or finish on missing evidence.
                if section is None or section.command != 'push_status':
                    return
                packet = section.model_dump(mode='json', exclude_unset=True)
                if 'gcode_state' not in packet or 'print_error' not in packet:
                    return
                ready = raw_status_is_idle('bambu',packet)
            except Exception:
                fail();return
            with lock:state['ready'] = ready
            done.set()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,protocol=mqtt.MQTTv311)
        if code:client.username_pw_set('bblp',code)
        if endpoint.use_tls:
            # Preserve existing Bambu LAN self-signed-certificate policy.
            tls = ssl.create_default_context();tls.check_hostname=False;tls.verify_mode=ssl.CERT_NONE
            client.tls_set_context(tls)
        client.on_connect,client.on_subscribe = on_connect,on_subscribe
        client.on_message,client.on_disconnect = on_message,on_disconnect
        client.connect(endpoint.host,endpoint.port,keepalive=30)
        client.loop_start()
        done.wait(timeout=max(0.0,deadline-time.monotonic()))
        with lock:
            ready = state['ready'] and state['connected'] and state['subscribed'] and not state['failed'] and time.monotonic() < deadline
            state['finishing'] = True
        client.disconnect();client.loop_stop();client=None
        with lock:return ready and state['ready'] and not state['failed'] and time.monotonic() < deadline
    except Exception:
        return False
    finally:
        if client is not None:
            with lock:state['finishing'] = True
            try:client.disconnect()
            except Exception:pass
            try:client.loop_stop()
            except Exception:pass
