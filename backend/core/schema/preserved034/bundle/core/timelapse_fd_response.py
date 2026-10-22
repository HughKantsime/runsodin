"""One admitted inode, bounded ASGI delivery, and unconditional descriptor cleanup."""
import asyncio,os,time
from fastapi import HTTPException
from starlette.responses import Response

LIMIT=(1<<63)-1

def admitted_range(value,size):
    if value is None:return 0,size-1,200
    def refuse():raise HTTPException(416,'Invalid byte range',headers={'Content-Range':f'bytes */{size}'})
    if not isinstance(value,str) or len(value)>256 or not value.isascii() or not value.startswith('bytes=') or ',' in value:refuse()
    parts=value[6:].split('-')
    if len(parts)!=2:refuse()
    a,b=parts
    for part in parts:
        if part and (not part.isdigit() or len(part)>20 or int(part)>LIMIT):refuse()
    if not a:
        if not b or int(b)==0 or size==0:refuse()
        start=max(0,size-int(b));end=size-1
    else:
        start=int(a);end=min(int(b),size-1) if b else size-1
        if start>=size or end<start:refuse()
    return start,end,206

class AdmittedFDResponse(Response):
    def __init__(self,fd,*,size,range_header=None,head=False,filename=None):
        self.fd=fd
        try:
            self.original=os.fstat(fd);self.start,self.end,code=admitted_range(range_header,size)
            self.head=head;self.deadline=time.monotonic()+300
            headers={'Accept-Ranges':'bytes','Content-Length':str(max(0,self.end-self.start+1))}
            if code==206:headers['Content-Range']=f'bytes {self.start}-{self.end}/{size}'
            if filename:headers['Content-Disposition']=f'attachment; filename="{filename}"'
            super().__init__(content=b'',status_code=code,headers=headers,media_type='video/mp4')
        except BaseException:self.close();raise
    def close(self):
        fd,self.fd=self.fd,None
        if fd is not None:os.close(fd)
    async def __call__(self,scope,receive,send):
        async def send_bounded(message):
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Media lease expired')
            await asyncio.wait_for(send(message),min(30,remaining))
        async def deliver():
            await send_bounded({'type':'http.response.start','status':self.status_code,'headers':self.raw_headers})
            position=self.start;left=max(0,self.end-self.start+1)
            while left and not self.head:
                now=os.fstat(self.fd)
                if any(getattr(now,k)!=getattr(self.original,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns')):raise RuntimeError('Admitted media changed')
                chunk=os.pread(self.fd,min(left,1<<20),position)
                if not chunk:raise RuntimeError('Admitted media truncated')
                after=os.fstat(self.fd)
                if any(getattr(after,k)!=getattr(self.original,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns')):raise RuntimeError('Admitted media changed')
                position+=len(chunk);left-=len(chunk)
                await send_bounded({'type':'http.response.body','body':chunk,'more_body':True})
            await send_bounded({'type':'http.response.body','body':b'','more_body':False})
        async def disconnected():
            while True:
                if (await receive())['type']=='http.disconnect':return
        tasks=[]
        try:
            tasks=[asyncio.create_task(deliver()),asyncio.create_task(disconnected())]
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:task.result()
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            self.close()
