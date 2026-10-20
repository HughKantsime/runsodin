"""Tenant-scoped encrypted connector configuration, not an inventory ledger."""
import json
import uuid

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import String, cast, delete, select, text, update
from sqlalchemy.exc import IntegrityError

from core.crypto import get_fernet
from core.models import SystemConfig
from modules.orders.models import Product

PREFIX = 'square_readonly:'


def tenant(user):
    group = user.get('group_id')
    if group is None:
        if user.get('role') != 'admin':
            raise HTTPException(403, 'An organization is required for Square access')
        return 0
    if type(group) is not int or group <= 0:
        raise HTTPException(403, 'Invalid Square organization')
    return group


def assert_interactive(user):
    if (not user or user.get('_auth_kind') not in ('jwt', 'session_jwt', 'bearer_jwt')
            or user.get('_token_scopes')):
        raise HTTPException(403, 'Interactive session access is required')


def key(owner):
    return PREFIX + str(owner)


def cipher():
    f = get_fernet()
    if f is None:
        raise HTTPException(503, 'Server encryption is unavailable. Ask an administrator to restore its encryption key.')
    return f


def load(db, owner):
    value = db.execute(select(SystemConfig.value).where(SystemConfig.key == key(owner))).scalar_one_or_none()
    if value is None:
        return None, None
    try:
        if not isinstance(value, str) or len(value) > 2_000_000:
            raise ValueError()
        doc = json.loads(cipher().decrypt(value.encode()))
        if (not isinstance(doc, dict) or doc.get('version') != 1 or doc.get('tenant') != owner
                or not isinstance(doc.get('mappings'), dict) or len(doc['mappings']) > 1000
                or not isinstance(doc.get('revision'), str) or not isinstance(doc.get('token'), str)
                or ('stock_writes_enabled' in doc and type(doc['stock_writes_enabled']) is not bool)
                or doc.get('environment') not in ('sandbox', 'production') or not isinstance(doc.get('merchant_id'), str)):
            raise ValueError()
        return doc, value
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        raise HTTPException(503, 'Square configuration cannot be decrypted or validated. Restore the correct server encryption key.') from None


def check_revision(doc, expected):
    actual = doc['revision'] if doc else None
    if actual != expected:
        raise HTTPException(409, 'Square connection changed. Reload it before continuing.')


def lock_owner(db, owner, user=None):
    if owner:
        result = db.execute(text('UPDATE groups SET id=id WHERE id=:id'), {'id': owner})
        if result.rowcount != 1:
            raise HTTPException(409, 'The organization no longer exists')
        if user and db.execute(text('SELECT group_id FROM users WHERE id=:id'), {'id': user['id']}).scalar_one_or_none() != owner:
            raise HTTPException(403, 'Organization membership changed. Sign in again.')


def fence(db, owner, doc, encoded):
    """Connector-row CAS lock, including global owner 0; caller holds until commit."""
    if db.execute(update(SystemConfig).where(SystemConfig.key == key(owner),
            cast(SystemConfig.value, String) == json.dumps(encoded))
            .values(value=SystemConfig.value).execution_options(synchronize_session=False)).rowcount != 1:
        raise HTTPException(409, 'Square connection changed. Reload it before continuing.')
    current, previous = load(db, owner)
    check_revision(current, doc['revision'])
    return current, previous


def writes_enabled(doc):
    from modules.orders.square_delivery_client import LIVE_WRITES_ENABLED
    return LIVE_WRITES_ENABLED and doc is not None and doc.get('stock_writes_enabled') is True


def own_product(db, owner, product_id, *, lock=False):
    clause = Product.org_id.is_(None) if owner == 0 else Product.org_id == owner
    if lock:
        result = db.execute(update(Product).where(Product.id == product_id, clause).values(id=Product.id))
        if result.rowcount != 1:
            raise HTTPException(404, 'Product not found in this Square organization')
    product = db.execute(select(Product).where(Product.id == product_id, clause).execution_options(populate_existing=True)).scalar_one_or_none()
    if product is None:
        raise HTTPException(404, 'Product not found in this Square organization')
    return product


def save(db, owner, doc, previous):
    doc['revision'] = uuid.uuid4().hex
    encoded = cipher().encrypt(json.dumps(doc, separators=(',', ':')).encode()).decode()
    try:
        if previous is None:
            db.add(SystemConfig(key=key(owner), value=encoded))
            db.flush()
        else:
            result = db.execute(update(SystemConfig).where(SystemConfig.key == key(owner),
                cast(SystemConfig.value, String) == json.dumps(previous)).values(value=encoded)
                .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise HTTPException(409, 'Square connection changed. Reload it before continuing.')
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Square connection changed. Reload it before continuing.') from None
    return doc


def public(db, owner, doc):
    if doc is None:
        return {'connected': False, 'revision': None, 'mappings': [], 'stock_writes_enabled': False, 'square_writes_enabled': False}
    result = []
    for pid, mapping in doc['mappings'].items():
        try:
            product = own_product(db, owner, int(pid))
        except (HTTPException, ValueError):
            continue
        result.append({'product_id': product.id, **mapping})
    return {'connected': True, 'revision': doc['revision'], 'environment': doc['environment'],
            'merchant_id': doc['merchant_id'], 'mappings': result,
            'stock_writes_enabled': doc.get('stock_writes_enabled') is True, 'square_writes_enabled': writes_enabled(doc)}


def forget_tenant(db, owner):
    """Caller holds the group-row lock shared with every connector writer."""
    from modules.orders.batch_production import assert_tenant_deletable, recipes
    assert_tenant_deletable(db, owner)
    db.execute(delete(recipes).where(recipes.c.owner == owner))
    from modules.orders.assortment import forget_tenant as forget_assortment
    forget_assortment(db,owner)
    db.execute(delete(SystemConfig).where(SystemConfig.key == key(owner)))


def forget_product(db, product):
    owner = product.org_id or 0
    lock_owner(db, owner)
    own_product(db, owner, product.id, lock=True)
    from modules.orders.batch_production import assert_product_deletable
    assert_product_deletable(db, product)
    doc, previous = load(db, owner)
    if doc and str(product.id) in doc['mappings']:
        del doc['mappings'][str(product.id)]
        save(db, owner, doc, previous)

    from modules.orders.assortment import forget_product as forget_assortment
    forget_assortment(db,product)
