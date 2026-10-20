"""Read-only Square HTTP workflow with real persistence and synthetic upstream reads."""
import io
import json
import uuid
import zipfile

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text, select
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def shop(review_db, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.orders.routes import square, products
    from modules.orders.square_client import SquareReader
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    review_db.execute(text("INSERT INTO products(id,name,org_id) VALUES(101,'Own part',1),(102,'Foreign part',2),(103,'Shared part',NULL),(104,'Own second part',1)"))
    review_db.commit()
    user = {'id': 3, 'role': 'admin', 'group_id': 1, '_auth_kind': 'jwt', 'is_active': True}
    calls = []
    def upstream(self, method, path, *, params=None, body=None):
        calls.append((method, path, params, body))
        if path == '/v2/merchants/me':
            return {'merchant': {'id': 'M2' if self.token.endswith('other') else 'M1', 'status': 'ACTIVE'}}
        if path == '/v2/locations':
            return {'locations': [{'id': 'L1', 'name': 'Shop', 'status': 'ACTIVE', 'merchant_id': 'M1'}, {'id': 'FOREIGN', 'status': 'ACTIVE', 'merchant_id': 'OTHER'}]}
        variation = {'id': 'V1', 'type': 'ITEM_VARIATION', 'present_at_all_locations': True,
                     'item_variation_data': {'item_id': 'I1', 'name': 'Regular', 'sku': 'PART', 'track_inventory': True}}
        parent = {'id': 'I1', 'type': 'ITEM', 'present_at_all_locations': True, 'item_data': {'name': 'Part', 'variations': [variation]}}
        if path == '/v2/catalog/list':
            return {'objects': [parent], 'cursor': 'next' if not params.get('cursor') else None}
        if path == '/v2/catalog/object/V1':
            return {'object': variation, 'related_objects': [parent]}
        if path == '/v2/inventory/counts/batch-retrieve':
            return {'counts': [{'catalog_object_id': 'V1', 'location_id': 'L1', 'state': 'IN_STOCK', 'quantity': '7.00000', 'calculated_at': '2026-10-20T12:00:00Z'}]}
        raise AssertionError('Unexpected external operation')
    monkeypatch.setattr(SquareReader, '_read', upstream)
    app = FastAPI(); app.include_router(square.router); app.include_router(products.router)
    def session():
        with Session(review_db.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    return client, user, calls, review_db, app, upstream


def connect(client, suffix=''):
    token = 'fixture-' + uuid.uuid4().hex + suffix
    current = client.get('/products/square').json()
    r = client.put('/products/square', json={'access_token': token, 'environment': 'sandbox', 'expected_revision': current['revision']})
    assert r.status_code == 200, r.text
    assert token not in r.text
    return r.json(), token


def mapped(shop):
    client, *_ = shop
    status, _ = connect(client)
    r = client.put('/products/101/square', json={'revision': status['revision'], 'location_id': 'L1', 'variation_id': 'V1'})
    assert r.status_code == 200, r.text
    return r.json()


def test_connect_catalog_map_stock_and_disconnect_without_remote_writes(shop):
    client, user, calls, db, *_ = shop
    status = mapped(shop)
    assert client.get('/products/square/locations', params={'revision': status['revision']}).json()['locations'] == [{'id': 'L1', 'name': 'Shop'}]
    first = client.get('/products/square/catalog', params={'revision': status['revision']}).json()
    assert first['items'][0]['sku'] == 'PART' and first['cursor'] == 'next'
    assert client.get('/products/square/catalog', params={'revision': status['revision'], 'cursor': first['cursor']}).json()['cursor'] is None
    r = client.get('/products/101/square', params={'revision': status['revision']})
    assert r.status_code == 200 and r.json()['quantity'] == '7.00000'
    assert r.json()['calculated_at'] == '2026-10-20T12:00:00Z'
    assert db.execute(text('SELECT COUNT(*) FROM products')).scalar() == 4
    assert all(method == 'GET' or path == '/v2/inventory/counts/batch-retrieve' for method, path, _, _ in calls)
    assert client.delete('/products/square', params={'revision': status['revision']}).json()['connected'] is False


def test_token_encrypted_and_no_error_or_audit_echo(shop, caplog):
    from core.models import SystemConfig
    client, _, _, db, *_ = shop
    status, token = connect(client)
    value = db.execute(select(SystemConfig.value).where(SystemConfig.key == 'square_readonly:1')).scalar_one()
    assert token not in str(value) and value.startswith('gAAAA')
    for payload in [{'access_token': {'bad': token}, 'environment': 'sandbox'}, {'access_token': token, 'environment': token}, {'access_token': token, 'environment': 'sandbox', 'extra': token}]:
        r = client.put('/products/square', json=payload)
        assert r.status_code == 422 and token not in r.text
    assert token not in caplog.text
    assert token not in str(db.execute(text('SELECT details FROM audit_logs')).fetchall())


def test_unknown_key_fail_closed_without_upstream_calls(shop, monkeypatch):
    client, _, calls, *_ = shop
    connect(client); calls.clear()
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    assert client.get('/products/square').status_code == 503 and calls == []


@pytest.mark.parametrize('product_id', [102, 103, 999])
def test_exact_tenant_ownership_even_for_admin(shop, product_id):
    client, _, calls, *_ = shop
    status, _ = connect(client); calls.clear()
    r = client.put(f'/products/{product_id}/square', json={'revision': status['revision'], 'location_id': 'L1', 'variation_id': 'V1'})
    assert r.status_code == 404 and calls == []


@pytest.mark.parametrize('role', ['operator', 'viewer'])
def test_nonadmin_reads_but_cannot_connect_map_or_disconnect(shop, role):
    client, user, *_ = shop
    status = mapped(shop); user['role'] = role
    assert client.get('/products/101/square', params={'revision': status['revision']}).status_code == 200
    assert client.put('/products/square', json={'access_token': 'fixture', 'environment': 'sandbox'}).status_code == 403
    assert client.delete('/products/square', params={'revision': status['revision']}).status_code == 403
    assert client.delete('/products/101/square', params={'revision': status['revision']}).status_code == 403


def test_api_token_cannot_change_connection_or_mapping(shop):
    client, user, *_ = shop
    status = mapped(shop); user['_auth_kind'] = 'user_api_token'; user['_token_scopes'] = ['read', 'write']
    assert client.delete('/products/square', params={'revision': status['revision']}).status_code == 403
    assert client.delete('/products/101/square', params={'revision': status['revision']}).status_code == 403


def test_unassigned_viewer_denied_global_admin_uses_only_global_products(shop):
    client, user, *_ = shop
    connect(client); user['group_id'] = None; user['role'] = 'viewer'
    assert client.get('/products/square').status_code == 403
    user['role'] = 'admin'
    status, _ = connect(client)
    assert client.put('/products/101/square', json={'revision': status['revision'], 'location_id': 'L1', 'variation_id': 'V1'}).status_code == 404
    assert client.put('/products/103/square', json={'revision': status['revision'], 'location_id': 'L1', 'variation_id': 'V1'}).status_code == 200


def test_mapping_duplicate_rejected_and_account_switch_invalidates(shop):
    client, *_ = shop
    status = mapped(shop)
    assert client.put('/products/104/square', json={'revision': status['revision'], 'location_id': 'L1', 'variation_id': 'V1'}).status_code == 409
    changed, _ = connect(client, 'other')
    assert changed['mappings'] == []
    assert client.get('/products/101/square', params={'revision': status['revision']}).status_code == 409


def test_same_account_rotation_preserves_map_but_invalidates_revision(shop):
    client, *_ = shop
    old = mapped(shop); new, _ = connect(client)
    assert new['mappings'] == old['mappings'] and new['revision'] != old['revision']
    assert client.get('/products/square/catalog', params={'revision': old['revision']}).status_code == 409


def test_stock_result_discarded_if_connection_changes_during_read(shop, monkeypatch):
    from modules.orders.square_client import SquareReader
    from modules.orders import square_store
    client, _, _, db, *_ = shop
    status = mapped(shop)
    def changed(self, *args):
        with Session(db.get_bind()) as other:
            doc, previous = square_store.load(other, 1); square_store.save(other, 1, doc, previous); other.commit()
        return {'state': 'known', 'quantity': '999', 'calculated_at': None}
    monkeypatch.setattr(SquareReader, 'count', changed)
    r = client.get('/products/101/square', params={'revision': status['revision']})
    assert r.status_code == 409 and '999' not in r.text


def test_stale_map_cannot_return_after_reconnect(shop, monkeypatch):
    from modules.orders.square_client import SquareReader
    from modules.orders import square_store
    client, _, _, db, *_ = shop
    old, _ = connect(client)
    def changed(self, *args):
        with Session(db.get_bind()) as other:
            square_store.forget_tenant(other, 1); other.commit()
        return {'item_name': 'Part', 'variation_name': 'Regular', 'tracked': True}
    monkeypatch.setattr(SquareReader, 'variation', changed)
    assert client.put('/products/101/square', json={'revision': old['revision'], 'location_id': 'L1', 'variation_id': 'V1'}).status_code == 409
    assert client.get('/products/square').json()['connected'] is False


def test_product_deletion_prunes_mapping(shop):
    client, *_ = shop
    mapped(shop)
    assert client.delete('/products/101').status_code == 204
    assert client.get('/products/square').json()['mappings'] == []


@pytest.mark.parametrize('route', ['groups', 'orgs'])
def test_owner_deletion_removes_encrypted_connection(shop, route, monkeypatch):
    from core.models import SystemConfig
    from modules.orders import square_store
    from modules.organizations import routes, routes_users
    client, user, _, db, app, *_ = shop
    db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(88,'Temporary owner',1)")); db.commit()
    with Session(db.get_bind()) as other:
        square_store.save(other, 88, {'version': 1, 'tenant': 88, 'token': 'fixture', 'environment': 'sandbox', 'merchant_id': 'M1', 'mappings': {}}, None); other.commit()
    monkeypatch.setattr(routes_users, 'require_feature', lambda *a: None)
    app.include_router(routes.router); app.include_router(routes_users.router)
    user['group_id'] = None
    r = client.delete('/'+route+'/88')
    assert r.status_code == 200, r.text
    assert db.execute(select(SystemConfig.value).where(SystemConfig.key == 'square_readonly:88')).scalar_one_or_none() is None


def test_support_bundle_excludes_encrypted_connection(shop):
    from modules.system import routes_admin
    client, user, *_ = shop
    app = shop[4]; app.include_router(routes_admin.router)
    connect(client); user['group_id'] = None
    r = client.get('/admin/support-bundle'); assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        assert 'square_readonly:' not in archive.read('settings_safe.json').decode()


def test_product_deleted_during_mapping_validation_cannot_leave_mapping(shop,monkeypatch):
    from modules.orders.square_client import SquareReader
    client,*_=shop;status,_=connect(client)
    def removed(self,*args):
        assert client.delete('/products/101').status_code==204
        return {'item_name':'Part','variation_name':'Regular','tracked':True}
    monkeypatch.setattr(SquareReader,'variation',removed)
    r=client.put('/products/101/square',json={'revision':status['revision'],'location_id':'L1','variation_id':'V1'})
    assert r.status_code==404 and client.get('/products/square').json()['mappings']==[]


def test_owner_deleted_before_first_connection_save_cannot_leave_credentials(shop,monkeypatch):
    from modules.orders.square_client import SquareReader
    from modules.orders import square_store
    client,user,_,db,*_=shop
    db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(88,'Fresh owner',1)"))
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(88,'fresh-owner','fixture','admin',1,88)"));db.commit()
    user.update(id=88,group_id=88)
    def removed(self):
        with Session(db.get_bind()) as other:
            square_store.lock_owner(other,88)
            other.execute(text('UPDATE users SET group_id=NULL WHERE group_id=88'))
            other.execute(text('DELETE FROM groups WHERE id=88'));square_store.forget_tenant(other,88);other.commit()
        return 'M1'
    monkeypatch.setattr(SquareReader,'merchant',removed)
    r=client.put('/products/square',json={'access_token':'synthetic','environment':'sandbox'})
    assert r.status_code==409
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='square_readonly:88'")).scalar()==0
