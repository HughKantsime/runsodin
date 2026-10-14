"""Real private endpoint after genuine company order/source/dispatch/capture."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_attempt_start_plan_order_ownership import test_verified_company_order_dispatch_creates_known_global_attempt as create_company_attempt


@pytest.fixture
def private_attempt(creators,review_db,tmp_path,monkeypatch):
    create_company_attempt(creators,review_db,tmp_path,monkeypatch,'12.99','750.25','USD','USD','known','platform')
    from core.registry import registry
    from modules.archives import register_providers
    monkeypatch.setattr(registry,'_providers',dict(registry._providers))
    register_providers(registry)
    from modules.orders.routes.attempt_valuation import router
    client,auth,token,_=creators
    client.app.include_router(router)
    identifier=review_db.execute(text('SELECT max(id) FROM print_jobs WHERE economic_uid IS NOT NULL')).scalar_one()
    # Shared dispatch support creates an unrelated tenant file at the same
    # path. Give that synthetic file its own path for the clean positive; the
    # dedicated shared-path negative below restores the conflict.
    review_db.execute(text("UPDATE print_files SET stored_path=stored_path || '.unrelated' WHERE id=52"))
    review_db.commit()
    return client,auth,token,identifier


def test_actual_platform_reads_retained_basis_and_private_cache(private_attempt):
    client,auth,_,identifier=private_attempt
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth('platform'))
    assert result.status_code==200,result.text
    assert result.headers['cache-control']=='private, no-store'
    value=result.json()
    assert value['target']['owner_scope']=='global'
    assert value['sources'][0]['price_decimal']=='12.99'
    assert value['sources'][0]['net_weight_g_decimal']=='750.25'
    assert 'actual_cost' not in value


def test_actual_tenant_cannot_read_company_basis(private_attempt):
    client,auth,_,identifier=private_attempt
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth('admin'))
    assert result.status_code==404,result.text
    assert '12.99' not in result.text


@pytest.mark.parametrize('scopes',[['read:spools'],['read:jobs'],['write']])
def test_narrow_keys_do_not_enter_private_audience(private_attempt,scopes):
    client,_,token,identifier=private_attempt
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=token(scopes))
    assert result.status_code==403,result.text
    assert '12.99' not in result.text


@pytest.mark.parametrize('payload,status',[(None,200),('{"version":1,"material_plan":{}}',200),('{"version":1,"unit_valuation":null}',404),('{"version":1,"version":1}',404)])
def test_missing_legacy_extension_distinct_from_malformed(private_attempt,review_db,payload,status):
    client,auth,_,identifier=private_attempt
    review_db.execute(text('UPDATE print_jobs SET spool_snapshot=:payload WHERE id=:id'),{'payload':payload,'id':identifier})
    review_db.commit()
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth('platform'))
    assert result.status_code==status,result.text
    if status==200:assert result.json()=={'state':'unavailable','reason':'missing_historical_valuation'}
    denied=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth('admin'))
    assert denied.status_code==404


def test_foreign_shared_path_alias_refuses_company_positive(private_attempt,review_db):
    client,auth,_,identifier=private_attempt
    review_db.execute(text('UPDATE print_files SET stored_path=(SELECT stored_path FROM print_files WHERE id=58) WHERE id=52'))
    review_db.commit()
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth('platform'))
    assert result.status_code==404,result.text
    assert '12.99' not in result.text


from tests.test_contracts.test_order_production_behavior import production_order
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_attempt_start_plan_order_ownership import test_order_linked_org_dispatch_creates_owned_known_attempt as create_org_attempt


@pytest.fixture
def org_attempt(production_order,creators,review_db,tmp_path,monkeypatch):
    create_org_attempt(production_order,review_db,tmp_path,monkeypatch,creators)
    from core.registry import registry
    from modules.archives import register_providers
    monkeypatch.setattr(registry,'_providers',dict(registry._providers))
    register_providers(registry)
    from modules.orders.routes.attempt_valuation import router
    client,auth,token,_=creators
    client.app.include_router(router)
    identifier=review_db.execute(text('SELECT max(id) FROM print_jobs WHERE economic_uid IS NOT NULL')).scalar_one()
    review_db.rollback()
    return client,auth,token,identifier


@pytest.mark.parametrize('carrier',['jwt','broad_key','resource_key','admin_key'])
def test_actual_org_owner_carriers_read_exact_basis(org_attempt,carrier):
    client,auth,token,identifier=org_attempt
    headers=auth() if carrier=='jwt' else token({'broad_key':['read'],'resource_key':['read','read:spools'],'admin_key':['admin']}[carrier])
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['target']['owner_key']==1
    assert result.json()['sources'][0]['price_decimal']=='7.25'
    assert result.headers['cache-control']=='private, no-store'


def test_actual_foreign_org_and_unassigned_viewer_receive_no_basis(org_attempt):
    client,auth,_,identifier=org_attempt
    for name in ('foreign','unassigned'):
        result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth(name))
        assert result.status_code==404,result.text
        assert '7.25' not in result.text
