"""Owned training labels must respect audience and valid geometry."""
import asyncio,json
import pytest
from sqlalchemy import text
from fastapi import HTTPException
from tests.test_contracts.test_education_review_workflow import review_db


class LabelRequest:
    def __init__(self, body):self.body=body
    async def json(self):return self.body


def seed(review_db, foreign=False):
    if foreign:
        review_db.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=10'))
        review_db.execute(text('UPDATE printers SET org_id=2 WHERE id=10'))
    review_db.execute(text("INSERT INTO vision_detections(id,printer_id,detection_type,confidence,status,bbox_json) VALUES(701,:printer,'spaghetti',0.9,'confirmed','[0,0,10,10]')"),{'printer':10 if foreign else 9})
    review_db.commit()


def test_foreign_tenant_cannot_relabel_training_frame(review_db):
    from modules.vision.routes.detections import label_training_data
    seed(review_db,foreign=True)
    try:
        asyncio.run(label_training_data(701,LabelRequest({'class':'detachment','bbox':[0,0,1,1]}),
            current_user={'id':3,'role':'admin','group_id':1},db=review_db))
    except HTTPException as error:
        assert error.status_code==404
    else:pytest.fail('Tenant administrator relabeled another tenant training detection')
    assert review_db.execute(text('SELECT detection_type FROM vision_detections WHERE id=701')).scalar_one()=='spaghetti'


