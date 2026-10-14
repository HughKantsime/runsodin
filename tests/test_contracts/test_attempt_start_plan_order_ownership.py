"""Actual scheduled order graph must survive durable dispatch into known birth."""
import hashlib
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_order_production_behavior import production_order
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_economic_creator_wiring import creators


def test_order_linked_org_dispatch_creates_owned_known_attempt(production_order, review_db, tmp_path, monkeypatch, creators):
    from core import auth
    from core.attempt_start_plan import prepare_start, create_start, finalize_start
    from core.attempt_start_transaction import start_transaction
    from modules.printers import dispatch
    client, _, order_id = production_order
    response = client.post(f'/orders/{order_id}/schedule')
    assert response.status_code == 200, response.text
    job_id = review_db.execute(text('SELECT min(j.id) FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'), {'id': order_id}).scalar_one()
    review_db.commit()
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(58,101,'owned-order.gcode','owned-order.gcode',:path,1)"), {'path': str(path)})
        conn.execute(text('UPDATE jobs SET printer_id=59,print_file_id=58,plate_index=0,payload_sha256=:hash WHERE id=:id'), {'id': job_id, 'hash': hashlib.sha256(path.read_bytes()).hexdigest()})
    source_client, auth_headers, _, _ = creators
    source = source_client.post('/api/spools', headers=auth_headers(), json={'filament_id':80,'purchase_evidence':{'currency':'USD','price':'7.25','net_weight_g':'1000'}})
    assert source.status_code == 200, source.text
    with engine.begin() as conn:
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=:id WHERE printer_id=59 AND slot_number=1'), {'id':source.json()['id']})
        conn.execute(text("INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES('org',1,'USD') ON CONFLICT(owner_scope,owner_key) DO UPDATE SET currency='USD'"))
    names = []
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', lambda jid, source, name, creds: (names.append(name) or True, 'Synthetic accepted physical sender'))
    result = dispatch.dispatch_job(59, job_id)
    assert result[0], result[1]
    with engine.begin() as conn:
        with start_transaction(conn) as scope:
            scope.fence_dispatch_membership(conn, education=True)
            plan = prepare_start(conn, scope, printer_id=59, observed_filename=names[0], protocol='moonraker', observation_fields={'filename': names[0], 'job_name': names[0], 'dispatch_key': auth.SECRET_KEY.encode()}, capture_required=True)
            assert plan.branch == 'fresh_authorized' and plan.job_id == job_id
            identifier = create_start(conn, scope, plan, {'started_at': '2026-10-13 12:00:00'})
            from modules.archives.attempt_accounting import capture_attempt_spools
            assert capture_attempt_spools(conn, identifier, 59, source='owned-test', start_scope=scope, start_plan=plan)
            assert finalize_start(conn, scope, plan) == identifier
    with engine.connect() as conn:
        row = conn.execute(text('SELECT p.economic_birth_nonce,p.economic_uid,p.scheduled_job_id,l.state,l.owner_scope,l.owner_key FROM print_jobs p JOIN cost_source_lifetimes l ON l.birth_nonce=p.economic_birth_nonce WHERE p.id=:id'), {'id': identifier}).one()
        assert row[0] and row[1] and tuple(row[2:]) == (job_id, 'known', 'org', 1)


    with engine.connect() as conn:
        snapshot = json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'), {'id':identifier}).scalar_one())
        valuation = snapshot['unit_valuation']
        from modules.archives.valuation_financial_graph import prove_financial_graph
        from modules.archives.valuation_parent_lock import lock_financial_attempt
        locked = lock_financial_attempt(conn, identifier)
        graph = prove_financial_graph(conn, identifier)
        assert locked == (graph['job_id'], graph['attempt_birth'], graph['attempt_uid'])
        assert graph['attempt_uid'] == valuation['attempt_uid']
        assert graph['owner_scope'] == 'org'
        from modules.archives.valuation_projection import parse_private_valuation
        assert parse_private_valuation(json.dumps(snapshot)) == valuation
        assert valuation['state'] == 'known' and valuation['target']['owner_scope'] == 'org'
        assert valuation['sources'][0]['price_decimal'] == '7.25'
        assert valuation['sources'][0]['net_weight_g_decimal'] == '1000'

