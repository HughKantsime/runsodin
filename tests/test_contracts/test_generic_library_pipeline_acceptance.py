"""Generic upload-to-queue via real HTTP, auth, parser and persisted payload."""
import hashlib
from pathlib import Path
import pytest
from sqlalchemy import text
from tests.test_contracts.test_print_file_upload_ownership_behavior import upload_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


@pytest.mark.parametrize('extension',['.3mf','.gcode'])
def test_uploaded_file_is_visible_model_and_dispatch_validated_queued_job(upload_client,review_db,monkeypatch,extension):
    from modules.models_library.routes import models_crud
    from modules.jobs.routes import jobs_crud
    from modules.organizations.services import EducationPolicyService
    from core.registry import registry
    from modules.printers.dispatch import _validated_print_options
    client,issue,storage=upload_client
    from fastapi.testclient import TestClient
    client=TestClient(client.app)
    client.app.include_router(models_crud.router)
    client.app.include_router(jobs_crud.router)
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    review_db.execute(text("INSERT INTO models(id,name,org_id,default_filament_type) VALUES(9901,'Owned unknown material',1,NULL)"))
    review_db.commit()
    headers=issue(['write:models','read:models','write:jobs','read:jobs'])
    payload=Path('tests/fixtures/telemetry/a1-petg-source-sanitized.3mf').read_bytes() if extension=='.3mf' else b'; printer_model = MK4\nG28\n'
    response=client.post('/print-files/upload',headers=headers,files={'file':('Owned pipeline'+extension,payload,'application/octet-stream')})
    assert response.status_code==200,response.text
    uploaded=response.json()
    library=client.get('/models',headers=headers)
    assert library.status_code==200,library.text
    assert uploaded['model_id'] in {model['id'] for model in library.json()}
    unknown=next(model for model in library.json() if model['id']==9901)
    assert unknown['default_filament_type'] is None
    detail=client.get('/models/9901',headers=headers)
    assert detail.status_code==200,detail.text
    assert detail.json()['default_filament_type'] is None
    options=client.get(f"/models/{uploaded['model_id']}/schedule-options",headers=headers)
    assert options.status_code==200,options.text
    pin=options.json();pin.pop('plate_indices')
    assert pin['print_file_id']==uploaded['id']
    assert pin['payload_sha256']==hashlib.sha256(payload).hexdigest()
    queued=client.post(f"/models/{uploaded['model_id']}/schedule",headers=headers,json={**pin,'quantity':2,'notes':'Owned upload pipeline'})
    assert queued.status_code==200,queued.text
    job_id=queued.json()['job_id']
    visible=client.get('/jobs',headers=headers)
    assert visible.status_code==200,visible.text
    assert job_id in {job['id'] for job in visible.json()}
    row=dict(review_db.execute(text('SELECT * FROM jobs WHERE id=:id'),{'id':job_id}).mappings().one())
    stored=review_db.execute(text('SELECT stored_path FROM print_files WHERE id=:id'),{'id':uploaded['id']}).scalar_one()
    assert Path(stored).is_relative_to(storage)
    assert Path(stored).read_bytes()==payload
    assert (row['model_id'],row['print_file_id'],row['quantity'],row['plate_index'])==(uploaded['model_id'],uploaded['id'],2,0)
    dispatch_options,error=_validated_print_options(row,{'api_type':'bambu' if extension=='.3mf' else 'prusalink'},stored,hashlib.sha256(payload).hexdigest())
    assert error is None
    assert dispatch_options is not None


def test_creation_material_default_and_validation_are_preserved(upload_client):
    from modules.models_library.routes import models_crud
    client,issue,_=upload_client
    client.app.include_router(models_crud.router)
    headers=issue(['write:models'])
    invalid=client.post('/models',headers=headers,json={'name':'Invalid null material','default_filament_type':None})
    assert invalid.status_code==422
    created=client.post('/models',headers=headers,json={'name':'Default material remains explicit'})
    assert created.status_code==201,created.text
    assert created.json()['default_filament_type']=='PLA'
