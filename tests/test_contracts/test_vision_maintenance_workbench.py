"""Versioned viewed-image annotations; legacy maintenance behavior unchanged."""
import asyncio,hashlib,json,io,zipfile
import pytest
from sqlalchemy import text
from fastapi import HTTPException
from tests.test_contracts.test_vision_training_acceptance import annotation_fixture,training_client
from tests.test_contracts.test_education_review_workflow import review_db


def body(root,**changes):
    return {'expected_image_sha256':hashlib.sha256((root/'9/frame.jpg').read_bytes()).hexdigest(),'expected_revision':0,'class':'detachment','negative':False,'bbox':[8,8,24,24],**changes}

@pytest.mark.parametrize('changes',[{'expected_revision':True},{'expected_revision':'0'},{'expected_revision':-1},{'expected_revision':2147483647},{'expected_image_sha256':'F'*64},{'negative':True},{'negative':0},{'class':'first_layer_failure'},{'bbox':[0,0,float('inf'),1]},{'bbox':[False,0,1,1]},{'extra':1}])
def test_closed_strict_request_refuses_before_write(annotation_fixture,changes):
    service,root,_=annotation_fixture
    with pytest.raises(HTTPException) as error:service.versioned_annotation(json.dumps(body(root,**changes)).encode())
    assert error.value.status_code==400

@pytest.mark.parametrize('declared',[None,'5000'])
def test_body_stream_limit_precedes_json_parser(monkeypatch,declared):
    from modules.vision import training_service as service
    class Request:
        headers={} if declared is None else {'content-length':declared}
        async def stream(self):yield b'x'*4000;yield b'x'*97
    monkeypatch.setattr(service.json,'loads',lambda *a,**k:pytest.fail('Parser must not be invoked'))
    with pytest.raises(HTTPException) as error:asyncio.run(service.read_annotation_body(Request()))
    assert error.value.status_code==413

@pytest.mark.parametrize('scopes,status',[(['write'],200),(['read'],403),(['write:models'],403),([],401)])
def test_actual_scoped_tokens_v2(training_client,annotation_fixture,scopes,status):
    client,issue=training_client;_,root,_=annotation_fixture
    response=client.post('/vision/training-data/701/annotation-v2',headers=issue(scopes,False),json=body(root))
    assert response.status_code==status

def test_real_tenant_refusal_and_changed_viewed_bytes_conflict(training_client,annotation_fixture,review_db):
    client,issue=training_client;_,root,_=annotation_fixture
    payload=body(root)
    response=client.post('/vision/training-data/701/annotation-v2',headers=issue(['write'],True),json=payload);assert response.status_code==404
    import cv2,numpy as np
    assert cv2.imwrite(str(root/'9/frame.jpg'),np.full((32,32,3),180,dtype=np.uint8))
    response=client.post('/vision/training-data/701/annotation-v2',headers=issue(['write'],False),json=payload);assert response.status_code==409
    assert review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one() is None

@pytest.mark.parametrize('label,negative,bbox,classid',[('spaghetti',False,[8,8,24,24],0),('first_layer',False,[8,8,24,24],1),('detachment',False,[8,8,24,24],2),('build_plate_empty',False,[8,8,24,24],3),('false_positive',True,[],None)])
def test_v2_labels_export_exact_human_geometry_without_rewriting_inference(training_client,annotation_fixture,review_db,label,negative,bbox,classid):
    client,issue=training_client;_,root,_=annotation_fixture
    payload=body(root,**{'class':label,'negative':negative,'bbox':bbox});headers=issue(['write'],False)
    response=client.post('/vision/training-data/701/annotation-v2',headers=headers,json=payload);assert response.status_code==200
    conflict=client.post('/vision/training-data/701/annotation-v2',headers=headers,json=payload);assert conflict.status_code==409
    assert review_db.execute(text('SELECT detection_type,confidence,status,bbox_json FROM vision_detections WHERE id=701')).one()==('spaghetti',.9,'confirmed','[0,0,10,10]')
    exported=client.get('/vision/training-data/export',headers=issue(['read'],False));assert exported.status_code==200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read('images/detection_701.jpg')==(root/'9/frame.jpg').read_bytes()
        expected=b'' if negative else f'{classid} 0.500000 0.500000 0.500000 0.500000\n'.encode()
        assert archive.read('labels/detection_701.txt')==expected