@pytest.mark.parametrize('price,net,source_currency,target_currency,expected_state,source_actor', [('0','1000','USD','USD','known','platform'), ('12.99','750.25','USD','USD','known','platform'), ('12.99','750.25','EUR','USD','unknown','platform'), ('12.99','750.25','USD',None,'unknown','platform'), ('12.99','750.25','USD','USD','unknown','admin')])
def test_verified_company_order_dispatch_creates_known_global_attempt(creators, review_db, tmp_path, monkeypatch, price, net, source_currency, target_currency, expected_state, source_actor):
    from core import auth as auth_module
    from core.registry import registry
    from core.attempt_start_plan import prepare_start, create_start, finalize_start
    from core.attempt_start_transaction import start_transaction
    from modules.orders.production import OrderFulfillmentService
    from modules.orders.routes.orders_crud import router
    from modules.printers import dispatch
    client, auth, _, _ = creators
    client.app.include_router(router, prefix='/api')
    monkeypatch.setitem(registry._providers, 'OrderFulfillmentProvider', OrderFulfillmentService())
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.execute(text('UPDATE printers SET org_id=NULL WHERE id=59'))
        conn.execute(text("INSERT INTO models(id,name,org_id,quantity_per_bed,build_time_hours,default_filament_type) VALUES(101,'Owned company model',NULL,1,1,'PLA')"))
        conn.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(101,'Owned company product',NULL,20)"))
        conn.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(101,101,1)'))
        conn.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(58,101,'owned-company.gcode','owned-company.gcode',:path,NULL)"), {'path': str(path)})
    response = client.post('/api/orders', headers=auth('platform'), json={'order_number': 'OWNED-PLAN-COMPANY', 'items': [{'product_id': 101, 'quantity': 1, 'unit_price': 20}]})
    assert response.status_code == 200, response.text
    order_id = response.json()['id']
    response = client.post(f'/api/orders/{order_id}/schedule', headers=auth('platform'))
    assert response.status_code == 200, response.text
    with engine.begin() as conn:
        job_id = conn.execute(text('SELECT min(j.id) FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'), {'id': order_id}).scalar_one()
        origin = conn.execute(text('SELECT o.economic_uid,o.economic_global_origin_nonce,g.origin_nonce FROM orders o JOIN cost_global_order_origins g ON g.order_uid=o.economic_uid WHERE o.id=:id'), {'id': order_id}).one()
        assert origin[0] and origin[1] == origin[2]
        conn.execute(text('UPDATE jobs SET printer_id=59,print_file_id=58,plate_index=0,payload_sha256=:hash WHERE id=:id'), {'id': job_id, 'hash': hashlib.sha256(path.read_bytes()).hexdigest()})
    source = client.post('/api/spools', headers=auth(source_actor), json={'filament_id':80,'purchase_evidence': {'currency':source_currency,'price':price,'net_weight_g':net}})
    assert source.status_code == 200, source.text
    spool_id = source.json()['id']
    with engine.begin() as conn:
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=:id WHERE printer_id=59 AND slot_number=1'), {'id':spool_id})
        conn.execute(text("INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES('global',0,:currency) ON CONFLICT(owner_scope,owner_key) DO UPDATE SET currency=excluded.currency"), {'currency':target_currency})
    names = []
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', lambda jid, source, name, creds: (names.append(name) or True, 'Synthetic accepted physical sender'))
    result = dispatch.dispatch_job(59, job_id)
    assert result[0], result[1]
    with engine.begin() as conn:
        with start_transaction(conn) as scope:
            scope.fence_dispatch_membership(conn, education=True)
            plan = prepare_start(conn, scope, printer_id=59, observed_filename=names[0], protocol='moonraker', observation_fields={'filename': names[0], 'job_name': names[0], 'dispatch_key': auth_module.SECRET_KEY.encode()}, capture_required=True)
            assert plan.branch == 'fresh_authorized' and plan.job_id == job_id
            identifier = create_start(conn, scope, plan, {'started_at': '2026-10-13 12:00:00'})
            from modules.archives.attempt_accounting import capture_attempt_spools
            assert capture_attempt_spools(conn, identifier, 59, source='owned-test', start_scope=scope, start_plan=plan)
            assert finalize_start(conn, scope, plan) == identifier
    with engine.connect() as conn:
        row = conn.execute(text('SELECT p.economic_birth_nonce,p.economic_uid,p.scheduled_job_id,l.state,l.owner_scope,l.owner_key FROM print_jobs p JOIN cost_source_lifetimes l ON l.birth_nonce=p.economic_birth_nonce WHERE p.id=:id'), {'id': identifier}).one()
        assert row[0] and row[1] and tuple(row[2:]) == (job_id, 'known', 'global', 0)
    with engine.connect() as conn:
        snapshot = json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'), {'id': identifier}).scalar_one())
        assert snapshot['version'] == 1
        valuation = snapshot['unit_valuation']
        from modules.archives.valuation_projection import parse_private_valuation
        assert parse_private_valuation(json.dumps(snapshot)) == valuation
        assert valuation['version'] == 2 and valuation['state'] == expected_state
        from modules.archives.valuation_financial_graph import prove_financial_graph
        from modules.archives.valuation_parent_lock import lock_financial_attempt
        locked = lock_financial_attempt(conn, identifier)
        graph = prove_financial_graph(conn, identifier)
        assert locked == (graph['job_id'], graph['attempt_birth'], graph['attempt_uid'])
        assert graph['attempt_uid'] == valuation['attempt_uid'] and graph['owner_scope'] == 'global'
        for table, field, value in [('cost_attempt_bindings','attempt_uid',graph['attempt_uid']), ('cost_job_bindings','job_uid',graph['job_uid']), ('cost_order_memberships','child_uid',graph['item_uid']), ('cost_global_order_origins','order_uid',graph['order_uid'])]:
            savepoint = conn.begin_nested()
            try:
                from sqlalchemy.exc import IntegrityError
                with pytest.raises(IntegrityError):
                    conn.execute(text(f'DELETE FROM {table} WHERE {field}=:value'), {'value':value})
            finally:
                savepoint.rollback()
            assert prove_financial_graph(conn, identifier) == graph
        from datetime import datetime, timezone
        assert valuation['captured_at'].endswith('Z')
        assert datetime.fromisoformat(valuation['captured_at']).utcoffset() == timezone.utc.utcoffset(None)
        assert valuation['target']['owner_scope'] == 'global' and (valuation['target'].get('currency') == target_currency if source_actor == 'platform' else True)
        if source_actor == 'platform':
            assert valuation['sources'][0]['price_decimal'] == price
            assert valuation['sources'][0]['net_weight_g_decimal'] == net
        else:
            assert valuation['sources'] == []
            assert valuation['reason'] == 'unresolved_material_mapping'
            assert snapshot['material_plan']['allocations'] == []
        assert not any(key in valuation for key in ('actual_cost', 'estimated_cost', 'total_cost'))
    original_snapshot = json.dumps(snapshot, sort_keys=True)
    replaced = client.patch(f'/api/spools/{spool_id}', headers=auth(source_actor), json={'purchase_evidence':{'currency':'USD','price':'99','net_weight_g':'1000'},'expected_purchase_revision':1})
    assert replaced.status_code == 200, replaced.text
    from modules.archives.attempt_accounting import capture_attempt_spools
    with engine.begin() as conn:
        conn.execute(text("UPDATE finance_currency_settings SET currency='EUR' WHERE owner_scope='global' AND owner_key=0"))
        assert capture_attempt_spools(conn, identifier, 59, source='legacy-recovery') is False
    with engine.connect() as conn:
        retained = json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'), {'id':identifier}).scalar_one())
        assert json.dumps(retained, sort_keys=True) == original_snapshot