@pytest.mark.parametrize('bbox',[[10,10,0,0],['invalid',0,1,1],[0,0,float('nan'),1],[],[0,0,1]])
def test_invalid_training_geometry_is_refused_without_mutation(review_db,bbox):
    from modules.vision.routes.detections import label_training_data
    seed(review_db)
    try:
        asyncio.run(label_training_data(701,LabelRequest({'class':'detachment','bbox':bbox,'expected_revision':0}),
            current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    except HTTPException as error:
        assert error.status_code in (400,422)
    else:pytest.fail('Invalid training geometry was persisted')
    assert review_db.execute(text('SELECT detection_type FROM vision_detections WHERE id=701')).scalar_one()=='spaghetti'


def test_training_list_excludes_foreign_tenant_before_pagination(review_db):
    from modules.vision.routes.detections import list_training_data
    seed(review_db,foreign=True)
    review_db.execute(text("UPDATE vision_detections SET frame_path='10/owned.jpg' WHERE id=701"));review_db.commit()
    rows=asyncio.run(list_training_data(printer_id=None,labeled=None,limit=50,offset=0,
        current_user={'id':3,'role':'admin','group_id':1},db=review_db))
    assert rows==[], 'Training list exposed another tenant detection'


def test_stats_do_not_expose_foreign_printer_identity(review_db):
    from modules.vision.routes.detections import get_vision_stats
    seed(review_db,foreign=True)
    result=asyncio.run(get_vision_stats(days=7,current_user={'id':3,'role':'admin','group_id':1},db=review_db))
    assert result['by_type']=={} and result['by_printer']==[], 'Statistics exposed another tenant activity'


def test_collected_training_frame_is_available_for_annotation(review_db,tmp_path,monkeypatch):
    import numpy as np
    from modules.vision import frame_storage
    from modules.vision.inference_engine import VisionInferenceEngine
    from modules.vision.detection_thread import PrinterVisionThread
    from modules.vision.routes.detections import list_training_data
    monkeypatch.setattr(frame_storage,'VISION_FRAMES_DIR',str(tmp_path/'frames'))
    from modules.vision import detection_thread
    monkeypatch.setattr(detection_thread,'engine',review_db.get_bind())
    from modules.vision import training_service
    monkeypatch.setattr(training_service,'VISION_FRAMES_DIR',str(tmp_path/'frames'))
    thread=PrinterVisionThread(9,'Owned',VisionInferenceEngine(),{'collect_training_data':1},0,None,'IDLE')
    monkeypatch.setattr(thread,'_capture_frame',lambda:np.full((32,32,3),100,dtype=np.uint8))
    thread._capture_and_analyze()
    assert len(list((tmp_path/'frames').rglob('*.jpg')))==1
    rows=asyncio.run(list_training_data(printer_id=9,labeled=None,limit=50,offset=0,
        current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    assert rows, 'Collected frame exists but has no annotation/list identity'
    assert rows[0]['detection_type']=='training_sample' and rows[0]['status']=='training'
    assert rows[0]['frame_path'].startswith('9/training/') and (tmp_path/'frames'/rows[0]['frame_path']).is_file()
    from modules.vision.routes.detections import list_vision_detections as list_detections,get_vision_stats
    admin={'id':3,'role':'admin','group_id':None}
    assert asyncio.run(list_detections(printer_id=9,detection_type=None,status_filter=None,limit=50,offset=0,current_user=admin,db=review_db))['total']==0
    assert asyncio.run(list_detections(printer_id=9,detection_type=None,status_filter='training',limit=50,offset=0,current_user=admin,db=review_db))['total']==1
    assert asyncio.run(get_vision_stats(days=7,current_user=admin,db=review_db))['by_type']=={}
    # Legacy detections with NULL status are not training samples and stay visible.
    review_db.execute(text("INSERT INTO vision_detections(printer_id,detection_type,confidence,status) VALUES(9,'spaghetti',0.9,NULL)"));review_db.commit()
    assert asyncio.run(list_detections(printer_id=9,detection_type=None,status_filter=None,limit=50,offset=0,current_user=admin,db=review_db))['total']==1
    assert asyncio.run(get_vision_stats(days=7,current_user=admin,db=review_db))['by_type']=={'spaghetti':1}
    from modules.vision import training_service as service
    labeled=service.label_frame(review_db,rows[0]['id'],{'class':'spaghetti','bbox':[8,8,24,24],'expected_revision':0},admin)
    assert labeled
    relisted=asyncio.run(list_training_data(printer_id=9,labeled=True,limit=50,offset=0,current_user=admin,db=review_db))
    assert [row['id'] for row in relisted]==[rows[0]['id']] and relisted[0]['annotation']['class']=='spaghetti'
    from modules.vision.routes.detections import serve_training_frame
    filename=rows[0]['frame_path'].split('/')[-1]
    served=asyncio.run(serve_training_frame(printer_id=9,filename=filename,current_user=admin,db=review_db))
    assert served.media_type=='image/jpeg' and served.body==(tmp_path/'frames'/rows[0]['frame_path']).read_bytes()


async def _owned_export(db, root, user):
    """Configure the normal service's storage root to an owned directory."""
    import io,zipfile
    from modules.vision import training_service
    from modules.vision.routes.detections import export_training_data
    previous=training_service.VISION_FRAMES_DIR
    training_service.VISION_FRAMES_DIR=str(root)
    try:
        response=await export_training_data(current_user=user,db=db)
        body=b''.join([chunk async for chunk in response.body_iterator])
    finally:training_service.VISION_FRAMES_DIR=previous
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return {name:archive.read(name) for name in archive.namelist()}


def _export_frame(db, root, *, foreign=False, annotation=None, kind='spaghetti'):
    import cv2
    import numpy as np
    import json
    seed(db,foreign=foreign)
    folder=root/('10' if foreign else '9');folder.mkdir(parents=True)
    assert cv2.imwrite(str(folder/'frame.jpg'),np.full((32,32,3),100,dtype=np.uint8))
    if annotation is not None:
        import hashlib
        annotation={'training_annotation':{'version':1,'revision':1,'class':annotation['label_class'],'negative':annotation['label_class']=='false_positive','bbox':annotation['label_bbox'],'image_sha256':hashlib.sha256((folder/'frame.jpg').read_bytes()).hexdigest(),'maintenance_only':True}}
    db.execute(text('UPDATE vision_detections SET frame_path=:path, detection_type=:kind, metadata_json=:metadata WHERE id=701'),
               {'path':folder.name+'/frame.jpg','kind':kind,'metadata':json.dumps(annotation) if annotation is not None else None})
    db.commit()


def test_export_uses_human_annotation_geometry(review_db,tmp_path):
    root=tmp_path/'frames'
    _export_frame(review_db,root,annotation={'label_class':'detachment','label_bbox':[8,8,24,24]})
    entries=asyncio.run(_owned_export(review_db,root,{'id':3,'role':'admin','group_id':None}))
    labels=[value.decode() for name,value in entries.items() if name.startswith('labels/')]
    assert labels==['2 0.500000 0.500000 0.500000 0.500000\n'], 'Export ignored human class/geometry'


def test_export_false_positive_is_empty_negative_label(review_db,tmp_path):
    root=tmp_path/'frames'
    _export_frame(review_db,root,kind='false_positive',annotation={'label_class':'false_positive','label_bbox':[]})
    entries=asyncio.run(_owned_export(review_db,root,{'id':3,'role':'admin','group_id':None}))
    labels=[value for name,value in entries.items() if name.startswith('labels/')]
    assert labels==[b''], 'False positive was exported as a positive spaghetti box'


def test_export_unannotated_review_is_not_ground_truth(review_db,tmp_path):
    root=tmp_path/'frames';_export_frame(review_db,root)
    entries=asyncio.run(_owned_export(review_db,root,{'id':3,'role':'admin','group_id':None}))
    assert not any(name.startswith(('images/','labels/')) for name in entries), 'Review status silently became ground truth'


def test_export_excludes_foreign_tenant_image_bytes(review_db,tmp_path):
    root=tmp_path/'frames'
    _export_frame(review_db,root,foreign=True,annotation={'label_class':'spaghetti','label_bbox':[0,0,10,10]})
    entries=asyncio.run(_owned_export(review_db,root,{'id':3,'role':'admin','group_id':1}))
    assert not any(name.startswith(('images/','labels/')) for name in entries), 'Export disclosed another tenant image bytes'


def _owned_frame_route(root):
    from modules.vision import training_service
    from modules.vision.routes.detections import serve_vision_frame
    async def owned(*args,**kwargs):
        previous=training_service.VISION_FRAMES_DIR
        training_service.VISION_FRAMES_DIR=str(root)
        try:return await serve_vision_frame(*args,**kwargs)
        finally:training_service.VISION_FRAMES_DIR=previous
    return owned


def test_frame_symlink_cannot_cross_printer_namespace(review_db,tmp_path):
    import cv2
    import numpy as np
    root=tmp_path/'frames';(root/'9').mkdir(parents=True);(root/'10').mkdir()
    assert cv2.imwrite(str(root/'10'/'foreign.jpg'),np.full((32,32,3),180,dtype=np.uint8))
    (root/'9'/'owned-name.jpg').symlink_to(root/'10'/'foreign.jpg')
    route=_owned_frame_route(root)
    try:
        asyncio.run(route(9,'owned-name.jpg',current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    except HTTPException as error:assert error.status_code==404
    else:pytest.fail('Frame endpoint accepted a symlink into another printer namespace')


def test_reassigned_printer_does_not_transfer_legacy_frame_to_new_tenant(review_db,tmp_path):
    import cv2
    import numpy as np
    root=tmp_path/'frames';(root/'10').mkdir(parents=True)
    assert cv2.imwrite(str(root/'10'/'historical.jpg'),np.full((32,32,3),100,dtype=np.uint8))
    seed(review_db,foreign=True)
    route=_owned_frame_route(root)
    try:
        asyncio.run(route(10,'historical.jpg',current_user={'id':5,'role':'admin','group_id':2},db=review_db))
    except HTTPException as error:assert error.status_code==404
    else:pytest.fail('Current printer reassignment transferred unknown historical image ownership')


@pytest.fixture
def annotation_fixture(review_db,tmp_path,monkeypatch):
    from modules.vision import training_service
    root=tmp_path/'frames';_export_frame(review_db,root)
    monkeypatch.setattr(training_service,'VISION_FRAMES_DIR',str(root))
    return training_service,root,{'id':3,'role':'admin','group_id':None}


def test_annotation_preserves_inference_and_unrelated_metadata(review_db,annotation_fixture):
    import json
    service,root,user=annotation_fixture
    review_db.execute(text("UPDATE vision_detections SET metadata_json='{"+'"inference_source":"owned"'+"}' WHERE id=701"));review_db.commit()
    before=review_db.execute(text('SELECT detection_type,bbox_json,confidence,status FROM vision_detections WHERE id=701')).one()
    result=service.label_frame(review_db,701,{'class':'detachment','bbox':[8,8,24,24],'expected_revision':0},user)
    assert result['annotation_revision']==1
    after=review_db.execute(text('SELECT detection_type,bbox_json,confidence,status FROM vision_detections WHERE id=701')).one()
    assert after==before
    stored=json.loads(review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one())
    assert stored['inference_source']=='owned'
    assert stored['training_annotation']['class']=='detachment'
    assert stored['training_annotation']['maintenance_only'] is True
    entries=asyncio.run(_owned_export(review_db,root,user))
    assert entries['labels/detection_701.txt']==b'2 0.500000 0.500000 0.500000 0.500000\n'


def test_stale_annotation_revision_cannot_overwrite(review_db,annotation_fixture):
    service,root,user=annotation_fixture
    service.label_frame(review_db,701,{'class':'detachment','bbox':[8,8,24,24],'expected_revision':0},user)
    before=review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one()
    with pytest.raises(HTTPException) as error:
        service.label_frame(review_db,701,{'class':'spaghetti','bbox':[0,0,10,10],'expected_revision':0},user)
    assert error.value.status_code==409
    assert review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one()==before


def test_concurrent_metadata_writer_wins_without_lost_update(review_db,annotation_fixture,monkeypatch):
    from sqlalchemy.orm import Session
    service,root,user=annotation_fixture;original=service.snapshot
    def interleaved(*args):
        result=original(*args)
        with Session(review_db.get_bind()) as other:
            other.execute(text("UPDATE vision_detections SET metadata_json='{"+'"concurrent":"retained"'+"}' WHERE id=701"));other.commit()
        return result
    monkeypatch.setattr(service,'snapshot',interleaved)
    with pytest.raises(HTTPException) as error:
        service.label_frame(review_db,701,{'class':'detachment','bbox':[8,8,24,24],'expected_revision':0},user)
    assert error.value.status_code==409
    assert review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one()=='{"concurrent":"retained"}'


@pytest.mark.parametrize('raw',['[]','null','invalid'])
def test_malformed_metadata_not_replaced(review_db,annotation_fixture,raw):
    service,root,user=annotation_fixture
    review_db.execute(text('UPDATE vision_detections SET metadata_json=:raw WHERE id=701'),{'raw':raw});review_db.commit()
    with pytest.raises(HTTPException) as error:
        service.label_frame(review_db,701,{'class':'detachment','bbox':[8,8,24,24],'expected_revision':0},user)
    assert error.value.status_code==400
    assert review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one()==raw


def test_changed_image_is_excluded_after_annotation(review_db,annotation_fixture):
    import cv2,numpy as np,json
    service,root,user=annotation_fixture
    service.label_frame(review_db,701,{'class':'detachment','bbox':[8,8,24,24],'expected_revision':0},user)
    assert cv2.imwrite(str(root/'9'/'frame.jpg'),np.full((32,32,3),180,dtype=np.uint8))
    entries=asyncio.run(_owned_export(review_db,root,user))
    assert not any(name.startswith(('images/','labels/')) for name in entries)
    assert json.loads(entries['export-report.json'])['excluded']==[{'id':701,'reason':'invalid_annotation_or_frame'}]


def test_image_geometry_must_fit_actual_dimensions(review_db,annotation_fixture):
    service,root,user=annotation_fixture
    with pytest.raises(HTTPException) as error:
        service.label_frame(review_db,701,{'class':'detachment','bbox':[0,0,40,40],'expected_revision':0},user)
    assert error.value.status_code==400
    assert review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one() is None


@pytest.fixture
def training_client(review_db,monkeypatch):
    import json,secrets
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from core.auth import hash_password
    from core.db import get_db
    from modules.vision.routes.detections import router
    monkeypatch.delenv('API_KEY',raising=False)
    app=FastAPI();app.include_router(router)
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    def issue(scopes,tenant):
        review_db.execute(text('UPDATE users SET group_id=:org WHERE id=3'),{'org':1 if tenant else None})
        raw='odin_'+secrets.token_urlsafe(32)
        review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'Owned annotation token',:hash,:prefix,:scopes)"),{'hash':hash_password(raw),'prefix':raw[:10],'scopes':json.dumps(scopes)})
        review_db.commit();return {'X-API-Key':raw}
    return TestClient(app),issue


@pytest.mark.parametrize('scopes,expected',[(['read'],403),([],401),(['write'],200)])
def test_real_token_annotation_write_scope(training_client,annotation_fixture,scopes,expected):
    client,issue=training_client
    response=client.post('/vision/training-data/701/label',headers=issue(scopes,False),json={'class':'detachment','bbox':[8,8,24,24],'expected_revision':0})
    assert response.status_code==expected


@pytest.mark.parametrize('endpoint',['/vision/training-data','/vision/training-data/export','/vision/stats','/vision/frames/9/frame.jpg'])
def test_write_only_token_cannot_read_training_or_frames(training_client,endpoint):
    client,issue=training_client
    assert client.get(endpoint,headers=issue(['write'],False)).status_code==403


def test_real_tenant_write_token_cannot_annotate_unknown_owner(training_client,annotation_fixture):
    client,issue=training_client
    response=client.post('/vision/training-data/701/label',headers=issue(['write'],True),json={'class':'detachment','bbox':[8,8,24,24],'expected_revision':0})
    assert response.status_code==404


def test_fifo_frame_refused_without_blocking(annotation_fixture):
    import os,subprocess,sys
    service,root,user=annotation_fixture
    os.mkfifo(root/'9'/'blocked.jpg')
    code="from modules.vision import training_service as s;import sys;s.VISION_FRAMES_DIR=sys.argv[1]\ntry:s.snapshot(9,'9/blocked.jpg')\nexcept ValueError:sys.exit(0)\nsys.exit(1)"
    result=subprocess.run([sys.executable,'-c',code,str(root)],capture_output=True,timeout=3)
    assert result.returncode==0,result.stderr.decode()


def test_image_association_drift_cannot_accept_annotation(review_db,annotation_fixture,monkeypatch):
    from sqlalchemy.orm import Session
    service,root,user=annotation_fixture;original=service.snapshot
    def interleaved(*args):
        result=original(*args)
        with Session(review_db.get_bind()) as other:
            other.execute(text("UPDATE vision_detections SET frame_path='9/replaced.jpg' WHERE id=701"));other.commit()
        return result
    monkeypatch.setattr(service,'snapshot',interleaved)
    with pytest.raises(HTTPException) as error:
        service.label_frame(review_db,701,{'class':'detachment','bbox':[8,8,24,24],'expected_revision':0},user)
    assert error.value.status_code==409
    assert review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one() is None


def test_actual_http_oversized_integer_geometry_is_refused(training_client,annotation_fixture):
    client,issue=training_client
    response=client.post('/vision/training-data/701/label',headers=issue(['write'],False),json={'class':'detachment','bbox':[0,0,10**1000,10],'expected_revision':0})
    assert response.status_code==400


@pytest.mark.parametrize('correction',[{'version':1.0},{'class':'unknown'},{'negative':True}])
def test_invalid_existing_annotation_cannot_be_replaced(review_db,annotation_fixture,correction):
    import json
    service,root,user=annotation_fixture
    service.label_frame(review_db,701,{'class':'detachment','bbox':[8,8,24,24],'expected_revision':0},user)
    value=json.loads(review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one())
    value['training_annotation'].update(correction)
    raw=json.dumps(value);review_db.execute(text('UPDATE vision_detections SET metadata_json=:raw WHERE id=701'),{'raw':raw});review_db.commit()
    with pytest.raises(HTTPException) as error:
        service.label_frame(review_db,701,{'class':'spaghetti','bbox':[0,0,10,10],'expected_revision':1},user)
    assert error.value.status_code==400
    assert review_db.execute(text('SELECT metadata_json FROM vision_detections WHERE id=701')).scalar_one()==raw


def test_continuous_training_collection_does_not_hit_export_or_listing_bounds(review_db,tmp_path,monkeypatch):
    from modules.vision import training_service as service
    from modules.vision.routes.detections import list_training_data
    admin={'id':3,'role':'admin','group_id':None}
    review_db.execute(text("INSERT INTO vision_detections(printer_id,detection_type,confidence,status,frame_path) "
        "SELECT 9,'training_sample',0,'training','9/training/f'||value||'.jpg' FROM json_each(:ids)"),{'ids':'['+','.join(str(i) for i in range(600))+']'})
    review_db.commit()
    archive=service.export_frames(review_db,admin)   # 600 unlabeled samples, none exportable: no 413
    import zipfile
    assert json.loads(zipfile.ZipFile(archive).read('export-report.json'))['exported']==0
    page=asyncio.run(list_training_data(printer_id=9,labeled=False,limit=50,offset=550,current_user=admin,db=review_db))
    assert len(page)==50
    assert asyncio.run(list_training_data(printer_id=9,labeled=True,limit=50,offset=0,current_user=admin,db=review_db))==[]


def test_training_sample_cannot_be_reviewed_into_a_detection(review_db):
    from fastapi import HTTPException
    from modules.vision.routes.detections import review_vision_detection as update_detection
    review_db.execute(text("INSERT INTO vision_detections(id,printer_id,detection_type,confidence,status,frame_path) VALUES(950,9,'training_sample',0,'training','9/training/x.jpg')"));review_db.commit()
    class Request:
        async def json(self):return {'status':'confirmed'}
    with pytest.raises(HTTPException) as error:
        asyncio.run(update_detection(detection_id=950,request=Request(),current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    assert error.value.status_code==409
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=950')).scalar_one()=='training'
