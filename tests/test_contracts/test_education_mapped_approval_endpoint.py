"""Normal report/approval HTTP flow retains explicit mapping and payload pins."""
import hashlib,json,uuid,time
from types import SimpleNamespace
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.orm import Session
import pytest
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_education_submission_upload import _sliced_3mf,submission_db,_principal,_ample_disk


@pytest.fixture
def review_db(submission_db):
    db,_=submission_db
    db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,bed_x_mm,bed_y_mm,nozzle_diameter,org_id) VALUES(9,'Test P1S','Bambu Lab P1S','P1S',1,'bambu',256,256,0.4,1)"))
    db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,1,'PLA')"))
    db.execute(text("INSERT INTO education_cost_center_printers(org_id,cost_center_id,printer_id,state,granted_by) VALUES(1,7,9,'active',3)"))
    db.commit()
    return db


@pytest.fixture
def mapped_case(token_client,review_db,monkeypatch,tmp_path,request):
    import io,zipfile
    import core.runtime_identity as identity
    from modules.organizations import education_access,routes_education_reviews,education_ams_read_service
    from modules.printers.telemetry.bambu import ams_evidence_transactions as transactions,ams_database_clock,trusted_ams_acquisition
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_sliced_3mf())) as original,zipfile.ZipFile(output,'w') as target:
        for item in original.infolist():
            data=original.read(item.filename)
            if item.filename=='Metadata/slice_info.config':data=data.replace(b'<plate>',b'<plate><metadata key="index" value="1"/>')
            if item.filename=='Metadata/plate_1.json':
                plate=json.loads(data)
                filament_ids=getattr(request,'param',[0])
                if filament_ids is None:plate.pop('filament_ids',None)
                elif filament_ids=='null':plate['filament_ids']=None
                elif filament_ids=='non-object':plate=[0]
                else:plate['filament_ids']=filament_ids
                data=json.dumps(plate).encode()
            if item.filename=='Metadata/plate_1.gcode':data=b'; filament_type = PLA\nM620 M\nM620 S0A\nM621 S0A\nT0\n'
            target.writestr(item.filename,data)
    payload=output.getvalue();path=tmp_path/'slice.3mf';path.write_bytes(payload)
    digest=hashlib.sha256(payload).hexdigest()
    key=Fernet.generate_key();monkeypatch.setenv('ENCRYPTION_KEY',key.decode())
    from modules.organizations.education_submission_service import process_sliced_3mf_submission
    _ample_disk(monkeypatch)
    uploaded=process_sliced_3mf_submission(review_db,source=io.BytesIO(payload),original_filename='slice.3mf',
        operation_id=str(uuid.uuid4()),principal=_principal(),cost_center_id=7,storage_root=tmp_path/'uploads')
    path=__import__('pathlib').Path(review_db.execute(text('SELECT stored_path FROM print_files WHERE id=:id'),
        {'id':uploaded['print_file_id']}).scalar_one())
    review_db.rollback()
    review_db.execute(text('UPDATE printers SET api_host=:host,api_key=:key WHERE id=9'),{'host':'printer.local','key':Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
    review_db.commit()
    runtime=SimpleNamespace(incarnation='a'*32,clock_domain='synthetic-domain')
    monkeypatch.setattr(identity,'read_runtime_identity',lambda:runtime)
    monkeypatch.setattr(identity,'revalidate_runtime_identity',lambda value:None)
    monkeypatch.setattr(transactions,'revalidate_runtime_identity',lambda value:None)
    monkeypatch.setattr(ams_database_clock,'revalidate_runtime_identity',lambda value:None)
    monkeypatch.setattr(education_ams_read_service,'SessionLocal',lambda:Session(review_db.get_bind()))
    def read(config,**kwargs):
        report=RawAMSSnapshot(kwargs['operation_id'],uuid.uuid4().hex,1,time.monotonic(),kwargs['configuration_key'],kwargs['owner_org_id'],(RawTrayFact(1,0,0,'PLA','reported'),))
        return RuntimeBoundAMSResult(True,report,runtime.incarnation,runtime.clock_domain,True,None)
    monkeypatch.setattr(trusted_ams_acquisition,'read_current_selected_ams',read)
    client,issue=token_client;client.app.include_router(routes_education_reviews.router)
    headers=issue(['write:education'])
    response=client.post(f"/education/submissions/{uploaded['id']}/ams-read",headers=headers,json={'revision':1,'printer_id':9,'selected_slots':[1]})
    assert response.status_code==200,response.text
    body={'revision':1,'printer_id':9,'command_id':str(uuid.uuid4()),'ams_generation_id':response.json()['generation_id'],
          'plate_index':0,'ams_mapping':[{'source_slot':0,'target_slot':1}]}
    return client,headers,body,digest,path,uploaded


def test_explicit_teacher_ams_selection_survives_approval(mapped_case,review_db):
    client,headers,body,digest,_,uploaded=mapped_case
    response=client.post(f"/education/submissions/{uploaded['id']}/approve",headers=headers,json=body)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['ams_mapping']==[0] and result['print_file_id']==uploaded['print_file_id'] and result['payload_sha256']==digest and result['plate_index']==0
    row=review_db.execute(text('SELECT ams_mapping,print_file_id,payload_sha256,plate_index FROM jobs WHERE id=:id'),{'id':uploaded['job_id']}).mappings().one()
    assert json.loads(row['ams_mapping'])==[0] and row['payload_sha256']==digest
    assert client.post(f"/education/submissions/{uploaded['id']}/approve",headers=headers,json=body).json()==result
    changed={**body,'ams_mapping':[{'source_slot':0,'target_slot':2}]}
    assert client.post(f"/education/submissions/{uploaded['id']}/approve",headers=headers,json=changed).status_code==409


@pytest.mark.parametrize('mapped_case',[None],indirect=True,ids=['missing-source'])
def test_plate_without_filament_ids_relies_on_xml_and_commands(mapped_case,review_db):
    client,headers,body,_,_,uploaded=mapped_case
    response=client.post(f"/education/submissions/{uploaded['id']}/approve",headers=headers,json=body)
    assert response.status_code==200,response.text
    assert response.json()['ams_mapping']==[0]


@pytest.mark.parametrize('mapped_case',[[1],[True],'null','non-object'],indirect=True,
                         ids=['conflicting-source','invalid-source','explicit-null','non-object-plate'])
def test_unverified_plate_source_refuses_without_approval(mapped_case,review_db):
    client,headers,body,_,_,uploaded=mapped_case
    response=client.post(f"/education/submissions/{uploaded['id']}/approve",headers=headers,json=body)
    assert response.status_code==409,response.text
    row=review_db.execute(text('SELECT status,ams_mapping FROM jobs WHERE id=:id'),
                         {'id':uploaded['job_id']}).one()
    assert row.status=='submitted' and row.ams_mapping is None
    assert review_db.execute(text('SELECT lifecycle_revision FROM education_submissions WHERE id=:id'),
                             {'id':uploaded['id']}).scalar_one()==1


@pytest.mark.parametrize('change',['file_bytes','configuration','superseded'])
def test_changed_payload_target_or_report_refuses_without_approval(mapped_case,review_db,change):
    client,headers,body,_,path,uploaded=mapped_case
    if change=='file_bytes':path.write_bytes(path.read_bytes()+b'changed')
    elif change=='configuration':review_db.execute(text("UPDATE printers SET api_host='changed.local' WHERE id=9"));review_db.commit()
    else:
        assert client.post(f"/education/submissions/{uploaded['id']}/ams-read",headers=headers,json={'revision':1,'printer_id':9,'selected_slots':[1]}).status_code==200
    response=client.post(f"/education/submissions/{uploaded['id']}/approve",headers=headers,json=body)
    assert response.status_code==409
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':uploaded['job_id']}).scalar_one()=='submitted'
    assert review_db.execute(text('SELECT ams_mapping FROM jobs WHERE id=:id'),{'id':uploaded['job_id']}).scalar_one() is None


@pytest.mark.parametrize('rollback_failure',[False,True])
def test_committed_then_error_is_unconfirmed_and_replays_without_second_approval(mapped_case,review_db,monkeypatch,rollback_failure):
    client,headers,body,_,_,uploaded=mapped_case
    original=Session.commit;original_rollback=Session.rollback;injected=[];rollback_errors=[]
    def commit(self):
        mapped=self.execute(text("SELECT count(*) FROM jobs WHERE status='pending' AND ams_mapping IS NOT NULL")).scalar_one()
        original(self)
        if mapped and not injected:
            injected.append(True)
            raise RuntimeError('synthetic lost approval acknowledgment')
    def rollback(self):
        if rollback_failure and injected and not rollback_errors:
            rollback_errors.append(True)
            raise RuntimeError('synthetic rollback unavailable')
        return original_rollback(self)
    monkeypatch.setattr(Session,'commit',commit)
    monkeypatch.setattr(Session,'rollback',rollback)
    response=client.post(f"/education/submissions/{uploaded['id']}/approve",headers=headers,json=body)
    assert injected and response.status_code==503
    assert bool(rollback_errors) is rollback_failure
    assert 'unconfirmed' in response.text.lower()
    replay=client.post(f"/education/submissions/{uploaded['id']}/approve",headers=headers,json=body)
    assert replay.status_code==200 and replay.json()['ams_mapping']==[0]
    assert review_db.execute(text('SELECT lifecycle_revision FROM education_submissions WHERE id=:id'),{'id':uploaded['id']}).scalar_one()==2