def test_frozen_value_fault_rolls_back_attempt_lifetime_and_registry(creators, review_db, tmp_path, monkeypatch):
    from modules.archives import first_start_valuation
    original = first_start_valuation.freeze_unit_valuation
    recorded = {}
    def fault(executor, scope, plan, attempt_id, material_plan, material_fence):
        value = original(executor, scope, plan, attempt_id, material_plan, material_fence)
        assert value['state'] == 'known'
        recorded.update(id=attempt_id, birth=value['attempt_birth'], uid=value['attempt_uid'])
        raise RuntimeError('Owned freeze fault')
    monkeypatch.setattr(first_start_valuation, 'freeze_unit_valuation', fault)
    with pytest.raises(RuntimeError, match='Owned freeze fault'):
        test_verified_company_order_dispatch_creates_known_global_attempt(creators, review_db, tmp_path, monkeypatch, '0', '1000', 'USD', 'USD', 'known', 'platform')
    with review_db.get_bind().connect() as observer:
        assert observer.execute(text('SELECT count(*) FROM print_jobs WHERE id=:id'), {'id':recorded['id']}).scalar_one() == 0
        assert observer.execute(text('SELECT count(*) FROM cost_source_lifetimes WHERE birth_nonce=:birth'), {'birth':recorded['birth']}).scalar_one() == 0
        assert observer.execute(text('SELECT count(*) FROM cost_identities WHERE identity_uid=:uid'), {'uid':recorded['uid']}).scalar_one() == 0
        assert observer.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one() == 1
        assert observer.execute(text('SELECT count(*) FROM dispatch_attempt_receipts WHERE observed_print_job_id IS NOT NULL')).scalar_one() == 0


