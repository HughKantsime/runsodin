"""New verified company orders require future-only retained origin proof."""
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
import pytest


def test_actual_platform_admin_order_records_prospective_global_origin(creators, review_db):
    from modules.orders.routes.orders_crud import router
    client, auth, _, _ = creators
    client.app.include_router(router, prefix='/api')
    response = client.post('/api/orders', headers=auth('platform'),
                           json={'order_number': 'OWNED-FUTURE-GLOBAL-ORIGIN', 'platform': 'direct'})
    assert response.status_code == 200, response.text
    identifier = response.json()['id']
    review_db.expire_all()
    source = review_db.execute(text('SELECT economic_uid,economic_global_origin_nonce,org_id FROM orders WHERE id=:id'), {'id': identifier}).one()
    assert source.economic_uid and source.org_id is None
    assert isinstance(source.economic_global_origin_nonce, str) and len(source.economic_global_origin_nonce) == 32
    origin = review_db.execute(text('SELECT origin_nonce,proof_kind,owner_scope,owner_key FROM cost_global_order_origins WHERE order_uid=:uid'), {'uid': source.economic_uid}).one()
    assert tuple(origin) == (source.economic_global_origin_nonce, 'verified_platform_admin_global_creation', 'global', 0)
    assert review_db.execute(text('SELECT count(*) FROM cost_global_origin_exclusions WHERE order_uid=:uid'), {'uid': source.economic_uid}).scalar_one() == 0


@pytest.mark.parametrize('payload', [{}, {'revenue': 0, 'labor_minutes': 0},
    {'revenue': 12.75, 'platform_fees': 0.25, 'order_date': '2026-10-13T11:30:00+02:00'}])
def test_global_origin_preserves_original_mapped_defaults(creators, review_db, payload):
    from modules.orders.routes.orders_crud import router
    from modules.orders.models import Order
    from modules.orders.schemas import OrderCreate
    client, auth, _, _ = creators; client.app.include_router(router, prefix='/api')
    data = OrderCreate.model_validate({'order_number': 'OWNED-DEFAULT-COMPARE', **payload})
    values = {name: getattr(data, name) for name in ('order_number','platform','customer_name','customer_email','order_date','notes','revenue','platform_fees','payment_fees','shipping_charged','shipping_cost')}
    reference = Order(**values, labor_minutes=data.labor_minutes or 0, org_id=None)
    review_db.add(reference); review_db.commit()
    expected = dict(review_db.execute(text('SELECT * FROM orders WHERE id=:id'), {'id': reference.id}).mappings().one())
    response = client.post('/api/orders', headers=auth('platform'), json={'order_number': 'OWNED-DEFAULT-COMPARE', **payload})
    assert response.status_code == 200, response.text
    actual = dict(review_db.execute(text('SELECT * FROM orders WHERE id=:id'), {'id': response.json()['id']}).mappings().one())
    for record in (actual, expected):
        for field in ('id', 'economic_uid', 'economic_global_origin_nonce'):
            record.pop(field)
        # Generated timestamps have the original server-default source, but
        # their instants can differ across separate INSERTs.
        assert record.pop('created_at') is not None
        assert record.pop('updated_at') is not None
    assert actual == expected
    assert 'economic_global_origin_nonce' not in Order.__table__.c


def _counts(db):
    return tuple(db.execute(text('SELECT count(*) FROM ' + table)).scalar_one()
                 for table in ('orders','order_items','cost_identities','cost_global_order_origins'))


@pytest.mark.parametrize('field', ['role', 'group_id'])
def test_current_actor_change_refuses_origin_and_rolls_back(creators, review_db, monkeypatch, field):
    from core import global_order_creation
    from modules.orders.routes.orders_crud import router
    client, auth, _, _ = creators; client.app.include_router(router, prefix='/api')
    before = _counts(review_db); review_db.commit()
    original = global_order_creation.create_attested_global_order
    def create(db, resource, request, actor):
        statement = "UPDATE users SET role='operator' WHERE id=71" if field == 'role' else 'UPDATE users SET group_id=1 WHERE id=71'
        db.execute(text(statement))
        return original(db, resource, request, actor)
    monkeypatch.setattr(global_order_creation, 'create_attested_global_order', create)
    response = client.post('/api/orders', headers=auth('platform'), json={'order_number': 'OWNED-ACTOR-DRIFT'})
    assert response.status_code == 403, response.text
    review_db.expire_all(); assert _counts(review_db) == before
    assert review_db.execute(text('SELECT role,group_id FROM users WHERE id=71')).one() == ('admin', None)


