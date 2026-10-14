"""Actual HTTP dedicated projection and canonical Education/file audiences."""
import base64
from io import BytesIO
from types import SimpleNamespace
import pytest
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db


def picture():
    output=BytesIO();Image.new('RGB',(16,16),'red').save(output,format='PNG')
    return base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def recent_app(review_db,monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.jobs.routes import router
    from modules.organizations.education_review_service import archive_audience_sql,assert_file_preview_allowed
    current={'user':{'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'}}
    provider=SimpleNamespace(archive_audience_sql=archive_audience_sql,assert_file_preview_allowed=assert_file_preview_allowed)
    monkeypatch.setattr(registry,'get_provider',lambda name:provider if name=='EducationPolicyProvider' else None)
    app=FastAPI();app.include_router(router,prefix='/api')
    app.dependency_overrides[get_current_user]=lambda:current['user']
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    return TestClient(app),current


def job(db,identifier,*,end='2026-10-12T12:00:00Z',start='2026-10-12T10:00:00Z',org=1,status='completed',file=None,model=None,printer=None):
    db.execute(text('INSERT INTO jobs(id,item_name,status,charged_to_org_id,actual_start,actual_end,duration_hours,print_file_id,model_id,printer_id) '
        'VALUES(:id,:name,:status,:org,:start,:end,99,:file,:model,:printer)'),{'id':identifier,'name':f'Terminal {identifier}','status':status,'org':org,'start':start,'end':end,'file':file,'model':model,'printer':printer})
    db.commit()


def test_dedicated_http_orders_newest_eight_ties_independent_of_queue_filters(review_db,recent_app):
    client,current=recent_app
    for identifier in range(100,110):job(review_db,identifier,end=f'2026-10-{identifier-90:02d}T12:00:00Z',status='failed' if identifier%2 else 'completed')
    job(review_db,200,status='pending',end='2030-01-01T00:00:00Z')
    response=client.get('/api/jobs/recent-terminal?status=pending&search=unrelated&org_id=2')
    assert response.status_code==200
    assert [item['id'] for item in response.json()]==list(range(109,101,-1))
    assert all(item['duration_seconds']!=99*3600 for item in response.json())
    review_db.execute(text("UPDATE jobs SET actual_end='2026-10-20T12:00:00Z' WHERE id IN (100,101)"));review_db.commit()
    assert [item['id'] for item in client.get('/api/jobs/recent-terminal').json()][:2]==[101,100]


def test_legacy_missing_invalid_end_is_last_unknown_and_zero_duration_is_real(review_db,recent_app):
    client,_=recent_app
    job(review_db,101,end=None);job(review_db,102,end='invalid');job(review_db,103,end='2026-10-12 12:00:00',start='2026-10-12 12:00:00')
    result=client.get('/api/jobs/recent-terminal').json();assert [item['id'] for item in result]==[103,102,101]
    assert result[0]['actual_end']=='2026-10-12T12:00:00Z' and result[0]['duration_seconds']==0
    assert all(item['actual_end'] is None and item['duration_seconds'] is None for item in result[1:])


def test_tenant_global_unassigned_and_requested_superadmin_scope(review_db,recent_app):
    client,current=recent_app
    job(review_db,100,org=1);job(review_db,101,org=2);job(review_db,102,org=None)
    assert {item['id'] for item in client.get('/api/jobs/recent-terminal?org_id=2').json()}=={100,102}
    current['user'].update(id=4,role='viewer',group_id=None)
    assert {item['id'] for item in client.get('/api/jobs/recent-terminal').json()}=={102}
    current['user'].update(id=3,role='admin')
    assert {item['id'] for item in client.get('/api/jobs/recent-terminal?org_id=2').json()}=={101,102}


def test_exact_file_thumbnail_and_related_names_require_independent_token_scopes(review_db,recent_app):
    client,current=recent_app
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,thumbnail_b64) VALUES(100,'safe','safe','/opaque/safe',1,:image)"),{'image':picture()})
    review_db.execute(text("INSERT INTO printers(id,name,org_id,shared) VALUES(100,'Safe printer',1,0)"));review_db.commit()
    job(review_db,100,file=100,printer=100)
    result=client.get('/api/jobs/recent-terminal').json()[0]
    assert result['thumbnail_data'].startswith('data:image/png;base64,') and result['printer_name']=='Safe printer'
    current['user'].update(_auth_kind='user_api_token',_token_scopes=['read:jobs'])
    result=client.get('/api/jobs/recent-terminal').json()[0]
    assert result['thumbnail_data'] is None and result['printer_name'] is None
    current['user']['_token_scopes']=['read:models']
    assert client.get('/api/jobs/recent-terminal').status_code==403
    current['user']['_token_scopes']=[]
    assert client.get('/api/jobs/recent-terminal').status_code==403


