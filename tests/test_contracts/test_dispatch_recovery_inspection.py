"""Actual authenticated HTTP inspection of retained dispatch evidence; no I/O."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_printer_control_behavior import control_client,token_client,review_db

@pytest.fixture
def recovery_client(control_client,review_db):
    from modules.printers import routes
    client,issue,calls=control_client
    client.app.include_router(routes.router)
    review_db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,owner_org_id,state) VALUES(299,953,:physical,:config,:payload,:token,1,'uncertain')"),dict(physical='a'*64,config='b'*64,payload='c'*64,token='d'*32))
    review_db.commit()
    return client,issue,calls


def test_owned_inspection_preserves_fence_and_does_not_infer_completion(recovery_client,review_db):
    client,issue,calls=recovery_client
    response=client.get('/printers/299/dispatch-recovery',headers=issue(['read:printers']))
    assert response.status_code==200
    body=response.json()
    assert body['state']=='uncertain' and body['job_id'] is None
    assert body['receipt_present'] is False and body['observed_print'] is None
    assert body['release_available'] is False
    assert calls==[]
    assert review_db.execute(text('SELECT state FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one()=='uncertain'
    for secret in ['a'*64,'b'*64,'c'*64,'d'*32]:assert secret not in response.text


@pytest.mark.parametrize('change',[
    "UPDATE printers SET org_id=2 WHERE id=299",
    "UPDATE printers SET org_id=2,shared=1 WHERE id=299",
    "UPDATE printer_dispatch_claims SET owner_org_id=2 WHERE printer_id=299",
    "UPDATE printer_dispatch_claims SET owner_org_id=NULL WHERE printer_id=299",
    "DELETE FROM printers WHERE id=299",
])
def test_foreign_or_unattributed_evidence_is_hidden(recovery_client,review_db,change):
    client,issue,calls=recovery_client
    review_db.execute(text(change));review_db.commit()
    assert client.get('/printers/299/dispatch-recovery',headers=issue(['read:printers'])).status_code==404
    assert calls==[]


def test_absent_claim_is_explicit_not_completed(recovery_client,review_db):
    client,issue,calls=recovery_client
    review_db.execute(text('DELETE FROM printer_dispatch_claims WHERE printer_id=299'));review_db.commit()
    response=client.get('/printers/299/dispatch-recovery',headers=issue(['read:printers']))
    assert response.status_code==200
    assert response.json()['state']=='no_retained_claim' and not response.json()['release_available']
    assert not any(response.json()['actions'].values()) and 'attempt_handle' not in response.json()
    assert calls==[]


def test_unscoped_token_cannot_inspect(recovery_client):
    client,issue,calls=recovery_client
    assert client.get('/printers/299/dispatch-recovery',headers=issue(['write:printers'])).status_code==403
    assert calls==[]

@pytest.mark.parametrize('job_org',[1,2])
def test_owned_receipt_and_job_metadata_are_scoped(recovery_client,review_db,job_org):
    client,issue,calls=recovery_client
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,hold) VALUES(953,'Synthetic private display',299,:org,'printing',1)"),{'org':job_org})
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,filename) VALUES(954,299,953,'completed',CURRENT_TIMESTAMP,'Synthetic private filename')"))
    review_db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,observed_print_job_id) VALUES(:token,299,953,:config,:power,:filename,954)"),{'token':'d'*32,'config':'b'*64,'power':'e'*64,'filename':'f'*64})
    review_db.commit()
    response=client.get('/printers/299/dispatch-recovery',headers=issue(['read:printers']))
    assert response.status_code==200
    data=response.json()
    assert data['receipt_present'] is True
    assert data['observed_print']==({'id':954,'status':'completed'} if job_org==1 else None)
    assert data['job_id']==(953 if job_org==1 else None)
    assert data['job']==({'status':'printing','held':True} if job_org==1 else None)
    assert data['state']=='uncertain' and data['release_available'] is False
    for value in ['Synthetic private display','Synthetic private filename','d'*32,'e'*64,'f'*64]:assert value not in response.text
    assert calls==[]


def test_receipt_with_wrong_observation_identity_does_not_report_completion(recovery_client,review_db):
    client,issue,calls=recovery_client
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(954,299,'completed',CURRENT_TIMESTAMP)"))
    review_db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,observed_print_job_id) VALUES(:token,299,953,:config,:power,:filename,954)"),{'token':'d'*32,'config':'b'*64,'power':'e'*64,'filename':'f'*64})
    review_db.commit()
    data=client.get('/printers/299/dispatch-recovery',headers=issue(['read:printers'])).json()
    assert data['receipt_present'] is True and data['observed_print'] is None and data['release_available'] is False
    assert calls==[]