def test_origin_receipt_fault_rolls_back_fresh_source_and_registry(creators, review_db):
    from fastapi.testclient import TestClient
    from modules.orders.routes.orders_crud import router
    client, auth, _, _ = creators; client.app.include_router(router, prefix='/api')
    before = _counts(review_db); review_db.commit()
    review_db.execute(text("CREATE TRIGGER owned_origin_fault BEFORE INSERT ON cost_global_order_origins BEGIN SELECT RAISE(ABORT,'owned origin fault'); END")); review_db.commit()
    response = TestClient(client.app, raise_server_exceptions=False).post('/api/orders', headers=auth('platform'), json={'order_number':'OWNED-ORIGIN-FAULT'})
    assert response.status_code == 500
    review_db.expire_all(); assert _counts(review_db) == before


@pytest.mark.parametrize('scope, status', [('write', 200), ('read', 403)])
def test_actual_scoped_key_origin_requires_current_write_scope(creators, review_db, scope, status):
    from modules.orders.routes.orders_crud import router
    client, _, token, _ = creators; client.app.include_router(router, prefix='/api')
    headers = token([scope])
    review_db.execute(text('UPDATE api_tokens SET user_id=71 WHERE id=(SELECT max(id) FROM api_tokens)')); review_db.commit()
    response = client.post('/api/orders', headers=headers, json={'order_number':'OWNED-KEY-ORIGIN'})
    assert response.status_code == status, response.text
    if status == 200:
        assert review_db.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one() == 1


def test_genuine_tracked_login_cookie_origin_preserves_cookie_priority(creators, review_db):
    import secrets
    from core.auth import hash_password
    from modules.organizations.routes_auth import router as auth_router
    from modules.orders.routes.orders_crud import router
    client, auth, _, _ = creators
    client.app.include_router(auth_router, prefix='/api'); client.app.include_router(router, prefix='/api')
    password = secrets.token_urlsafe(24)
    review_db.execute(text('UPDATE users SET password_hash=:hash WHERE id=71'), {'hash': hash_password(password)}); review_db.commit()
    login = client.post('/api/auth/login', data={'username':'platform','password':password})
    assert login.status_code == 200
    assert review_db.execute(text('SELECT count(*) FROM active_sessions WHERE user_id=71')).scalar_one() == 1
    response = client.post('/api/orders', headers=auth('unassigned'), json={'order_number':'OWNED-TRACKED-COOKIE'})
    assert response.status_code == 200, response.text
    assert review_db.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one() == 1


def test_untracked_cookie_cannot_fall_back_to_valid_bearer_for_origin(creators, review_db):
    from core.auth import create_access_token
    from modules.orders.routes.orders_crud import router
    client, auth, _, _ = creators; client.app.include_router(router, prefix='/api')
    client.cookies.set('session', create_access_token({'sub':'platform'}))
    before = _counts(review_db); review_db.commit()
    response = client.post('/api/orders', headers=auth('platform'), json={'order_number':'OWNED-UNTRACKED-COOKIE'})
    assert response.status_code == 403, response.text
    review_db.expire_all(); assert _counts(review_db) == before


@pytest.mark.parametrize('change', ['delete', 'expire', 'scope'])
def test_selected_key_change_after_authentication_refuses_without_origin(creators, review_db, monkeypatch, change):
    from core import global_order_creation
    from modules.orders.routes.orders_crud import router
    client, _, token, _ = creators; client.app.include_router(router, prefix='/api')
    headers = token(['write'])
    review_db.execute(text('UPDATE api_tokens SET user_id=71 WHERE id=(SELECT max(id) FROM api_tokens)')); review_db.commit()
    token_id = review_db.execute(text('SELECT max(id) FROM api_tokens')).scalar_one()
    before = _counts(review_db); review_db.commit()
    original = global_order_creation.create_attested_global_order
    def create(db, resource, request, actor):
        statements = {'delete': 'DELETE FROM api_tokens WHERE id=:id',
                      'expire': "UPDATE api_tokens SET expires_at='2020-01-01' WHERE id=:id",
                      'scope': "UPDATE api_tokens SET scopes='[\"read\"]' WHERE id=:id"}
        db.execute(text(statements[change]), {'id': token_id})
        return original(db, resource, request, actor)
    monkeypatch.setattr(global_order_creation, 'create_attested_global_order', create)
    response = client.post('/api/orders', headers=headers, json={'order_number':'OWNED-KEY-CHANGE'})
    assert response.status_code == 403, response.text
    review_db.expire_all(); assert _counts(review_db) == before
    assert review_db.execute(text('SELECT scopes,expires_at FROM api_tokens WHERE id=:id'), {'id': token_id}).one() == ('["write"]', None)


