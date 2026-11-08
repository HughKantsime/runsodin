"""One bounded private JSON frame. No execution authority or cleanup proof.

Caller exclusively owns a nonblocking anonymous pipe descriptor. Send does not
close it: caller must close the writer so receive can confirm frame termination.
"""
import fcntl
import json
import math
import os
import select
import stat
import struct
import sys
import time

MAX_BYTES=65536
MAX_DEPTH=8
MAX_NODES=8192
MAX_INTEGER_BITS=256


class ProtocolError(ValueError):
    def __init__(self):super().__init__('Invalid private sender message')


def _check_deadline(deadline):
    if type(deadline) not in (int,float) or not math.isfinite(deadline) or time.monotonic()>=deadline:
        raise ProtocolError()


def _descriptor(fd,write):
    if type(fd) is not int or fd<=2:raise ProtocolError()
    value=os.fstat(fd)
    flags=fcntl.fcntl(fd,fcntl.F_GETFL)
    if not stat.S_ISFIFO(value.st_mode) or not flags&os.O_NONBLOCK:
        raise ProtocolError()
    if flags&os.O_ACCMODE != (os.O_WRONLY if write else os.O_RDONLY):
        raise ProtocolError()
    if sys.platform=='darwin':anonymous=value.st_nlink==0
    elif sys.platform.startswith('linux'):anonymous=os.readlink('/proc/self/fd/'+str(fd)).startswith('pipe:[')
    else:anonymous=False
    if not anonymous:raise ProtocolError()


def _validate(value):
    remaining=MAX_NODES
    content_budget=MAX_BYTES
    active=set()
    def visit(item,depth):
        nonlocal remaining,content_budget
        remaining-=1
        if remaining<0:raise ProtocolError()
        kind=type(item)
        # Bound aggregate serializer work before allocating encoded JSON.
        # Escaping/UTF-8 expansion is at most six bytes per character; final
        # byte cap still applies after serialization.
        if kind is str:content_budget-=len(item)+2
        elif kind is int:content_budget-=80
        elif kind is float:content_budget-=32
        elif kind in (dict,list):content_budget-=2+2*len(item)
        else:content_budget-=5
        if content_budget<0:raise ProtocolError()
        if kind is str:
            if len(item)>MAX_BYTES:raise ProtocolError()
        elif kind is int:
            if item.bit_length()>MAX_INTEGER_BITS:raise ProtocolError()
        elif kind is float:
            if not math.isfinite(item):raise ProtocolError()
        elif item is None or kind is bool:pass
        elif kind in (dict,list):
            if depth>=MAX_DEPTH or len(item)>MAX_NODES or id(item) in active:
                raise ProtocolError()
            active.add(id(item))
            try:
                if kind is dict:
                    for key,child in item.items():
                        if type(key) is not str:raise ProtocolError()
                        visit(key,depth+1);visit(child,depth+1)
                else:
                    for child in item:visit(child,depth+1)
            finally:active.remove(id(item))
        else:raise ProtocolError()
    visit(value,0)


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ProtocolError()
        result[key]=value
    return result


def _constant(_):raise ProtocolError()


def _ready(fd,write,deadline):
    _check_deadline(deadline)
    selected=select.select([] if write else [fd],[fd] if write else [],[],deadline-time.monotonic())
    _check_deadline(deadline)
    if not selected[1 if write else 0]:raise ProtocolError()


def send(fd,value,deadline):
    """Write a frame; caller must close writer. Partial failure is uncertain."""
    try:
        _check_deadline(deadline);_descriptor(fd,True);_validate(value)
        data=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        if not 1<=len(data)<=MAX_BYTES:raise ProtocolError()
        framed=struct.pack('!I',len(data))+data
        offset=0
        while offset<len(framed):
            _ready(fd,True,deadline);_check_deadline(deadline)
            try:count=os.write(fd,framed[offset:])
            except (BlockingIOError,InterruptedError):continue
            _check_deadline(deadline)
            if count<=0:raise ProtocolError()
            offset+=count
    except Exception:
        raise ProtocolError() from None


def receive(fd,deadline):
    """Accept exactly one complete frame only after EOF before deadline."""
    try:
        _check_deadline(deadline);_descriptor(fd,False)
        buffer=bytearray();expected=None
        while True:
            _ready(fd,False,deadline);_check_deadline(deadline)
            try:chunk=os.read(fd,min(4096,MAX_BYTES+5-len(buffer)))
            except (BlockingIOError,InterruptedError):continue
            _check_deadline(deadline)
            if not chunk:break
            buffer.extend(chunk)
            if expected is None and len(buffer)>=4:
                expected=struct.unpack('!I',buffer[:4])[0]
                if not 1<=expected<=MAX_BYTES:raise ProtocolError()
            if expected is not None and len(buffer)>expected+4:raise ProtocolError()
        if expected is None or len(buffer)!=expected+4:raise ProtocolError()
        value=json.loads(buffer[4:].decode('utf-8'),object_pairs_hook=_pairs,parse_constant=_constant)
        _validate(value);_check_deadline(deadline)
        return value
    except Exception:
        raise ProtocolError() from None
