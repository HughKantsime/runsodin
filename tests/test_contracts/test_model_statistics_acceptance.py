"""Real route/auth projections use completed generic job rows and measured timing."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db, interactive_headers

@pytest.fixture
def stats_client(token_client, review_db, monkeypatch):
    from modules.models_library.routes import models_crud
    from core.registry import registry
    client, issue = token_client
    client.app.include_router(models_crud.router)
    # The persisted privacy predicate must work even if the provider disappears.
    monkeypatch.delitem(registry._providers, 'EducationPolicyProvider', raising=False)
    review_db.execute(text("INSERT INTO models(id,name,org_id,build_time_hours) VALUES(30,'Shared',NULL,90),(31,'Tenant one',1,90),(32,'Tenant two',2,90)"))
    review_db.commit()
    return client, issue


def job(db, job_id, *, model=30, org=1, status='completed', start='2026-10-12 10:00:00', end='2026-10-12 12:00:00', quantity=1, file=None):
    db.execute(text('INSERT INTO jobs(id,model_id,item_name,status,quantity,charged_to_org_id,actual_start,actual_end,duration_hours,print_file_id) VALUES(:id,:model,\'Synthetic\',:status,:quantity,:org,:start,:end,90,:file)'), dict(id=job_id,model=model,status=status,quantity=quantity,org=org,start=start,end=end,file=file))
    db.commit()


def stats(client, headers=None, query=''):
    response=client.get('/models-with-pricing'+query,headers=headers or interactive_headers())
    assert response.status_code==200
    return {row['id']:row['job_statistics'] for row in response.json()}


def test_rows_status_and_actual_time_without_estimate_fallback(stats_client,review_db):
    client,_=stats_client
    job(review_db,40,quantity=7)
    job(review_db,41,start=None,end=None)
    job(review_db,42,end='2026-10-12 09:00:00')
    job(review_db,43,end='2026-10-12 10:00:00')
    for offset,status in enumerate(['pending','scheduled','printing','failed','cancelled','submitted','rejected']):
        job(review_db,50+offset,status=status)
    assert stats(client)[30]=={'completed_job_count':4,'actual_print_hours':2.0,'unknown_timing_job_count':3}
    assert stats(client)[31]=={'completed_job_count':0,'actual_print_hours':0.0,'unknown_timing_job_count':0}


def test_shared_model_tenant_unassigned_and_explicit_superadmin_org(stats_client,review_db):
    from core.dependencies import get_current_user
    client,_=stats_client
    job(review_db,40,org=1);job(review_db,41,org=2);job(review_db,42,org=None)
    job(review_db,43,org=2,model=32)
    assert stats(client)[30]['completed_job_count']==1
    assert 32 not in stats(client)
    client.app.dependency_overrides[get_current_user]=lambda:{'id':5,'role':'viewer','group_id':2}
    assert stats(client)[30]['completed_job_count']==1
    assert stats(client)[32]['completed_job_count']==1
    client.app.dependency_overrides[get_current_user]=lambda:{'id':6,'role':'viewer','group_id':None}
    assert stats(client)[30]['completed_job_count']==1
    assert set(stats(client))=={30}
    client.app.dependency_overrides[get_current_user]=lambda:{'id':6,'role':'admin','group_id':None}
    assert stats(client)[30]['completed_job_count']==3
    assert stats(client,query='?org_id=2')[30]['completed_job_count']==1
    assert 31 not in stats(client,query='?org_id=2')


@pytest.mark.parametrize('scopes,allowed',[(['read:models'],False),(['read:models','write:jobs'],False),(['read:models','read:jobs'],True),(['read'],True),(['admin'],True)])
def test_actual_scoped_token_auth_separates_jobs_from_models(stats_client,review_db,scopes,allowed):
    client,issue=stats_client;job(review_db,40)
    result=stats(client,issue(scopes))[30]
    assert result==({'completed_job_count':1,'actual_print_hours':2.0,'unknown_timing_job_count':0} if allowed else None)


@pytest.mark.parametrize('link',['job','model','job_file','model_file','variant_file','file_job','submission_path','final_path','staging_path','cleanup_path'])
def test_persisted_education_graph_excluded_without_provider(stats_client,review_db,link):
    client,_=stats_client
    # Existing fixture's Education submission has job 13/model12/file11.
    model=30;file=None
    if link=='job':
        review_db.execute(text("UPDATE jobs SET model_id=30,status='completed',actual_start='2026-10-12 10:00:00',actual_end='2026-10-12 12:00:00' WHERE id=13"))
    elif link=='model':
        review_db.execute(text('UPDATE models SET org_id=1 WHERE id=30'))
        review_db.execute(text('UPDATE education_submissions SET model_id=30 WHERE id=14'))
    elif link=='job_file': file=11
    elif link=='model_file': review_db.execute(text('UPDATE models SET print_file_id=11 WHERE id=30'))
    elif link=='variant_file': review_db.execute(text('UPDATE print_files SET model_id=30 WHERE id=11'))
    elif link=='file_job': review_db.execute(text('UPDATE print_files SET job_id=40 WHERE id=11'))
    else:
        path='/opaque/upload-1.3mf' if link=='submission_path' else '/synthetic/private.3mf'
        review_db.execute(text("INSERT INTO print_files(id,filename,stored_path,org_id) VALUES(50,'alias.3mf',:path,1)"),{'path':path})
        file=50
        if link!='submission_path':
            column={'final_path':'final_path','staging_path':'staging_path','cleanup_path':'cleanup_path'}[link]
            review_db.execute(text(f'UPDATE education_upload_operations SET {column}=:path WHERE operation_id=\'upload-1\''),{'path':path})
    review_db.commit()
    if link!='job':job(review_db,40,file=file)
    assert stats(client)[30]=={'completed_job_count':0,'actual_print_hours':0.0,'unknown_timing_job_count':0}


def test_category_selection_keeps_only_authorized_selected_models(stats_client,review_db):
    client,_=stats_client
    review_db.execute(text("UPDATE models SET category='Chosen' WHERE id=31"))
    review_db.commit()
    job(review_db,40);job(review_db,41,model=31)
    result=stats(client,query='?category=Chosen')
    assert set(result)=={31}
    assert result[31]['completed_job_count']==1


def test_jobs_scope_alone_does_not_authorize_model_library(stats_client):
    client,issue=stats_client
    assert client.get('/models-with-pricing',headers=issue(['read:jobs'])).status_code==403


@pytest.mark.parametrize('endpoint', ['/models', '/models-with-pricing'])
@pytest.mark.parametrize('principal,expected', [
    ({'id': 3, 'role': 'admin', 'group_id': 1}, {12, 31}),
    ({'id': 5, 'role': 'viewer', 'group_id': 2}, {32}),
    ({'id': 6, 'role': 'viewer', 'group_id': None}, set()),
    ({'id': 6, 'role': 'admin', 'group_id': None}, {12, 31, 32}),
])
def test_hide_filter_preserves_tenant_and_category_for_both_or_branches(stats_client, review_db, endpoint, principal, expected):
    from core.dependencies import get_current_user
    client, _ = stats_client
    # Hidden live Education history stays visible only to its authorized tenant.
    # Hidden ordinary models disappear; unhidden models still obey tenant scope.
    review_db.execute(text('UPDATE models SET hidden_from_library=TRUE WHERE id IN (12,30)'))
    review_db.execute(text("UPDATE models SET category='Chosen' WHERE id=31"))
    review_db.commit()
    client.app.dependency_overrides[get_current_user] = lambda: principal
    response = client.get(endpoint, headers=interactive_headers())
    assert response.status_code == 200
    assert {row['id'] for row in response.json()} == expected
    response = client.get(endpoint + '?category=Chosen', headers=interactive_headers())
    assert response.status_code == 200
    assert {row['id'] for row in response.json()} == expected & {31}
