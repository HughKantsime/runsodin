"""Actual model pricing boundaries and known estimates, no real financial records."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def pricing_client(token_client,review_db):
    from modules.models_library.routes import pricing
    client,issue=token_client;client.app.include_router(pricing.router)
    review_db.execute(text("INSERT INTO models(id,name,org_id,color_requirements,build_time_hours) VALUES(199,'Synthetic own price',1,:colors,2),(200,'Synthetic foreign price',2,:colors,2)"),{'colors':'{"slot":{"grams":100}}'});review_db.commit()
    return client,issue


def test_model_cost_requires_authentication(pricing_client):
    client,_=pricing_client
    assert client.get('/models/199/cost').status_code==401


def test_foreign_model_cost_is_not_disclosed(pricing_client):
    client,issue=pricing_client
    response=client.get('/models/200/cost',headers=issue(['read:models']))
    assert response.status_code==404
    assert 'Synthetic foreign price' not in response.text


def test_unrelated_scope_cannot_read_model_cost(pricing_client):
    client,issue=pricing_client
    assert client.get('/models/199/cost',headers=issue(['write:jobs'])).status_code==403


@pytest.mark.parametrize('scope',['read','write:jobs','read:models'])
def test_narrow_token_cannot_change_global_pricing(pricing_client,review_db,scope):
    client,issue=pricing_client
    response=client.put('/pricing-config',headers=issue([scope]),json={'spool_cost':999})
    assert response.status_code==403
    assert review_db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='pricing_config'")).scalar_one()==0


def test_default_model_cost_known_estimates(pricing_client):
    client,issue=pricing_client
    response=client.get('/models/199/cost',headers=issue(['read:models']))
    assert response.status_code==200
    data=response.json()
    assert data['costs']=={'material':2.5,'labor':3.75,'electricity':0.02,'depreciation':0.12,'packaging':0.45,'failure':0.48,'overhead':0.0,'other':0.0}
    assert data['subtotal']==7.32 and data['suggested_price']==10.98


def test_explicit_zero_model_markup_is_preserved(pricing_client,review_db):
    client,issue=pricing_client
    review_db.execute(text('UPDATE models SET markup_percent=0 WHERE id=199'));review_db.commit()
    data=client.get('/models/199/cost',headers=issue(['read:models'])).json()
    assert data['margin_percent']==0 and data['suggested_price']==data['subtotal']


def test_explicit_zero_print_duration_is_preserved(pricing_client,review_db):
    client,issue=pricing_client
    review_db.execute(text('UPDATE models SET build_time_hours=0 WHERE id=199'));review_db.commit()
    data=client.get('/models/199/cost',headers=issue(['read:models'])).json()
    assert data['print_hours']==0 and data['costs']['electricity']==0 and data['costs']['depreciation']==0


def test_explicit_zero_material_cost_is_preserved(pricing_client,review_db):
    client,issue=pricing_client
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES(199,'Synthetic','Synthetic free material','PLA',0)"));review_db.commit()
    data=client.get('/models/199/cost',headers=issue(['read:models'])).json()
    assert data['cost_per_gram']==0 and data['costs']['material']==0

@pytest.mark.parametrize('scope',['admin','interactive'])
def test_authorized_global_pricing_write_and_read(pricing_client,scope):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue=pricing_client
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    response=client.put('/pricing-config',headers=headers,json={'spool_cost':40})
    assert response.status_code==200 and response.json()['spool_cost']==40
    assert client.get('/pricing-config',headers=headers).json()['spool_cost']==40

@pytest.mark.parametrize('mode',['shared','superadmin'])
def test_shared_and_superadmin_model_cost_access_is_preserved(pricing_client,review_db,mode):
    client,issue=pricing_client
    if mode=='shared':review_db.execute(text('UPDATE models SET org_id=NULL WHERE id=200'))
    else:review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.commit()
    response=client.get('/models/200/cost',headers=issue(['read:models']))
    assert response.status_code==200 and response.json()['model_id']==200

@pytest.mark.parametrize('change',[{'spool_weight':0},{'spool_weight':None},{'printer_lifespan':0},{'printer_lifespan':None}])
def test_invalid_divisors_never_commit_pricing_configuration(pricing_client,review_db,change):
    client,issue=pricing_client
    response=client.put('/pricing-config',headers=issue(['admin']),json=change)
    assert response.status_code==422,response.text
    assert review_db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='pricing_config'")).scalar_one()==0


@pytest.mark.parametrize('field',['spool_cost','hourly_rate','electricity_rate','printer_wattage','printer_cost','packaging_cost','failure_rate','monthly_rent','parts_per_month','post_processing_min','packing_min','support_min','default_margin','other_costs'])
@pytest.mark.parametrize('invalid',[None,-1,'Infinity','NaN'])
def test_invalid_numeric_settings_leave_existing_configuration_unchanged(pricing_client,review_db,field,invalid):
    client,issue=pricing_client
    headers=issue(['admin'])
    original=client.put('/pricing-config',headers=headers,json={'spool_cost':40}).json()
    response=client.put('/pricing-config',headers=headers,json={field:invalid})
    assert response.status_code==422,response.text
    assert client.get('/pricing-config',headers=headers).json()==original


def test_partial_pricing_update_preserves_existing_settings(pricing_client):
    client,issue=pricing_client
    headers=issue(['admin'])
    first=client.put('/pricing-config',headers=headers,json={'spool_cost':40,'hourly_rate':27}).json()
    response=client.put('/pricing-config',headers=headers,json={'packaging_cost':2})
    assert response.status_code==200
    expected={**first,'packaging_cost':2}
    assert response.json()==expected
    assert client.get('/models/199/cost',headers=issue(['read:models'])).status_code==200


@pytest.mark.parametrize('kind',['free_material','zero_markup','zero_duration','explicit_zero_inputs'])
def test_job_cost_service_preserves_known_zero_values(pricing_client,review_db,kind):
    from modules.models_library.services import calculate_job_cost
    client,issue=pricing_client
    if kind=='free_material':
        review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES(199,'Synthetic','Synthetic free material','PLA',0)"))
    elif kind=='zero_markup':
        review_db.execute(text('UPDATE models SET markup_percent=0 WHERE id=199'))
    elif kind=='zero_duration':
        review_db.execute(text('UPDATE models SET build_time_hours=0 WHERE id=199'))
    review_db.commit()
    if kind=='explicit_zero_inputs':
        actual=calculate_job_cost(review_db,model_id=199,filament_grams=0,print_hours=0)
        standalone=calculate_job_cost(review_db,filament_grams=0,print_hours=0)
        assert actual==standalone
    else:
        estimate=client.get('/models/199/cost',headers=issue(['read:models'])).json()
        actual=calculate_job_cost(review_db,model_id=199)
        assert actual==(estimate['subtotal'],estimate['suggested_price'],estimate['margin_percent'])


@pytest.mark.parametrize('stored',[{'hourly_rate':None},{'spool_weight':0},['Synthetic malformed settings']])
def test_invalid_stored_configuration_is_not_overwritten(pricing_client,review_db,stored):
    import json
    client,issue=pricing_client
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('pricing_config',:value)"),{'value':json.dumps(stored)})
    review_db.commit()
    response=client.put('/pricing-config',headers=issue(['admin']),json={'packaging_cost':2})
    assert response.status_code in (409,422)
    assert json.loads(review_db.execute(text("SELECT value FROM system_config WHERE key='pricing_config'")).scalar_one())==stored


def test_partial_update_preserves_zero_and_unknown_keys(pricing_client,review_db):
    import json
    client,issue=pricing_client
    saved={'hourly_rate':0,'future_extension':{'enabled':True}}
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('pricing_config',:value)"),{'value':json.dumps(saved)})
    review_db.commit()
    response=client.put('/pricing-config',headers=issue(['admin']),json={'packaging_cost':2})
    assert response.status_code==200
    assert response.json()['hourly_rate']==0 and response.json()['future_extension']==saved['future_extension']


@pytest.mark.parametrize('model_id',[None,199,999999])
def test_omitted_service_inputs_use_model_or_fallback_values(pricing_client,review_db,model_id):
    from modules.models_library.services import calculate_job_cost
    expected=(7.32,10.98,50.0) if model_id==199 else (4.57,6.86,50.0)
    assert calculate_job_cost(review_db,model_id=model_id)==expected


@pytest.mark.parametrize('field',['hourly_rate','spool_weight','printer_lifespan','parts_per_month'])
def test_boolean_financial_settings_are_rejected(pricing_client,review_db,field):
    client,issue=pricing_client
    response=client.put('/pricing-config',headers=issue(['admin']),json={field:True})
    assert response.status_code==422
    assert review_db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='pricing_config'")).scalar_one()==0


def test_numeric_strings_are_normalized_and_zero_overhead_count_is_supported(pricing_client):
    client,issue=pricing_client
    headers=issue(['admin'])
    response=client.put('/pricing-config',headers=headers,json={'hourly_rate':'27.5','printer_wattage':'100','parts_per_month':0})
    assert response.status_code==200
    assert response.json()['hourly_rate']==27.5 and response.json()['printer_wattage']==100
    estimate=client.get('/models/199/cost',headers=issue(['read:models']))
    assert estimate.status_code==200 and estimate.json()['costs']['overhead']==0



def test_finite_configuration_cannot_publish_nonfinite_estimates(pricing_client):
    client,issue=pricing_client
    response=client.put('/pricing-config',headers=issue(['admin']),json={'spool_cost':1e308,'spool_weight':1e-308})
    assert response.status_code==200
    result=client.get('/models/199/cost',headers=issue(['read:models']))
    assert result.status_code==422
    assert result.json()['detail']=='Pricing calculation exceeds supported numeric range'


def test_job_calculator_cannot_return_nonfinite_estimates(pricing_client, review_db):
    from fastapi import HTTPException
    from modules.models_library.services import calculate_job_cost
    client, issue = pricing_client
    saved = client.put('/pricing-config', headers=issue(['admin']),
                       json={'spool_cost': 1e308, 'spool_weight': 1e-308})
    assert saved.status_code == 200
    with pytest.raises(HTTPException) as rejected:
        calculate_job_cost(review_db, model_id=199)
    assert rejected.value.status_code == 422
    assert rejected.value.detail == 'Pricing calculation exceeds supported numeric range'
