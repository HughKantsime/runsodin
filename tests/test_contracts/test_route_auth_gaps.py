"""Tenant and authentication fixes for printer slot, overlay and digest routes."""
import asyncio
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import review_db

OPERATOR={'id':3,'role':'operator','group_id':1}


@pytest.fixture
def printers(review_db):
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,is_active,camera_url) VALUES(61,'Foreign bambu','bambu',2,1,NULL),"
                           "(62,'Foreign RTSP','moonraker',2,1,'rtsp://camera-user:owned-secret@192.0.2.9/stream')"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) VALUES(61,1,'PLA','FF0000')"))
    review_db.commit()
    return review_db


def test_foreign_printer_slots_are_not_readable_or_writable(printers):
    from modules.printers import routes_bambu
    with pytest.raises(HTTPException) as read:
        asyncio.run(routes_bambu.get_unmatched_slots(printer_id=61,current_user=OPERATOR,db=printers))
    assert read.value.status_code==404
    with pytest.raises(HTTPException) as write:
        asyncio.run(routes_bambu.manual_slot_assignment(printer_id=61,slot_number=1,
            assignment=routes_bambu.ManualSlotAssignment(filament_type='PETG',color_hex='00FF00'),current_user=OPERATOR,db=printers))
    assert write.value.status_code==404
    printers.expire_all()
    assert printers.execute(text('SELECT filament_type,color_hex FROM filament_slots WHERE printer_id=61')).one()==('PLA','FF0000')
    own=asyncio.run(routes_bambu.get_unmatched_slots(printer_id=61,current_user={'id':1,'role':'admin','group_id':None},db=printers))
    assert own['printer_id']==61


def test_overlay_never_exposes_stored_camera_url(printers):
    from modules.printers.routes_crud import get_overlay_data
    payload=get_overlay_data(printer_id=62,db=printers)
    assert payload['camera_url']=='/api/cameras/62/stream'
    assert 'owned-secret' not in str(payload) and 'rtsp://' not in str(payload)


def test_digest_status_requires_authentication():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from modules.notifications.routes import alerts
    app=FastAPI();app.include_router(alerts.router)
    response=TestClient(app).get('/alerts/digest-status')
    assert response.status_code==401
