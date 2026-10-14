"""Queue model options against owned, physical local sliced fixtures, never hardware."""
import hashlib
import zipfile
import pytest
from sqlalchemy import text
from tests.test_contracts.test_product_bom_integrity import bom_client, review_db
from tests.test_contracts.test_model_variant_audience_behavior import variants_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client

@pytest.fixture
def schedule_client(bom_client, monkeypatch):
    from modules.models_library.routes import models_crud
    from modules.organizations.services import EducationPolicyService
    from core.registry import registry
    from modules.jobs import register_providers
    monkeypatch.setattr(registry, '_providers', dict(registry._providers))
    register_providers(registry)
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    bom_client.app.include_router(models_crud.router)
    return bom_client

@pytest.fixture
def payload(review_db,tmp_path):
    path=tmp_path/'owned.3mf'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('Metadata/plate_1.gcode','G1 X1')
        archive.writestr('Metadata/plate_2.gcode','G1 X2')
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,model_id,org_id,plate_count,compatible_api_types) VALUES(201,'owned.3mf','owned.3mf',:path,101,1,1,'bambu')"),{'path':str(path)})
    review_db.commit()
    return path

def row(db):
    return db.execute(text('SELECT * FROM jobs ORDER BY id DESC LIMIT 1')).mappings().one()

def test_absent_body_legacy_defaults(schedule_client,review_db):
    response=schedule_client.post('/models/101/schedule')
    assert response.status_code==200,response.text
    job=row(review_db)
    assert (job['quantity'],job['priority'],job['plate_index'])==(1,5,None)


def test_default_file_bound_job_passes_real_dispatch_options(schedule_client,review_db,payload):
    from modules.printers.dispatch import _validated_print_options
    response=schedule_client.post('/models/101/schedule?printer_id=9')
    assert response.status_code==200,response.text
    job=dict(row(review_db))
    assert (job['print_file_id'],job['plate_index'])==(201,0)
    options,error=_validated_print_options(job,{'api_type':'bambu'},str(payload),hashlib.sha256(payload.read_bytes()).hexdigest())
    assert error is None
    assert options['plate_num']==1


@pytest.mark.parametrize('fail_insert',[False,True])
def test_limited_actor_charges_once_or_rolls_back_with_job(schedule_client,review_db,payload,monkeypatch,fail_insert):
    from core.dependencies import get_current_user
    from modules.models_library.routes import models_crud
    schedule_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'operator','group_id':1,'is_active':True,'quota_jobs':1}
    if fail_insert:
        from modules.jobs import queue_creation
        observed = []
        def refuse(db, job, actor):
            # Fault the canonical insertion path after quota has been staged.
            assert db.execute(text('SELECT jobs_used FROM quota_usage WHERE user_id=3')).scalar()==1
            observed.append(job)
            raise RuntimeError('Owned insert failure')
        monkeypatch.setattr(queue_creation,'_stage_prospective_queue_job',refuse)
        with pytest.raises(RuntimeError,match='Owned insert failure'):
            schedule_client.post('/models/101/schedule')
        assert len(observed)==1
        assert review_db.execute(text('SELECT count(*) FROM quota_usage')).scalar()==0
        assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar()==1
    else:
        assert schedule_client.post('/models/101/schedule',json={'quantity':7}).status_code==200
        assert review_db.execute(text('SELECT jobs_used FROM quota_usage WHERE user_id=3')).scalar()==1
        from core.errors import OdinError
        with pytest.raises(OdinError) as exhausted:
            schedule_client.post('/models/101/schedule')
        assert exhausted.value.status==429
        assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar()==2
        assert review_db.execute(text('SELECT jobs_used FROM quota_usage WHERE user_id=3')).scalar()==1


def test_all_options_snapshot_binding_and_actual_cached_false_count(schedule_client,review_db,payload):
    options=schedule_client.get('/models/101/schedule-options').json()
    assert options['plate_indices']==[0,1]
    response=schedule_client.post('/models/101/schedule?printer_id=9',json={**{k:v for k,v in options.items() if k!='plate_indices'},'quantity':7,'priority':2,'due_date':'2026-10-13T02:00:00-04:00','notes':'Owned scheduling','plate_index':1})
    assert response.status_code==200,response.text
    job=row(review_db)
    assert (job['quantity'],job['priority'],job['notes'],job['print_file_id'],job['plate_index'])==(7,2,'Owned scheduling',201,1)
    assert job['payload_sha256']==hashlib.sha256(payload.read_bytes()).hexdigest()
    assert '06:00:00' in str(job['due_date'])
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar()==2

@pytest.mark.parametrize('body',[{'quantity':0},{'quantity':1.5},{'quantity':True},{'priority':6},{'plate_index':-1},{'plate_index':True}])
def test_invalid_options_no_job(schedule_client,review_db,body):
    assert schedule_client.post('/models/101/schedule',json=body).status_code==422
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar()==1

