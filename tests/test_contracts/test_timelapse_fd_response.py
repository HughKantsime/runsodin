import asyncio,os
import pytest
from fastapi import HTTPException
from core.timelapse_fd_response import admitted_range,AdmittedFDResponse

@pytest.mark.parametrize('value,expected',[(None,(0,9,200)),('bytes=2-4',(2,4,206)),('bytes=-3',(7,9,206)),('bytes=5-',(5,9,206)),('bytes=0-100',(0,9,206))])
def test_range(value,expected):assert admitted_range(value,10)==expected
@pytest.mark.parametrize('value',['bytes=10-','bytes=-0','bytes=3-2','bytes=1-2,3-4','bytes=','bytes='+('1'*21)+'-','bytes=9223372036854775808-','bytes=١-2','x'*257])
def test_invalid_range(value):
 with pytest.raises(HTTPException) as err:admitted_range(value,10)
 assert err.value.status_code==416

def response(tmp_path,**kw):
 p=tmp_path/'owned.mp4';p.write_bytes(b'0123456789');fd=os.open(p,os.O_RDONLY);return AdmittedFDResponse(fd,size=10,**kw),fd,p

def closed(fd):
 with pytest.raises(OSError):os.fstat(fd)

@pytest.mark.parametrize('head',[False,True])
def test_original_inode_after_path_replacement_and_head(tmp_path,head):
 r,fd,p=response(tmp_path,range_header='bytes=2-4',head=head);p.unlink();p.write_bytes(b'foreign');messages=[]
 async def send(message):messages.append(message)
 async def receive():await asyncio.sleep(999)
 asyncio.run(r({},receive,send));closed(fd)
 assert b''.join(m.get('body',b'') for m in messages)==(b'' if head else b'234')
 assert messages[0]['status']==206

def test_start_failure_closes_without_iterator(tmp_path):
 r,fd,_=response(tmp_path)
 async def send(message):raise RuntimeError('start fault')
 async def receive():await asyncio.sleep(999)
 with pytest.raises(RuntimeError):asyncio.run(r({},receive,send))
 closed(fd)

def test_constructor_refusal_closes(tmp_path):
 p=tmp_path/'owned';p.write_bytes(b'x');fd=os.open(p,os.O_RDONLY)
 with pytest.raises(HTTPException):AdmittedFDResponse(fd,size=1,range_header='bytes=9-')
 closed(fd)

def test_disconnect_closes(tmp_path):
 r,fd,_=response(tmp_path)
 async def send(message):await asyncio.sleep(999)
 async def receive():return {'type':'http.disconnect'}
 asyncio.run(r({},receive,send));closed(fd)

def test_expired_lease_and_content_drift_close(tmp_path):
 r,fd,p=response(tmp_path);r.deadline=0
 async def send(message):pass
 async def receive():await asyncio.sleep(999)
 with pytest.raises(TimeoutError):asyncio.run(r({},receive,send))
 closed(fd)
 r,fd,p=response(tmp_path);p.write_bytes(b'changed')
 with pytest.raises(RuntimeError):asyncio.run(r({},receive,send))
 closed(fd)

def test_cancellation_closes_before_body_starts(tmp_path):
 r,fd,_=response(tmp_path)
 async def exercise():
  async def send(message):await asyncio.sleep(999)
  async def receive():await asyncio.sleep(999)
  task=asyncio.create_task(r({},receive,send));await asyncio.sleep(0);task.cancel()
  with pytest.raises(asyncio.CancelledError):await task
 asyncio.run(exercise());closed(fd)
