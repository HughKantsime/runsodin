"""Real token HTTP qualification with retained dispatch evidence, no external I/O."""
import json
import pytest
from sqlalchemy import text, select, event
from sqlalchemy.orm import Session
from tests.test_contracts.test_production_operator_token import authenticate
from tests.test_contracts.test_batch_acceptance import batch, make_recipe
from tests.test_contracts.test_square_readonly_workflow import shop
from tests.test_contracts.test_education_review_workflow import review_db


def request():
    return {'reference_job_id':301, 'name':'Counted latch plate', 'materials':'PETG white',
            'qualification_confirmed':True, 'outputs':[{'product_id':101,'kind':'main','expected':100}]}


@pytest.mark.parametrize('role', ['operator','admin'])
def test_token_qualifies_retained_dispatch_and_records_operator(batch, role):
    authenticate(batch, role=role)
    response = batch[0].post('/products/production/recipes', json=request())
    assert response.status_code == 200, response.text
    recipe = response.json()
    assert recipe['qualified'] and recipe['qualified_by_token']
    assert recipe['payload']['configuration_key'] == 'b'*64
    assert recipe['operator_id'] == 3
    token_id = batch[3].execute(text('SELECT id FROM api_tokens')).scalar_one()
    assert recipe['api_token_id'] == token_id
    stored = json.loads(batch[3].execute(text('SELECT definition FROM production_recipes')).scalar_one())
    assert stored['qualified_by_token'] and stored['api_token_id'] == token_id
    from core.models import AuditLog
    audit = batch[3].execute(select(AuditLog).where(AuditLog.action=='production.recipe_qualified')).scalar_one()
    assert audit.details == {'recipe_id':recipe['id'], 'reference_job_id':301,
                             'operator_id':3, 'api_token_id':token_id, 'auth_kind':'user_api_token'}
    assert batch[0].post('/products/production/recipes', json=request()).status_code == 409
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one() == 1
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one() == 0


@pytest.mark.parametrize('scopes,role', [(['write'],'operator'),(['admin'],'admin'),
    (['agent:write'],'admin'),(['read'],'admin'),([], 'admin'),(['write:production'],'viewer')])
def test_scope_and_role_floor(batch, scopes, role):
    authenticate(batch, scopes=scopes, role=role)
    assert batch[0].post('/products/production/recipes', json=request()).status_code in (401,403)
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one() == 0


@pytest.mark.parametrize('change,status', [
    ('confirmation',409),('running',409),('receipt',409),('stable_key',409),
    ('foreign',404),('education',404),('reference',409),('permission',403),
])
def test_token_cannot_bypass_qualification_refusals(batch, change, status):
    if change == 'reference': make_recipe(batch)
    authenticate(batch)
    data=request(); db=batch[3]
    if change == 'confirmation': data['qualification_confirmed']=False
    elif change == 'running': db.execute(text("UPDATE jobs SET status='printing' WHERE id=301"))
    elif change == 'receipt': db.execute(text('DELETE FROM dispatch_attempt_receipts WHERE job_id=301'))
    elif change == 'stable_key': db.execute(text("UPDATE dispatch_attempt_receipts SET stable_configuration_key='invalid' WHERE job_id=301"))
    elif change == 'foreign': db.execute(text('UPDATE users SET group_id=2 WHERE id=3'))
    elif change == 'education': db.execute(text('UPDATE education_submissions SET job_id=301 WHERE id=14'))
    elif change == 'permission':
        from core.models import SystemConfig
        db.add(SystemConfig(key='rbac_permissions',value={'action_access':{'products.recipes':[]}}))
    db.commit()
    response=batch[0].post('/products/production/recipes',json=data)
    assert response.status_code == status, response.text
    assert db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one() == (1 if change=='reference' else 0)


def test_password_operator_stays_denied_and_token_reaches_outside_qualification(batch):
    batch[1]['role']='operator'
    assert batch[0].post('/products/production/recipes',json=request()).status_code==403
    assert batch[0].post('/products/production/outside-prints/1/qualify-and-bind',json={}).status_code==403
    authenticate(batch,role='operator')
    # Authorized; the empty body is then refused by validation, not permission.
    assert batch[0].post('/products/production/outside-prints/1/qualify-and-bind',json={}).status_code==422


def test_recipe_audit_failure_rolls_back(batch):
    from core.models import AuditLog
    authenticate(batch)
    def fail(*args): raise RuntimeError('synthetic audit persistence failure')
    event.listen(AuditLog,'before_insert',fail)
    try:
        with pytest.raises(RuntimeError,match='synthetic audit persistence failure'):
            batch[0].post('/products/production/recipes',json=request())
    finally: event.remove(AuditLog,'before_insert',fail)
    with Session(batch[3].get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one()==0
