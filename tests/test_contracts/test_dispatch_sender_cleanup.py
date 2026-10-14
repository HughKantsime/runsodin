"""Actual command-session cleanup, isolated owned threads; no device I/O."""
import threading
from types import SimpleNamespace
import pytest
from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig

@pytest.fixture
def config():
    return BambuAdapterConfig(printer_id='synthetic-owned',serial='SYNTHETIC',host='127.0.0.1',port=9,access_code='',use_tls=False)


def test_partial_command_loop_start_keeps_client_owned_until_cleanup(config,monkeypatch,caplog):
    from modules.printers.telemetry.bambu import commands,session
    end=threading.Event();events=[]
    thread=threading.Thread(target=lambda:end.wait(5),name='owned-synthetic-command',daemon=True)
    def start():thread.start();raise RuntimeError('synthetic-private-marker')
    def stop():events.append('loop_stop');end.set();thread.join(2)
    fake=SimpleNamespace(connect=lambda *a,**kw:events.append('connect'),loop_start=start,
        disconnect=lambda:events.append('disconnect'),loop_stop=stop)
    monkeypatch.setattr(commands.BambuCommandAdapter,'_client_factory',staticmethod(lambda:fake))
    try:
        assert session.run_command(config,'start_print','synthetic.3mf') is False
        assert not thread.is_alive()
        assert events==['connect','disconnect','loop_stop']
        assert 'synthetic-private-marker' not in caplog.text
    finally:
        end.set()
        if thread.ident is not None:thread.join(2)


@pytest.mark.parametrize('failure',['upload','connect_false','connect_exception','start_exception','success'])
def test_legacy_actual_dispatch_always_disconnects_owned_client(monkeypatch,failure):
    from modules.printers import dispatch
    from modules.printers.adapters import bambu
    calls=[]
    class Fake:
        def __init__(self,**kw):pass
        def upload_file(self,*a):calls.append('upload');return failure!='upload'
        def connect(self):
            calls.append('connect')
            if failure=='connect_exception':raise RuntimeError('synthetic')
            return failure!='connect_false'
        def start_print(self,*a,**kw):
            calls.append('start')
            if failure=='start_exception':raise RuntimeError('synthetic')
            return True
        def disconnect(self):calls.append('disconnect')
    monkeypatch.setattr(bambu,'BambuPrinter',Fake);monkeypatch.setattr(dispatch,'_ws',lambda *a:None)
    monkeypatch.setattr(dispatch.time,'sleep',lambda *a:None)
    creds={'ip':'127.0.0.1','serial':'SYNTHETIC','access_code':''}
    if failure in ['connect_exception','start_exception']:
        with pytest.raises(Exception):dispatch._dispatch_bambu_legacy(53,'synthetic.3mf','synthetic.3mf',creds)
    else:
        result=dispatch._dispatch_bambu_legacy(53,'synthetic.3mf','synthetic.3mf',creds)
        assert result[0] is (failure=='success')
    assert calls[-1]=='disconnect' and calls.count('disconnect')==1


def test_legacy_disconnect_attempts_both_cleanup_operations_on_fault():
    from modules.printers.adapters.bambu import BambuPrinter
    events=[]
    printer=BambuPrinter(ip='127.0.0.1',serial='SYNTHETIC',access_code='')
    def disconnect():events.append('disconnect');raise RuntimeError('synthetic')
    printer._client=SimpleNamespace(disconnect=disconnect,loop_stop=lambda:events.append('loop_stop'))
    with pytest.raises(RuntimeError):printer.disconnect()
    assert events==['disconnect','loop_stop']


@pytest.mark.parametrize('phase',['start','method','cleanup'])
def test_command_session_cleanup_and_sanitized_failures(config,monkeypatch,caplog,phase):
    from modules.printers.telemetry.bambu import session
    calls=[]
    class Fake:
        def __init__(self,*a):pass
        def start(self):
            calls.append('start')
            if phase=='start':raise RuntimeError('synthetic-private-marker')
        def is_connected(self):return True
        def start_print(self,*a):
            calls.append('command')
            if phase=='method':raise RuntimeError('synthetic-private-marker')
            return True
        def stop(self):
            calls.append('stop')
            if phase=='cleanup':raise RuntimeError('synthetic-private-marker')
    monkeypatch.setattr(session,'BambuCommandAdapter',Fake)
    if phase=='cleanup':
        with pytest.raises(Exception):session.run_command(config,'start_print','synthetic.3mf')
    else:assert session.run_command(config,'start_print','synthetic.3mf') is False
    assert calls[-1]=='stop' and calls.count('stop')==1
    assert 'synthetic-private-marker' not in caplog.text