def test_private_capture_rejects_duplicate_and_expired_plan(creators, review_db, tmp_path, monkeypatch):
    from core import attempt_start_plan
    from core.economic_source_creation import SourceCreationError
    from modules.archives.attempt_accounting import capture_attempt_spools
    complete = attempt_start_plan._complete_fresh_capture
    retained = {}
    def completion(executor, scope, plan, attempt_id):
        complete(executor, scope, plan, attempt_id)
        retained.update(scope=scope, plan=plan, id=attempt_id)
        with pytest.raises(SourceCreationError, match='Fresh capture capability unavailable'):
            capture_attempt_spools(executor, attempt_id, 59, source='duplicate', start_scope=scope, start_plan=plan)
    monkeypatch.setattr(attempt_start_plan, '_complete_fresh_capture', completion)
    test_verified_company_order_dispatch_creates_known_global_attempt(creators, review_db, tmp_path, monkeypatch, '0', '1000', 'USD', 'USD', 'known', 'platform')
    with review_db.get_bind().begin() as executor:
        original = executor.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'), {'id':retained['id']}).scalar_one()
        with pytest.raises(SourceCreationError, match='no longer current'):
            capture_attempt_spools(executor, retained['id'], 59, source='expired', start_scope=retained['scope'], start_plan=retained['plan'])
        assert executor.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'), {'id':retained['id']}).scalar_one() == original


@pytest.mark.parametrize('scope,plan', [(None, object()), (object(), None)])
def test_private_capture_refuses_unpaired_capability_before_mutation(review_db, scope, plan):
    from core.economic_source_creation import SourceCreationError
    from modules.archives.attempt_accounting import capture_attempt_spools
    with review_db.get_bind().begin() as executor:
        before = executor.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()
        with pytest.raises(SourceCreationError, match='Paired fresh capture'):
            capture_attempt_spools(executor, 999, 59, source='unpaired', start_scope=scope, start_plan=plan)
        assert executor.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == before