@pytest.mark.parametrize('change',['bytes','pointer'])
def test_options_pin_rejects_stale_payload_before_quota(schedule_client,review_db,payload,change):
    options=schedule_client.get('/models/101/schedule-options').json();options.pop('plate_indices');options['plate_index']=1
    if change=='bytes':payload.write_bytes(b'changed')
    else:
        review_db.execute(text("INSERT INTO print_files(id,filename,stored_path,model_id,org_id) VALUES(202,'replacement.3mf',:path,101,1)"),{'path':str(payload)});review_db.commit()
    assert schedule_client.post('/models/101/schedule',json=options).status_code==409
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar()==1
    assert review_db.execute(text('SELECT count(*) FROM quota_usage')).scalar()==0

@pytest.mark.parametrize('kind',['missing','duplicate','empty','nonbambu','foreign_file','foreign_model','foreign_printer','missing_policy','denied_policy','unreadable','bed','protocol','null_model'])
def test_preflight_refusal_never_admits_job_or_quota(schedule_client,review_db,payload,monkeypatch,kind):
    printer=9;model=101
    if kind in ('missing','duplicate','empty'):
        with zipfile.ZipFile(payload,'w') as archive:
            archive.writestr('Metadata/plate_1.gcode','G1 X1')
            if kind!='missing': archive.writestr('Metadata/plate_2.gcode','' if kind=='empty' else 'G1 X2')
            if kind=='duplicate': archive.writestr('Metadata/plate_2.gcode','G1 X3')
    if kind=='nonbambu':review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=9"))
    if kind=='foreign_file':review_db.execute(text('UPDATE print_files SET org_id=2 WHERE id=201'))
    if kind=='null_model':review_db.execute(text('UPDATE models SET print_file_id=201 WHERE id=101'));review_db.execute(text('UPDATE print_files SET model_id=NULL WHERE id=201'))
    if kind=='foreign_model':model=102
    if kind=='foreign_printer':
        printer=99
        review_db.execute(text("INSERT INTO printers(id,name,org_id,api_type,is_active) VALUES(99,'Synthetic foreign target',2,'bambu',1)"))
    if kind in ('missing_policy','denied_policy'):
        from core.registry import registry
        class Denied:
            def assert_generic_model_reference_allowed(self,*args):
                from fastapi import HTTPException
                raise HTTPException(409,'Synthetic denial')
        monkeypatch.setitem(registry._providers,'EducationPolicyProvider',None if kind=='missing_policy' else Denied())
    if kind=='unreadable':payload.unlink()
    if kind=='bed':review_db.execute(text('UPDATE print_files SET bed_x_mm=300,bed_y_mm=300 WHERE id=201'))
    if kind=='protocol':review_db.execute(text("UPDATE printers SET api_type='elegoo' WHERE id=9"))
    review_db.commit()
    response=schedule_client.post(f'/models/{model}/schedule?printer_id={printer}',json={'plate_index':1})
    assert response.status_code in (400,404,409,503),response.text
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar()==1
    assert review_db.execute(text('SELECT count(*) FROM quota_usage')).scalar()==0


def test_shared_printer_can_queue_owned_model(schedule_client,review_db,payload):
    review_db.execute(text("INSERT INTO printers(id,name,org_id,api_type,is_active,shared) VALUES(99,'Synthetic shared target',2,'bambu',1,1)"));review_db.commit()
    assert schedule_client.post('/models/101/schedule?printer_id=99',json={'plate_index':1}).status_code==200

@pytest.mark.parametrize('model_id',[None,103])
def test_primary_pointer_cannot_cross_model_association(schedule_client,review_db,payload,model_id):
    review_db.execute(text('UPDATE models SET print_file_id=201 WHERE id=101'))
    review_db.execute(text('UPDATE print_files SET model_id=:id WHERE id=201'),{'id':model_id});review_db.commit()
    assert schedule_client.post('/models/101/schedule',json={'plate_index':1}).status_code==404
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar()==1


def test_file_change_between_hash_and_snapshot_refused(schedule_client,review_db,payload,monkeypatch):
    from core import print_payload_snapshot as dispatch_payload_snapshot
    original=dispatch_payload_snapshot.hash_regular_payload
    observed=[]
    def changed(path):
        observed.append(path)
        digest=original(path)
        payload.write_bytes(b'Synthetic changed source')
        return digest
    monkeypatch.setattr(dispatch_payload_snapshot,'hash_regular_payload',changed)
    assert schedule_client.post('/models/101/schedule',json={'plate_index':1}).status_code==400
    assert len(observed)==1
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar()==1
    assert review_db.execute(text('SELECT count(*) FROM quota_usage')).scalar()==0



def test_options_metadata_requires_write_jobs_scope(variants_client):
    client,issue=variants_client
    assert client.get('/models/101/schedule-options',headers=issue(['read:models'])).status_code==403
