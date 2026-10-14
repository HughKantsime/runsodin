"""Historical detections cannot transfer ownership or resume unrelated jobs."""
import asyncio
import pytest
from sqlalchemy import text
from fastapi import HTTPException
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_vision_training_acceptance import seed,LabelRequest,training_client


@pytest.fixture
def command_calls(monkeypatch):
    from modules.printers import route_utils
    calls=[]
    monkeypatch.setattr(route_utils,'_send_printer_command',lambda printer,command:calls.append((printer.id,command)) or True)
    return calls


@pytest.mark.parametrize('operation',['list','detail','review'])
def test_unknown_historical_detection_not_transferred_with_printer(review_db,operation):
    from modules.vision.routes import detections
    seed(review_db,foreign=True)
    user={'id':5,'role':'admin','group_id':2}
    if operation=='list':
        result=asyncio.run(detections.list_vision_detections(printer_id=None,detection_type=None,status_filter=None,limit=50,offset=0,current_user=user,db=review_db))
        assert result=={'items':[],'total':0}
    else:
        with pytest.raises(HTTPException) as error:
            if operation=='detail':asyncio.run(detections.get_vision_detection(701,current_user=user,db=review_db))
            else:asyncio.run(detections.review_vision_detection(701,LabelRequest({'status':'dismissed'}),current_user=user,db=review_db))
        assert error.value.status_code==404
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar_one()=='confirmed'


@pytest.mark.parametrize('state',['IDLE','FINISH','FAILED','OFFLINE'])
def test_dismiss_does_not_send_resume_to_nonpaused_printer(review_db,command_calls,state):
    from modules.vision.routes.detections import dismiss_and_resume
    seed(review_db);review_db.execute(text('UPDATE printers SET gcode_state=:state WHERE id=9'),{'state':state});review_db.commit()
    try:asyncio.run(dismiss_and_resume(701,current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    except HTTPException as error:assert error.status_code==409
    assert command_calls==[], 'Dismiss sent physical resume to a nonpaused printer'


def test_unlinked_legacy_detection_cannot_resume_current_paused_print(review_db,command_calls):
    from modules.vision.routes.detections import dismiss_and_resume
    seed(review_db);review_db.execute(text("UPDATE printers SET gcode_state='PAUSE' WHERE id=9"));review_db.commit()
    with pytest.raises(HTTPException) as error:
        asyncio.run(dismiss_and_resume(701,current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    assert error.value.status_code==409
    assert command_calls==[]
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar_one()=='confirmed'


@pytest.mark.parametrize('operation',['review','dismiss-and-resume'])
def test_read_token_cannot_mutate_detection_or_resume(training_client,review_db,command_calls,operation):
    seed(review_db)
    client,issue=training_client
    headers=issue(['read'],False)
    if operation=='review':response=client.patch('/vision/detections/701',headers=headers,json={'status':'dismissed'})
    else:response=client.post('/vision/detections/701/dismiss-and-resume',headers=headers)
    assert response.status_code==403
    assert command_calls==[]
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar_one()=='confirmed'


def test_platform_review_preserves_capture_and_inference(review_db):
    from modules.vision.routes.detections import review_vision_detection
    seed(review_db)
    before=review_db.execute(text('SELECT detection_type,bbox_json,confidence,frame_path,metadata_json FROM vision_detections WHERE id=701')).one()
    result=asyncio.run(review_vision_detection(701,LabelRequest({'status':'dismissed'}),current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    assert result=={'id':701,'status':'dismissed'}
    assert review_db.execute(text('SELECT detection_type,bbox_json,confidence,frame_path,metadata_json FROM vision_detections WHERE id=701')).one()==before
    assert review_db.execute(text('SELECT status,reviewed_by FROM vision_detections WHERE id=701')).one()==('dismissed',3)


@pytest.mark.parametrize('body',[[],None,{'status':'running'}])
def test_review_body_validation_no_mutation(review_db,body):
    from modules.vision.routes.detections import review_vision_detection
    seed(review_db)
    with pytest.raises(HTTPException) as error:
        asyncio.run(review_vision_detection(701,LabelRequest(body),current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    assert error.value.status_code==400
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar_one()=='confirmed'


@pytest.mark.parametrize('field,value',[('frame_path','9/changed.jpg'),('status','pending'),('print_job_id',41)])
def test_review_refuses_interleaved_association_or_status_change(review_db,field,value):
    from sqlalchemy.orm import Session
    from modules.vision.routes.detections import review_vision_detection
    seed(review_db)
    class Interleaved(Session):
        def execute(self,statement,*args,**kwargs):
            if str(statement).startswith('UPDATE vision_detections'):
                with Session(review_db.get_bind()) as other:
                    other.execute(text('UPDATE vision_detections SET '+field+'=:value WHERE id=701'),{'value':value});other.commit()
            return super().execute(statement,*args,**kwargs)
    with Interleaved(review_db.get_bind()) as db:
        with pytest.raises(HTTPException) as error:
            asyncio.run(review_vision_detection(701,LabelRequest({'status':'dismissed'}),current_user={'id':3,'role':'admin','group_id':None},db=db))
    assert error.value.status_code==409
    assert review_db.execute(text('SELECT reviewed_by FROM vision_detections WHERE id=701')).scalar_one() is None


@pytest.mark.parametrize('ack_lost',[False,True])
def test_review_commit_uncertainty_reports_fixed_503(review_db,ack_lost):
    from sqlalchemy.orm import Session
    from modules.vision.routes.detections import review_vision_detection
    seed(review_db)
    class FaultSession(Session):
        def commit(self):
            if ack_lost:super().commit()
            raise RuntimeError('Owned commit fault')
    with FaultSession(review_db.get_bind()) as db:
        with pytest.raises(HTTPException) as error:
            asyncio.run(review_vision_detection(701,LabelRequest({'status':'dismissed'}),current_user={'id':3,'role':'admin','group_id':None},db=db))
    assert error.value.status_code==503
    assert 'refresh before retrying' in error.value.detail
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar_one()==('dismissed' if ack_lost else 'confirmed')
