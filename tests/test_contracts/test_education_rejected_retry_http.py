"""Actual signed-license/JWT HTTP new upload after rejection; no print commands."""
import io,zipfile,uuid
from functools import partial
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from tests.test_contracts.test_education_submission_upload import submission_db,_sliced_3mf,_ample_disk
from tests.test_contracts.test_license_user_limits import signed_license

@pytest.fixture
def retry_case(submission_db,signed_license,monkeypatch):
    from core.db import get_db
    from core.auth import create_access_token
    from core.errors import OdinError
    from modules.organizations import education_access,routes_education_submissions,routes_education_reviews
    db,root=submission_db
    manager,license=signed_license();assert license.valid and license.has_feature('education_workflows')
    monkeypatch.setattr(education_access,'get_license',manager.get_license)
    _ample_disk(monkeypatch)
    monkeypatch.setattr(routes_education_submissions,'_DurableMultipartParser',partial(routes_education_submissions._DurableMultipartParser,storage_root=root))
    app=FastAPI();app.include_router(routes_education_submissions.router);app.include_router(routes_education_reviews.router)
    @app.middleware('http')
    async def lost_response(request,call_next):
        response=await call_next(request)
        if request.headers.get('X-Owned-Lose-Response')=='true' and response.status_code in (200,201):
            raise ConnectionError('Synthetic response lost after actual server commit')
        return response
    @app.exception_handler(OdinError)
    async def error(request,error):return JSONResponse(status_code=error.status,content=error.to_envelope())
    def database():
        with Session(db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=database
    def auth(username):return {'Authorization':'Bearer '+create_access_token(data={'sub':username})}
    client=TestClient(app)
    def upload(token,username='student',payload=None,lose_response=False):
        headers=auth(username)
        if lose_response:headers['X-Owned-Lose-Response']='true'
        response=client.post('/education/submissions',headers=headers,data={'submission_token':token,'cost_center_id':'7'},files={'file':('revised.3mf',payload or _sliced_3mf(),'application/octet-stream')})
        return response
    return db,client,auth,upload

def test_rejected_history_remains_and_fresh_upload_requires_fresh_teacher_review(retry_case):
    db,client,auth,upload=retry_case
    response=upload(str(uuid.uuid4()));assert response.status_code==201;original=response.json()
    rejected=client.post(f"/education/submissions/{original['id']}/reject",headers=auth('manager'),json={'revision':1,'reason':'Revise supports','command_id':str(uuid.uuid4())})
    assert rejected.status_code==200 and rejected.json()['status']=='rejected'
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_sliced_3mf())) as before,zipfile.ZipFile(output,'w') as after:
        for item in before.infolist():after.writestr(item.filename,before.read(item.filename).replace(b'value="12.5"',b'value="11.5"'))
    token=str(uuid.uuid4());revised=output.getvalue()
    # Actual server commits, but the caller discards the response as transport loss.
    with pytest.raises(ConnectionError,match='after actual server commit'):
        upload(token,payload=revised,lose_response=True)
    retried=upload(token,payload=revised);assert retried.status_code in (200,201)
    new=retried.json();assert new['id']!=original['id'] and new['job_id']!=original['job_id'] and new['print_file_id']!=original['print_file_id']
    db.expire_all()
    old=db.execute(text('SELECT s.status,j.status,j.rejected_reason FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=:id'),{'id':original['id']}).one()
    assert old==('rejected','rejected','Revise supports')
    current=db.execute(text('SELECT s.status,s.submitted_by,j.status,j.printer_id,j.approved_by FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=:id'),{'id':new['id']}).one()
    assert current==('submitted',1,'submitted',None,None)
    assert db.execute(text('SELECT count(*) FROM education_submissions WHERE operation_id=:token'),{'token':token}).scalar_one()==1
    assert db.execute(text('SELECT count(*) FROM education_submissions')).scalar_one()==2

@pytest.mark.parametrize('refusal',['foreign','removed_grant'])
def test_existing_upload_authority_refuses_foreign_or_removed_entitlement(retry_case,refusal):
    db,client,auth,upload=retry_case
    if refusal=='foreign':
        db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(2,'Foreign synthetic',1)"))
        db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(4,'foreign','fixture','viewer',1,2)"));username='foreign'
    else:
        db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1 AND role='student'"));username='student'
    db.commit()
    response=upload(str(uuid.uuid4()),username=username);assert response.status_code in (403,404)
    assert db.execute(text('SELECT count(*) FROM education_submissions')).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM jobs')).scalar_one()==0
