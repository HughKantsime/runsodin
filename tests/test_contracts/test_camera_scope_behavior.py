"""Actual camera/timelapse HTTP scopes, no physical camera or transcoder."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def camera_client(token_client,review_db,monkeypatch):
    from modules.printers import camera_routes
    client,issue=token_client
    client.app.include_router(camera_routes.router)
    calls=[]
    monkeypatch.setattr(camera_routes,'sync_go2rtc_config',lambda db:calls.append('sync'))
    monkeypatch.setattr(camera_routes,'get_camera_url',lambda printer:'rtsp://synthetic.invalid/camera')
    review_db.execute(text('UPDATE printers SET camera_enabled=1,is_active=1'))
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,frame_count) VALUES(199,9,'synthetic.mp4','ready',3)"));review_db.commit()
    return client,issue,calls

@pytest.mark.parametrize('path',['/cameras','/cameras/9/stream','/timelapses','/timelapses/199'])
def test_unrelated_token_cannot_read_camera_or_timelapse(camera_client,path):
    client,issue,calls=camera_client
    response=client.get(path,headers=issue(['write:jobs']))
    assert response.status_code==403
    assert calls==[]

@pytest.mark.parametrize('scope',['read:printers','write:jobs','read'])
def test_narrow_token_cannot_toggle_camera(camera_client,review_db,scope):
    client,issue,calls=camera_client
    response=client.patch('/cameras/9/toggle',headers=issue([scope]))
    assert response.status_code==403
    assert review_db.execute(text('SELECT camera_enabled FROM printers WHERE id=9')).scalar_one()==1
    assert calls==[]

@pytest.mark.parametrize('scope',['read:printers','read','admin'])
@pytest.mark.parametrize('path',['/cameras','/cameras/9/stream','/timelapses','/timelapses/199'])
def test_matching_read_scopes_keep_camera_metadata_usable(camera_client,path,scope):
    client,issue,_=camera_client
    assert client.get(path,headers=issue([scope])).status_code==200

@pytest.mark.parametrize('operation',['trim','speed'])
@pytest.mark.parametrize('scope',['read:printers','write:jobs'])
def test_narrow_token_cannot_start_media_editor(camera_client,scope,operation,monkeypatch):
    from modules.printers import camera_routes
    client,issue,calls=camera_client
    monkeypatch.setattr(camera_routes,'_check_ffmpeg',lambda:calls.append('ffmpeg_probe') or False)
    response=client.post(f'/timelapses/199/{operation}',headers=issue([scope]),json={'start_seconds':0,'end_seconds':1,'multiplier':2})
    assert response.status_code==403 and calls==[]

@pytest.mark.parametrize('field',['camera_enabled','is_active'])
def test_disabled_or_inactive_camera_has_no_direct_stream(camera_client,review_db,field):
    client,issue,calls=camera_client
    review_db.execute(text(f'UPDATE printers SET {field}=0 WHERE id=9'));review_db.commit()
    response=client.get('/cameras/9/stream',headers=issue(['read:printers']))
    assert response.status_code==404 and calls==[]

@pytest.mark.parametrize('operation,body',[
 ('trim',{'start_seconds':'bad','end_seconds':2}),
 ('trim',{'start_seconds':{},'end_seconds':2}),
 ('trim',{'start_seconds':True,'end_seconds':2}),
 ('trim',{'start_seconds':0,'end_seconds':None}),
 ('trim',{'start_seconds':2,'end_seconds':1}),
 ('trim',{'start_seconds':0,'end_seconds':10**400}),
 ('speed',{'multiplier':10**400}),
 ('speed',{'multiplier':'bad'}),('speed',{'multiplier':{}}),('speed',{'multiplier':True}),('speed',{'multiplier':None}),
])
def test_malformed_media_edit_returns_client_error_before_process(camera_client,operation,body,monkeypatch):
    from modules.printers import camera_routes
    client,issue,calls=camera_client
    monkeypatch.setattr(camera_routes,'_check_ffmpeg',lambda:True)
    monkeypatch.setattr(camera_routes,'_get_timelapse_path',lambda t:pytest.fail('invalid input must never resolve media file'))
    monkeypatch.setattr(camera_routes.subprocess,'run',lambda *a,**k:pytest.fail('invalid input must never start process'))
    client.raise_server_exceptions=False
    response=client.post(f'/timelapses/199/{operation}',headers=issue(['write:printers']),json=body)
    assert response.status_code in (400,422),response.text
    assert calls==[]

@pytest.mark.parametrize('path',['/timelapses/199/video','/timelapses/199/stream','/timelapses/199/download'])
def test_unrelated_token_cannot_resolve_private_media(camera_client,path,monkeypatch):
    from modules.printers import camera_routes
    client,issue,calls=camera_client
    monkeypatch.setattr(camera_routes,'_get_timelapse_path',lambda t:pytest.fail('unrelated token must not resolve private media'))
    # Video has its own resolver; a non-existing synthetic filename still must be denied before lookup.
    assert client.get(path,headers=issue(['write:jobs'])).status_code==403


def test_read_only_token_cannot_delete_timelapse_record(camera_client,review_db):
    client,issue,_=camera_client
    response=client.delete('/timelapses/199',headers=issue(['read:printers']))
    assert response.status_code==403
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapses WHERE id=199')).scalar_one()==1


def test_missing_printer_fails_closed_for_timelapse_detail(camera_client,review_db):
    client,issue,_=camera_client
    # Simulate an orphan retained recording in a legacy import, without weakening the schema.
    from modules.printers import camera_routes
    class OrphanQuery:
        def filter(self,*a):return self
        def first(self):return None
    # The real recording exists; only the missing parent query is modeled at the parent boundary.
    from sqlalchemy.orm import Session
    original_query=Session.query
    def query(self,entity,*args,**kwargs):
        if entity is camera_routes.Printer:return OrphanQuery()
        return original_query(self,entity,*args,**kwargs)
    from unittest.mock import patch
    with patch.object(Session,'query',query):
        response=client.get('/timelapses/199',headers=issue(['read:printers']))
    assert response.status_code==404

@pytest.mark.parametrize('path',['stream','download'])
def test_matching_token_serves_owned_media_bytes(camera_client,path,tmp_path,monkeypatch):
    from modules.printers import camera_routes
    client,issue,_=camera_client
    media=tmp_path/'synthetic.mp4';media.write_bytes(b'SYNTHETIC_MEDIA_BYTES')
    monkeypatch.setattr(camera_routes,'_get_timelapse_path',lambda t:media)
    response=client.get(f'/timelapses/199/{path}',headers=issue(['read:printers']))
    assert response.status_code==200 and response.content==b'SYNTHETIC_MEDIA_BYTES'
    assert response.headers['content-type']=='video/mp4'
    if path=='download':assert 'odin_timelapse_199.mp4' in response.headers['content-disposition']

@pytest.mark.parametrize('scope',['write:printers','write','admin'])
def test_authorized_camera_toggle_persists(camera_client,review_db,scope):
    client,issue,_=camera_client
    response=client.patch('/cameras/9/toggle',headers=issue([scope]))
    assert response.status_code==200 and response.json()['camera_enabled'] is False
    assert review_db.execute(text('SELECT camera_enabled FROM printers WHERE id=9')).scalar_one()==0

@pytest.mark.parametrize('field',['camera_enabled','is_active'])
def test_disabled_or_inactive_camera_never_proxies_webrtc(camera_client,review_db,field,monkeypatch):
    from modules.printers import camera_routes
    client,issue,calls=camera_client
    review_db.execute(text(f'UPDATE printers SET {field}=0 WHERE id=9'));review_db.commit()
    monkeypatch.setattr(camera_routes.httpx,'AsyncClient',lambda *a,**k:pytest.fail('disabled camera must never contact signaling peer'))
    response=client.post('/cameras/9/webrtc',headers=issue(['read:printers']),content='SYNTHETIC_SDP')
    assert response.status_code==404 and calls==[]
