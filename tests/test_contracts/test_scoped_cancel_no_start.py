"""A reconciled no-start receipt grants no printer authority."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('evidence',['reconciled','unreconciled','older_audit','observed','later_receipt','wrong_receipt'])
def test_scoped_cancel_requires_no_start_for_every_receipt(review_db,evidence):
    from modules.jobs.scoped_cancel import assert_cancel_allowed
    from core.errors import OdinError
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    registry.register_provider("EducationPolicyProvider", EducationPolicyService())
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,hold) VALUES(8070,'Never started','pending',1)"))
    review_db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,created_at,observed_print_job_id) VALUES(:token,9,8070,:key,:key,:key,'2026-10-25',:observed)"),{'token':'a'*32,'key':'b'*64,'observed':123 if evidence=='observed' else None})
    if evidence!='unreconciled':
        review_db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,timestamp,details) VALUES('operator.dispatch_no_start_reconciled','job',8070,:stamp,:details)"),{'stamp':'2026-10-24' if evidence=='older_audit' else '2026-10-26','details':json.dumps({'job_left_held':True,'gate_refusal':'ams_read_failed','claim_token':'c'*32 if evidence=='wrong_receipt' else 'a'*32})})
    if evidence=='later_receipt':
        review_db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,created_at) VALUES(:token,9,8070,:key,:key,:key,'2026-10-27')"),{'token':'c'*32,'key':'b'*64})
    job=SimpleNamespace(id=8070,status='pending',hold=True)
    user={'_token_scopes':['write:jobs'],'_auth_method':'api_token','role':'operator'}
    # Match the token marker used by the production RBAC helper.
    user['_is_api_token']=True
    if evidence=='reconciled':assert_cancel_allowed(review_db,job,user)
    else:
        with pytest.raises(OdinError):assert_cancel_allowed(review_db,job,user)


from tests.test_contracts.test_production_ops import cancel_client, batch, shop


def test_reconciled_receipt_can_cancel_through_scoped_http_route(cancel_client):
    client,user,_,db,*_=cancel_client
    db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,created_at) VALUES(:token,301,401,:key,:key,:key,'2026-10-25')"),{'token':'a'*32,'key':'b'*64})
    db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,timestamp,details) VALUES('operator.dispatch_no_start_reconciled','job',401,'2026-10-26',:details)"),{'details':json.dumps({'job_left_held':True,'gate_refusal':'ams_read_failed'})})
    db.commit()
    response=client.post('/jobs/401/cancel')
    assert response.status_code==200,response.text
    assert db.execute(text('SELECT status FROM jobs WHERE id=401')).scalar_one()=='cancelled'
