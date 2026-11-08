"""Fixed local worker operations. Parent must register before ARM.

Only private anonymous pipes carry inputs/results. This worker is not a public
API, never runs supplied code, and offers no quiescence or remote clock proof.
"""
import os
import sys
import time
from core import sender_ipc
from core.sender_operation_protocol import validate_request,make_result

BOOTSTRAP_SECONDS=30.0


class _PowerReceipt:
    def __init__(self):self.value=None;self.sent=False
    def send(self,value):
        if self.sent:raise sender_ipc.ProtocolError()
        self.value=value;self.sent=True
    def close(self):pass


def execute_request(raw):
    request=validate_request(raw)
    message=request.message;inputs=message['inputs'];deadline=message['deadline']
    if message['operation']=='status_idle':
        try:
            if inputs['api_type']=='bambu':
                from modules.printers.dispatch_bambu_readiness import fresh_bambu_status_is_idle as acquire
            else:
                from modules.printers.dispatch_readiness import fresh_http_status_is_idle as acquire
            result={'idle':acquire(inputs,deadline) is True}
        except Exception:result={'idle':False}
    else:
        result={'status':'uncertain','power_state':None}
        try:
            from modules.printers.plug_power_acquisition import _http_power_worker,_mqtt_power_worker
            collector=_PowerReceipt()
            if inputs['type']=='mqtt':
                from core.mqtt_connection_snapshot import MqttConnectionSnapshot
                inputs=dict(inputs,connection=MqttConnectionSnapshot(**inputs['connection']))
                _mqtt_power_worker(collector,inputs,deadline)
                kind,status=collector.value
                if kind=='broker':result={'status':status,'power_state':None}
            else:
                _http_power_worker(collector,inputs,deadline)
                kind,observed=collector.value
                if kind=='observed' and type(observed) is bool:
                    result={'status':'accepted' if observed else 'refused','power_state':observed}
        except Exception:result={'status':'uncertain','power_state':None}
    return make_result(request,result)


def main(input_fd,result_fd):
    exit_code=2
    try:
        raw=sender_ipc.receive(input_fd,time.monotonic()+BOOTSTRAP_SECONDS)
        request=validate_request(raw)
        result=execute_request(request.message)
        sender_ipc.send(result_fd,result.message,request.message['deadline'])
        exit_code=0
    except Exception:
        # No raw exceptions, credentials or uncontrolled device text leave here.
        pass
    finally:
        for descriptor in (input_fd,result_fd):
            try:os.close(descriptor)
            except Exception:exit_code=2
    return exit_code


if __name__=='__main__':
    try:
        if len(sys.argv)!=3:raise ValueError()
        descriptors=[int(arg) for arg in sys.argv[1:]]
        if any(fd<=2 for fd in descriptors) or len(set(descriptors))!=2:raise ValueError()
    except Exception:sys.exit(2)
    sys.exit(main(*descriptors))