@pytest.mark.parametrize('thumbnail',['https://outside.example/image.jpg','<svg/>','not-base64',base64.b64encode(b'not an image').decode(),'a'*400000])
def test_invalid_or_external_thumbnail_is_never_returned(review_db,recent_app,thumbnail):
    client,_=recent_app
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,thumbnail_b64) VALUES(100,'safe','safe','/opaque/safe',1,:image)"),{'image':thumbnail});review_db.commit()
    job(review_db,100,file=100)
    assert client.get('/api/jobs/recent-terminal').json()[0]['thumbnail_data'] is None


def education(db):
    db.execute(text("UPDATE jobs SET status='completed',print_file_id=11,actual_start='2026-10-12T10:00:00Z',actual_end='2026-10-12T12:00:00Z' WHERE id=13"))
    db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
    db.execute(text('UPDATE print_files SET thumbnail_b64=:image WHERE id=11'),{'image':picture()});db.commit()


@pytest.mark.parametrize('uid,role,visible',[(1,'viewer',True),(2,'viewer',True),(3,'admin',True),(4,'viewer',False),(5,'admin',False)])
def test_education_audience_filters_entire_projection_before_limit(review_db,recent_app,uid,role,visible):
    client,current=recent_app;education(review_db)
    current['user'].update(id=uid,role=role,group_id=2 if uid==5 else 1)
    result=client.get('/api/jobs/recent-terminal').json()
    assert any(item['id']==13 for item in result)==visible
    if visible:assert next(item for item in result if item['id']==13)['thumbnail_data'].startswith('data:image/png;')
    else:assert all('file.3mf' not in str(item) for item in result)


def test_persisted_education_path_alias_never_becomes_generic_job_metadata(review_db,recent_app,monkeypatch):
    client,current=recent_app;education(review_db)
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,thumbnail_b64) VALUES(100,'alias','alias','/opaque/upload-1.3mf',1,:image)"),{'image':picture()});review_db.commit()
    job(review_db,100,file=100)
    current['user'].update(id=4,role='viewer')
    assert client.get('/api/jobs/recent-terminal').json()==[]
    # Remove operation path evidence; raw persisted submission/file alias remains protective.
    review_db.execute(text("UPDATE education_upload_operations SET final_path=NULL WHERE operation_id='upload-1'"));review_db.commit()
    assert client.get('/api/jobs/recent-terminal').json()==[]
    from core.registry import registry
    monkeypatch.setattr(registry,'get_provider',lambda name:None)
    assert client.get('/api/jobs/recent-terminal').json()==[]


def test_unauthorized_exact_file_does_not_fall_back_to_authorized_model(review_db,recent_app):
    client,_=recent_app
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,thumbnail_b64) VALUES(100,'foreign','foreign','/opaque/foreign',2,:image),(101,'safe','safe','/opaque/safe',1,:image)"),{'image':picture()})
    review_db.execute(text("INSERT INTO models(id,name,org_id,print_file_id,default_filament_type,thumbnail_b64) VALUES(100,'Safe model',1,101,'PLA',:image)"),{'image':picture()});review_db.commit()
    job(review_db,100,file=100,model=100)
    result=client.get('/api/jobs/recent-terminal').json()[0]
    assert result['thumbnail_data'] is None


