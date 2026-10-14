"""Actual spawned Linux child verifies root identity and its live namespace.

Network result is a software fixture. No device or DB authority acceptance.
"""
import multiprocessing
import os
import sys
import time
from types import SimpleNamespace
from dataclasses import replace

sys.path.insert(0,'/source/backend')
from core.runtime_identity import read_runtime_identity
from modules.printers.telemetry.bambu import trusted_ams_acquisition as module
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact


def child(channel,expected,wrong_domain):
    calls=[];outputs=[]
    def fixture(inner,*args):
        calls.append(True)
        inner.put({'snapshot':RawAMSSnapshot('a'*32,'b'*32,1,time.monotonic(),
            'c'*64,1,(RawTrayFact(1,0,0,'PETG','reported'),), idle_without_error=True),'error':None})
    module._network_worker=fixture
    local=read_runtime_identity()
    runtime=replace(expected,clock_domain='foreign-namespace') if wrong_domain else expected
    module._trusted_network_worker(SimpleNamespace(put=outputs.append),None,'c'*64,1,[1],
        'a'*32,time.monotonic()+3,runtime=runtime)
    if wrong_domain:
        assert not calls and outputs[0]['snapshot'] is None
        assert outputs[0]['error']=='AMS runtime evidence unavailable'
    else:
        assert calls==[True] and outputs[0]['error'] is None
        assert outputs[0]['snapshot'].idle_without_error is True
        assert outputs[0]['clock_domain']==local.clock_domain==expected.clock_domain
        assert outputs[0]['runtime_incarnation']==expected.incarnation
    channel.put(True)


def main():
    assert os.geteuid()==10001
    current=read_runtime_identity()
    context=multiprocessing.get_context('spawn')
    for wrong in (False,True):
        channel=context.Queue(1)
        process=context.Process(target=child,args=(channel,current,wrong))
        try:
            process.start();process.join(10)
            assert not process.is_alive() and process.exitcode==0
            assert channel.get(timeout=1) is True
        finally:
            if process.is_alive():process.kill();process.join(2)
            channel.close();channel.join_thread();process.close()
    print('trusted-ams-native-child-binding-pass; software network fixture only')


if __name__=='__main__':
    main()
