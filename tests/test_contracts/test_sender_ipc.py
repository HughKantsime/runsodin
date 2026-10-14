"""Synthetic private pipes; no device/network calls or credential output."""
import json
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from core import sender_ipc as ipc


def pipe():
    r,w=os.pipe()
    os.set_blocking(r,False);os.set_blocking(w,False)
    return r,w


def receive_bytes(data):
    r,w=pipe()
    try:
        os.write(w,data);os.close(w);w=None
        return ipc.receive(r,time.monotonic()+1)
    finally:
        os.close(r)
        if w is not None:os.close(w)


def frame(data):return struct.pack('!I',len(data))+data


def test_actual_pipe_roundtrip():
    r,w=pipe()
    try:
        value={'id':'a'*32,'payload':'x'*60000,'ok':True}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(ipc.receive,r,time.monotonic()+2)
            ipc.send(w,value,time.monotonic()+2)
            os.close(w);w=None
            assert future.result()==value
    finally:
        os.close(r)
        if w is not None:os.close(w)


@pytest.mark.parametrize('raw',[
    b'',b'\x00',struct.pack('!I',0),struct.pack('!I',65537),
    frame(b'{'),frame(b'\xff'),frame(b'{"a":1,"a":2}'),
    frame(b'{"a":NaN}'),frame(b'{"a":Infinity}'),frame(b'{"a":1e999}'),
    frame(b'{}')+b'x',frame(b'{}')+frame(b'{}'),frame(b'['*9+b'0'+b']'*9),
])
def test_malformed_refused_with_fixed_error(raw):
    with pytest.raises(ipc.ProtocolError) as error:receive_bytes(raw)
    assert str(error.value)=='Invalid private sender message'


def test_waits_for_eof_not_just_complete_json():
    r,w=pipe()
    try:
        os.write(w,frame(b'{}'))
        with pytest.raises(ipc.ProtocolError):ipc.receive(r,time.monotonic()+.03)
    finally:os.close(r);os.close(w)


@pytest.mark.parametrize('value',[{'x':float('nan')},{'x':float('inf')},{'x':'x'*65536},{1:'invalid'}])
def test_bad_outgoing_refused_before_bytes(value):
    r,w=pipe()
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(w,value,time.monotonic()+1)
        with pytest.raises(BlockingIOError):os.read(r,1)
    finally:os.close(r);os.close(w)


def test_cyclic_outgoing_refused():
    value={};value['cycle']=value
    r,w=pipe()
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(w,value,time.monotonic()+1)
    finally:os.close(r);os.close(w)


@pytest.mark.parametrize('deadline',[True,float('nan'),float('inf'),0])
def test_invalid_deadline_refused(deadline):
    r,w=pipe()
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(w,{},deadline)
    finally:os.close(r);os.close(w)


def test_blocking_pipe_refused_without_changing_mode():
    r,w=os.pipe()
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(w,{},time.monotonic()+1)
        assert os.get_blocking(w)
    finally:os.close(r);os.close(w)


def test_read_direction_refused():
    r,w=pipe()
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(r,{},time.monotonic()+1)
    finally:os.close(r);os.close(w)


def test_fragmented_frame():
    r,w=pipe()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(ipc.receive,r,time.monotonic()+1)
            for part in (b'\x00',b'\x00\x00\x07',b'{"a"',b':1}'):
                os.write(w,part)
            os.close(w);w=None
            assert future.result()=={'a':1}
    finally:
        os.close(r)
        if w is not None:os.close(w)


def test_huge_integer_refused_before_serialization():
    r,w=pipe()
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(w,{'n':1<<100000},time.monotonic()+1)
        with pytest.raises(BlockingIOError):os.read(r,1)
    finally:os.close(r);os.close(w)


def test_duplicate_inherited_writer_prevents_acceptance():
    r,w=pipe();extra=os.dup(w)
    try:
        ipc.send(w,{},time.monotonic()+1);os.close(w);w=None
        with pytest.raises(ipc.ProtocolError):ipc.receive(r,time.monotonic()+.03)
    finally:
        os.close(r);os.close(extra)
        if w is not None:os.close(w)


def test_partial_write_budget_exhaustion_is_error(monkeypatch):
    r,w=pipe()
    original=ipc.os.write
    writes=[0]
    def partial(fd,data):
        writes[0]+=1
        if writes[0]==1:return original(fd,data[:8])
        raise BlockingIOError()
    monkeypatch.setattr(ipc.os,"write",partial)
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(w,{'data':'a'*60000},time.monotonic()+.03)
        assert os.read(r,65536)  # Partial bytes cannot be called a successful send.
    finally:os.close(r);os.close(w)


@pytest.mark.parametrize('phase',['ready','eof','decode','write'])
def test_expiry_after_io_readiness_is_not_accepted(monkeypatch,phase):
    r,w=pipe();clock=[100.0]
    try:
        if phase!='write':os.write(w,frame(b'{}'));os.close(w);w=None
        monkeypatch.setattr(ipc.time,'monotonic',lambda:clock[0])
        if phase=='ready':
            original=ipc.select.select
            def ready(*args):
                result=original(*args);clock[0]=102;return result
            monkeypatch.setattr(ipc.select,'select',ready)
        elif phase=='eof':
            original=ipc.os.read
            def read(*args):
                result=original(*args)
                if not result:clock[0]=102
                return result
            monkeypatch.setattr(ipc.os,'read',read)
        elif phase=='decode':
            original=ipc.json.loads
            def decode(*args,**kwargs):
                result=original(*args,**kwargs);clock[0]=102;return result
            monkeypatch.setattr(ipc.json,'loads',decode)
        else:
            original=ipc.os.write
            def write(*args):
                result=original(*args);clock[0]=102;return result
            monkeypatch.setattr(ipc.os,'write',write)
        with pytest.raises(ipc.ProtocolError):
            if phase=='write':ipc.send(w,{},101.0)
            else:ipc.receive(r,101.0)
    finally:
        os.close(r)
        if w is not None:os.close(w)


def test_named_fifo_refused(tmp_path):
    path=tmp_path/'channel';os.mkfifo(path)
    r=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
    w=os.open(path,os.O_WRONLY|os.O_NONBLOCK)
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(w,{},time.monotonic()+1)
    finally:os.close(r);os.close(w)


def test_aggregate_size_refused_before_serialization(monkeypatch):
    r,w=pipe()
    monkeypatch.setattr(ipc.json,'dumps',lambda *args,**kwargs:pytest.fail('Oversized tree serialized'))
    try:
        with pytest.raises(ipc.ProtocolError):ipc.send(w,['a'*60000]*100,time.monotonic()+1)
        with pytest.raises(BlockingIOError):os.read(r,1)
    finally:os.close(r);os.close(w)