def test_selected_jwt_revocation_after_authentication_refuses_and_rolls_back(creators, review_db, monkeypatch):
    import jwt
    from datetime import datetime, timezone
    from core import auth as auth_module, global_order_creation
    from modules.orders.routes.orders_crud import router
    client, auth, _, _ = creators; client.app.include_router(router, prefix='/api')
    headers = auth('platform')
    payload = jwt.decode(headers['Authorization'].partition(' ')[2], auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
    before = _counts(review_db); review_db.commit()
    original = global_order_creation.create_attested_global_order
    def create(db, resource, request, actor):
        db.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:expiry)'), {'jti': payload['jti'], 'expiry': datetime.fromtimestamp(payload['exp'], timezone.utc)})
        return original(db, resource, request, actor)
    monkeypatch.setattr(global_order_creation, 'create_attested_global_order', create)
    response = client.post('/api/orders', headers=headers, json={'order_number':'OWNED-JWT-REVOKED'})
    assert response.status_code == 403, response.text
    review_db.expire_all(); assert _counts(review_db) == before
    assert review_db.execute(text('SELECT count(*) FROM token_blacklist WHERE jti=:jti'), {'jti': payload['jti']}).scalar_one() == 0


def test_legacy_service_company_order_stays_unattested(creators, review_db, monkeypatch):
    import secrets
    from modules.orders.routes.orders_crud import router
    client, _, _, _ = creators; client.app.include_router(router, prefix='/api')
    key = secrets.token_urlsafe(24); monkeypatch.setenv('API_KEY', key)
    review_db.execute(text('UPDATE users SET is_active=0 WHERE id!=71')); review_db.commit()
    response = client.post('/api/orders', headers={'X-API-Key': key}, json={'order_number':'OWNED-SERVICE-COMPANY'})
    assert response.status_code == 200, response.text
    row = review_db.execute(text('SELECT economic_uid,economic_global_origin_nonce FROM orders WHERE id=:id'), {'id': response.json()['id']}).one()
    assert row.economic_uid and row.economic_global_origin_nonce is None
    assert review_db.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one() == 0


def test_deleted_company_order_numeric_reuse_gets_new_uid_and_origin(creators, review_db, monkeypatch):
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    from modules.orders.routes.orders_crud import router
    client, auth, _, _ = creators; client.app.include_router(router, prefix='/api')
    monkeypatch.setitem(registry._providers, 'OrderFulfillmentProvider', OrderFulfillmentService())
    first = client.post('/api/orders', headers=auth('platform'), json={'order_number':'OWNED-FIRST-LIFETIME'})
    assert first.status_code == 200, first.text
    identifier = first.json()['id']
    original = review_db.execute(text('SELECT economic_uid,economic_global_origin_nonce FROM orders WHERE id=:id'), {'id': identifier}).one()
    review_db.commit()
    assert client.delete(f'/api/orders/{identifier}', headers=auth('platform')).status_code == 204
    second = client.post('/api/orders', headers=auth('platform'), json={'order_number':'OWNED-SECOND-LIFETIME'})
    assert second.status_code == 200, second.text
    # This real SQLite INTEGER PRIMARY KEY fixture reuses the deleted highest ID.
    assert second.json()['id'] == identifier
    current = review_db.execute(text('SELECT economic_uid,economic_global_origin_nonce FROM orders WHERE id=:id'), {'id': identifier}).one()
    assert current.economic_uid != original.economic_uid
    assert current.economic_global_origin_nonce != original.economic_global_origin_nonce
    retained = review_db.execute(text('SELECT order_uid,origin_nonce FROM cost_global_order_origins')).all()
    assert set(retained) == {tuple(original), tuple(current)}


def test_existing_unattested_global_root_cannot_gain_origin_from_new_child(creators, review_db, monkeypatch):
    import secrets
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    from modules.orders.models import Product
    from modules.orders.routes.orders_crud import router
    client, auth, _, _ = creators; client.app.include_router(router, prefix='/api')
    monkeypatch.setitem(registry._providers, 'OrderFulfillmentProvider', OrderFulfillmentService())
    key = secrets.token_urlsafe(24); monkeypatch.setenv('API_KEY', key)
    review_db.execute(text('UPDATE users SET is_active=0 WHERE id!=71'))
    product = Product(name='Owned legacy child', price=0, org_id=None)
    review_db.add(product); review_db.commit()
    created = client.post('/api/orders', headers={'X-API-Key': key}, json={'order_number':'OWNED-LEGACY-ROOT'})
    assert created.status_code == 200, created.text
    identifier = created.json()['id']
    original = review_db.execute(text('SELECT economic_uid,economic_global_origin_nonce FROM orders WHERE id=:id'), {'id': identifier}).one()
    review_db.commit()
    added = client.post(f'/api/orders/{identifier}/items', headers=auth('platform'), json={'product_id':product.id,'quantity':1})
    assert added.status_code == 200, added.text
    assert review_db.execute(text('SELECT economic_uid,economic_global_origin_nonce FROM orders WHERE id=:id'), {'id': identifier}).one() == original
    assert original.economic_uid and original.economic_global_origin_nonce is None
    assert review_db.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one() == 0
