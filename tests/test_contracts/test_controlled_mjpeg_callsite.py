"""Exercise the real relay hook, checking membership before each emitted frame."""
import asyncio
import httpx
from sqlalchemy import text
from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_controlled_guards import marked


def test_mjpeg_relay_hook_and_per_frame_membership(marked,monkeypatch):
    from modules.printers import mjpeg_relay as relay
    from modules.printers.controlled_camera import frame_guard
    marked.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1"));marked.commit()
    real_client=httpx.AsyncClient
    class Frames(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first'
            with marked.get_bind().begin() as c:
                c.execute(text('DELETE FROM controlled_audience_members WHERE user_id=1'))
            yield b'forbidden-second'
    def transport(request):
        if request.url.path=='/api/streams':return httpx.Response(200,json={'printer_1_mjpeg':{}})
        return httpx.Response(200,headers={'content-type':'multipart/x-mixed-replace; boundary=frame'},stream=Frames())
    monkeypatch.setattr(relay.httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(transport),**kw))
    viewer={'user_id':1,'token_id':None,'privileged':False}
    async def run():
        entries=[]
        response=await relay.open_mjpeg(1,check=lambda:viewer,sync=lambda:None,
            claim=lambda v,p:relay.claim_stream(entries,v,p),release=lambda e:relay.release_stream(entries,e),
            check_frame=frame_guard(marked.get_bind(),1))
        chunks=[c async for c in response.body_iterator]
        assert chunks==[b'first']
        assert entries==[]
    asyncio.run(run())
