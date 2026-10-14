"""Owned Vigil storage/capture/statistics assertions; no model downloads or hardware."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import cv2
import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.test_contracts.test_education_review_workflow import review_db


def test_owned_printer_thread_captures_and_decodes_exact_go2rtc_stream(monkeypatch):
    import httpx
    from modules.vision import detection_thread as module
    monkeypatch.setattr(module,'GO2RTC_BASE','http://127.0.0.1:1984')
    ok,encoded=cv2.imencode('.jpg',np.full((20,30,3),70,dtype=np.uint8));assert ok
    calls=[];frames=[]
    def get(url,**kwargs):calls.append((url,kwargs));return SimpleNamespace(status_code=200,content=encoded.tobytes())
    monkeypatch.setattr(httpx,'get',get)
    worker=module.PrinterVisionThread(9,'Owned camera',None,{'capture_interval_sec':0.01},2,33,'RUNNING')
    def capture():frames.append(worker._capture_frame());worker.stop()
    monkeypatch.setattr(worker,'_capture_and_analyze',capture)
    worker.start();worker.join(timeout=2)
    assert not worker.is_alive() and len(frames)==1 and frames[0].shape==(20,30,3)
    assert calls==[('http://127.0.0.1:1984/api/frame.jpeg?src=printer_9',{'timeout':5})]


@pytest.mark.parametrize('status,payload',[(500,b'owned error'),(200,b'not-jpeg')])
def test_failed_frame_read_never_becomes_image(monkeypatch,status,payload):
    import httpx
    from modules.vision.detection_thread import PrinterVisionThread
    monkeypatch.setattr(httpx,'get',lambda *a,**k:SimpleNamespace(status_code=status,content=payload))
    thread=PrinterVisionThread(9,'Owned',None,{},2,33,'RUNNING')
    assert thread._capture_frame() is None


def test_detection_frame_printer_directory_contains_real_jpeg(tmp_path,monkeypatch):
    from modules.vision import frame_storage
    monkeypatch.setattr(frame_storage,'VISION_FRAMES_DIR',str(tmp_path))
    relative=frame_storage.save_detection_frame(9,np.full((20,30,3),70,dtype=np.uint8),'spaghetti')
    assert relative.startswith('9/')
    assert cv2.imread(str(tmp_path/relative)).shape==(20,30,3)


def test_claim_same_second_frames_do_not_overwrite_previous_capture(tmp_path,monkeypatch):
    from modules.vision import frame_storage
    monkeypatch.setattr(frame_storage,'VISION_FRAMES_DIR',str(tmp_path))
    monkeypatch.setattr(frame_storage,'datetime',SimpleNamespace(now=lambda tz:datetime(2026,10,12,10,0,0,tzinfo=timezone.utc)))
    first=frame_storage.save_detection_frame(9,np.full((20,30,3),70,dtype=np.uint8),'spaghetti')
    original=(tmp_path/first).read_bytes()
    second=frame_storage.save_detection_frame(9,np.full((20,30,3),180,dtype=np.uint8),'spaghetti')
    assert first!=second and (tmp_path/first).read_bytes()==original


def test_claim_frame_save_does_not_follow_printer_directory_symlink(tmp_path,monkeypatch):
    from modules.vision import frame_storage
    root=tmp_path/'owned-root';root.mkdir();outside=tmp_path/'owned-outside';outside.mkdir()
    (root/'9').symlink_to(outside,target_is_directory=True)
    monkeypatch.setattr(frame_storage,'VISION_FRAMES_DIR',str(root))
    try:frame_storage.save_detection_frame(9,np.full((20,30,3),70,dtype=np.uint8),'spaghetti')
    except (ValueError,OSError):pass
    assert list(outside.iterdir())==[]


def test_claim_failed_codec_write_does_not_return_nonexistent_frame(tmp_path,monkeypatch):
    from modules.vision import frame_storage
    monkeypatch.setattr(frame_storage,'VISION_FRAMES_DIR',str(tmp_path))
    monkeypatch.setattr(cv2,'imencode',lambda *a,**k:(False,None))
    try:result=frame_storage.save_detection_frame(9,np.full((20,30,3),70,dtype=np.uint8),'spaghetti')
    except (ValueError,OSError):return
    assert result is None


@pytest.fixture
def stats_client(review_db):
    from modules.vision.routes import detections
    from modules.vision.models import VisionDetection
    from core.db import get_db
    from core.dependencies import get_current_user
    app=FastAPI();app.include_router(detections.router,prefix='/api')
    principal={'id':3,'username':'owned-review','role':'admin','group_id':None,'is_active':True,'_auth_kind':'session_jwt'}
    app.dependency_overrides[get_current_user]=lambda:principal
    def db():
        with Session(review_db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=db
    now=datetime.now(timezone.utc)
    review_db.add_all([VisionDetection(id=9401,printer_id=9,detection_type='spaghetti',confidence=.9,status='confirmed',created_at=now),
                      VisionDetection(id=9402,printer_id=9,detection_type='spaghetti',confidence=.8,status='dismissed',created_at=now),
                      VisionDetection(id=9403,printer_id=10,detection_type='detachment',confidence=.8,status='pending',created_at=now),
                      VisionDetection(id=9404,printer_id=9,detection_type='spaghetti',confidence=.8,status='confirmed',created_at=now-timedelta(days=8))])
    review_db.commit()
    with TestClient(app) as client:yield client,principal


def test_actual_stats_route_aggregates_window_type_status_printer_and_reviewed_ratio(stats_client):
    client,_=stats_client
    response=client.get('/api/vision/stats?days=7');assert response.status_code==200
    body=response.json()
    assert body['by_type']=={'spaghetti':2,'detachment':1}
    assert body['by_status']=={'confirmed':1,'dismissed':1,'pending':1}
    assert {row['printer_id']:row['count'] for row in body['by_printer']}=={9:2,10:1}
    assert body['accuracy_pct']==50.0 and body['total_reviewed']==2


def test_tenant_stats_cannot_inherit_unknown_legacy_ownership(stats_client):
    client,principal=stats_client;principal['group_id']=1
    response=client.get('/api/vision/stats?days=7');assert response.status_code==200
    assert response.json()['by_type']=={} and response.json()['by_printer']==[]