def test_owned_and_foreign_education_same_path_never_mask_each_other(review_db,recent_app):
    client,current=recent_app;education(review_db)
    current['user'].update(id=1,role='viewer')
    assert [item['id'] for item in client.get('/api/jobs/recent-terminal').json()]==[13]
    review_db.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) VALUES(100,2,'foreign','foreign','Foreign','FOREIGN','active',1,5)"))
    review_db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,expected_hash,reservation_released_at,final_path) VALUES('foreign-upload',2,5,'committed',100,100,100,'digest',CURRENT_TIMESTAMP,'/opaque/upload-1.3mf')"))
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,created_by,model_id) VALUES(100,'foreign','foreign','/opaque/upload-1.3mf',2,5,100)"))
    review_db.execute(text("INSERT INTO models(id,name,print_file_id,org_id) VALUES(100,'Foreign educational model',100,2)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,model_id,print_file_id,submitted_by,charged_to_user_id,charged_to_org_id) VALUES(100,'Foreign educational name','completed',100,100,5,5,2)"))
    review_db.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision) VALUES(100,2,'foreign-upload',100,100,100,100,5,'submitted',1)"));review_db.commit()
    assert client.get('/api/jobs/recent-terminal').json()==[]
    # Own canonical proof is valid again, but a drifted persisted foreign alias
    # still protects the whole projection, not only the image.
    review_db.execute(text("UPDATE education_upload_operations SET final_path=NULL WHERE operation_id='foreign-upload'"));review_db.commit()
    assert client.get('/api/jobs/recent-terminal').json()==[]


@pytest.mark.parametrize('change',[
    "UPDATE models SET print_file_id=NULL WHERE id=12",
    "UPDATE print_files SET stored_path='/opaque/drifted' WHERE id=11",
    "UPDATE jobs SET print_file_id=NULL WHERE id=13",
])
def test_drifted_education_graph_omits_whole_terminal_card(review_db,recent_app,change):
    client,current=recent_app;education(review_db);current['user'].update(id=1,role='viewer')
    assert [item['id'] for item in client.get('/api/jobs/recent-terminal').json()]==[13]
    review_db.execute(text(change));review_db.commit()
    assert client.get('/api/jobs/recent-terminal').json()==[]


def test_authorization_precedes_newest_eight_limit(review_db,recent_app):
    client,current=recent_app;education(review_db);current['user'].update(id=4,role='viewer')
    for identifier in range(100,108):job(review_db,identifier,end=f'2026-10-{identifier-95:02d}T00:00:00Z')
    result=client.get('/api/jobs/recent-terminal').json()
    assert len(result)==8 and {item['id'] for item in result}==set(range(100,108))


def test_offset_ordering_and_malformed_date_never_outrank_valid_completion(review_db,recent_app):
    client,_=recent_app
    job(review_db,101,end='2026-10-12T12:00:00+02:00',start='2026-10-12T10:00:00+02:00')
    job(review_db,102,end='2026-10-12T11:00:00Z',start='2026-10-12T11:00:00Z')
    job(review_db,103,end='2026-10-13')
    result=client.get('/api/jobs/recent-terminal').json()
    assert [item['id'] for item in result]==[102,101,103]
    assert result[1]['actual_end']=='2026-10-12T10:00:00Z' and result[1]['duration_seconds']==7200
    assert result[2]['actual_end'] is None


def test_jpeg_is_validated_and_foreign_related_printer_name_is_unavailable(review_db,recent_app):
    client,_=recent_app
    output=BytesIO();Image.new('RGB',(16,16),'blue').save(output,format='JPEG')
    image=base64.b64encode(output.getvalue()).decode()
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,thumbnail_b64) VALUES(100,'safe','safe','/opaque/safe',1,:image)"),{'image':image})
    review_db.execute(text("INSERT INTO printers(id,name,org_id,shared) VALUES(100,'Foreign private printer',2,0)"));review_db.commit()
    job(review_db,100,file=100,printer=100)
    result=client.get('/api/jobs/recent-terminal').json()[0]
    assert result['thumbnail_data'].startswith('data:image/jpeg;base64,') and result['printer_name'] is None


def test_oversized_image_dimensions_return_placeholder_data(review_db,recent_app):
    client,_=recent_app
    output=BytesIO();Image.new('RGB',(1025,1),'blue').save(output,format='PNG')
    image=base64.b64encode(output.getvalue()).decode()
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,thumbnail_b64) VALUES(100,'safe','safe','/opaque/safe',1,:image)"),{'image':image});review_db.commit()
    job(review_db,100,file=100)
    assert client.get('/api/jobs/recent-terminal').json()[0]['thumbnail_data'] is None
