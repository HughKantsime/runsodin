"""Square connection policy, cutover attestations and explicit stock sends."""
from datetime import datetime, timezone
from typing import Literal

from modules.orders.production_auth import require_production_operator
from core.rbac import require_action
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, SecretStr, StrictBool, StrictInt, Field
from sqlalchemy import String, cast, delete
from sqlalchemy.orm import Session
import json

from core.db import get_db
from core.dependencies import log_audit
from core.models import SystemConfig
from core.rbac import require_role, require_interactive_session
from modules.orders import square_store as store
from modules.orders.square_client import SquareReader, identifier
# Capture the deployment key while application routes load at process startup.
from modules.orders import square_delivery_client  # noqa: F401

class SafeSquareRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def safe(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, 'Invalid Square request. Check the required fields.') from None
        return safe


router = APIRouter(prefix='/products', tags=['Square inventory'], route_class=SafeSquareRoute)
admin = require_role('admin', scope='write')
viewer = require_role('viewer', scope='read')
def strict_interactive(user=Depends(require_interactive_session())):
    store.assert_interactive(user)
    return user


interactive = Depends(strict_interactive)


class Connection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    access_token: SecretStr
    environment: Literal['sandbox', 'production']
    expected_revision: str | None = None


class Mapping(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str
    location_id: str
    variation_id: str


def connected(db, owner, revision=None):
    doc, previous = store.load(db, owner)
    if doc is None:
        raise HTTPException(409, 'Connect Square first')
    if revision is not None:
        store.check_revision(doc, revision)
    return doc, previous, SquareReader(doc['environment'], doc['token'])


def still_current(db, owner, doc):
    # Discard transaction snapshots before observing concurrent reconnects.
    db.rollback()
    latest, _ = store.load(db, owner)
    store.check_revision(latest, doc['revision'])
    return latest


@router.get('/square')
def status(user=Depends(viewer), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    doc, _ = store.load(db, owner)
    return store.public(db, owner, doc)


@router.put('/square', dependencies=[interactive, Depends(require_action('products.square_manage', scope='write'))])
def configure(data: Connection, user=Depends(admin), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    store.cipher()  # Do not make an external call if secure persistence is unavailable.
    old, _ = store.load(db, owner)
    store.check_revision(old, data.expected_revision)
    token = data.access_token.get_secret_value()
    if not token or len(token) > 4096 or not token.isascii() or any(not 33 <= ord(c) <= 126 for c in token):
        raise HTTPException(400, 'Enter a valid Square access token')
    merchant = SquareReader(data.environment, token).merchant()
    db.rollback()
    store.lock_owner(db, owner, user)
    latest, previous = store.load(db, owner)
    store.check_revision(latest, data.expected_revision)
    mappings = latest['mappings'] if latest and latest['merchant_id'] == merchant and latest['environment'] == data.environment else {}
    doc = store.save(db, owner, {'version': 1, 'tenant': owner, 'token': token, 'environment': data.environment,
                                  'merchant_id': merchant, 'mappings': mappings,
                                  'stock_writes_enabled': bool(latest and latest['merchant_id'] == merchant and latest['environment'] == data.environment and latest.get('stock_writes_enabled') is True)}, previous)
    log_audit(db, 'square.connection_saved', 'system_config', None, 'Read-only Square connection configured')
    db.commit()
    return store.public(db, owner, doc)


@router.delete('/square', dependencies=[interactive, Depends(require_action('products.square_manage', scope='write'))])
def disconnect(revision: str = Query(max_length=64), user=Depends(admin), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    store.lock_owner(db, owner, user)
    doc, previous, _ = connected(db, owner, revision)
    result = db.execute(delete(SystemConfig).where(SystemConfig.key == store.key(owner),
                        cast(SystemConfig.value, String) == json.dumps(previous)))
    if result.rowcount != 1:
        raise HTTPException(409, 'Square connection changed. Reload it before continuing.')
    log_audit(db, 'square.disconnected', 'system_config', None, 'Local Square connection and mappings removed')
    db.commit()
    return store.public(db, owner, None)


@router.get('/square/locations')
def locations(revision: str = Query(max_length=64), user=Depends(viewer), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    doc, _, reader = connected(db, owner, revision)
    values = reader.locations(doc['merchant_id'])
    still_current(db, owner, doc)
    return {'revision': revision, 'locations': values}


@router.get('/square/catalog')
def catalog(revision: str = Query(max_length=64), cursor: str | None = Query(default=None, max_length=4096),
            user=Depends(viewer), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    doc, _, reader = connected(db, owner, revision)
    result = reader.catalog(cursor)
    still_current(db, owner, doc)
    return {'revision': revision, **result}


@router.put('/{product_id}/square', dependencies=[interactive, Depends(require_action('products.square_manage', scope='write'))])
def map_product(product_id: int, data: Mapping, user=Depends(admin), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    store.own_product(db, owner, product_id)
    doc, _, reader = connected(db, owner, data.revision)
    location, variation = identifier(data.location_id), identifier(data.variation_id)
    if not any(v['id'] == location for v in reader.locations(doc['merchant_id'])):
        raise HTTPException(409, 'Choose an active Square location for this account')
    meta = reader.variation(variation, location)
    if not meta['tracked']:
        raise HTTPException(409, 'Enable inventory tracking for this variation at the selected Square location first')
    db.rollback()
    store.lock_owner(db, owner, user)
    store.own_product(db, owner, product_id, lock=True)
    latest, previous, _ = connected(db, owner, data.revision)
    for pid, value in latest['mappings'].items():
        if pid != str(product_id) and value['variation_id'] == variation and value['location_id'] == location:
            raise HTTPException(409, 'This Square variation and location already map to another ODIN product')
    if str(product_id) not in latest['mappings'] and len(latest['mappings']) >= 1000:
        raise HTTPException(409, 'This connection has reached its product mapping limit')
    old_mapping = latest['mappings'].get(str(product_id), {})
    baseline = old_mapping.get('stock_baseline') if (old_mapping.get('location_id'), old_mapping.get('variation_id')) == (location, variation) else None
    latest['mappings'][str(product_id)] = {'location_id': location, 'variation_id': variation,
                                          'item_name': meta['item_name'], 'variation_name': meta['variation_name']}
    if baseline:
        latest['mappings'][str(product_id)]['stock_baseline'] = baseline
    doc = store.save(db, owner, latest, previous)
    log_audit(db, 'square.product_mapped', 'product', product_id, 'Explicit Square variation/location selected')
    db.commit()
    return store.public(db, owner, doc)


@router.delete('/{product_id}/square', dependencies=[interactive, Depends(require_action('products.square_manage', scope='write'))])
def unmap_product(product_id: int, revision: str = Query(max_length=64), user=Depends(admin), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    store.lock_owner(db, owner, user)
    store.own_product(db, owner, product_id, lock=True)
    doc, previous, _ = connected(db, owner, revision)
    doc['mappings'].pop(str(product_id), None)
    doc = store.save(db, owner, doc, previous)
    log_audit(db, 'square.product_unmapped', 'product', product_id, 'Local Square mapping removed')
    db.commit()
    return store.public(db, owner, doc)


@router.get('/{product_id}/square')
def stock(product_id: int, revision: str = Query(max_length=64), user=Depends(viewer), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    store.own_product(db, owner, product_id)
    doc, _, reader = connected(db, owner, revision)
    mapping = doc['mappings'].get(str(product_id))
    if not mapping:
        raise HTTPException(409, 'Map this product to a Square variation and location first')
    if not any(v['id'] == mapping['location_id'] for v in reader.locations(doc['merchant_id'])):
        raise HTTPException(409, 'The mapped Square location is inactive or unavailable')
    meta = reader.variation(mapping['variation_id'], mapping['location_id'])
    result = reader.count(mapping['variation_id'], mapping['location_id']) if meta['tracked'] else {
        'quantity': None, 'calculated_at': None, 'state': 'untracked'}
    still_current(db, owner, doc)
    store.own_product(db, owner, product_id)
    return {'revision': revision, 'retrieved_at': datetime.now(timezone.utc).isoformat(), **result}


class Activation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str
    stock_writes_enabled: StrictBool


@router.put('/square/activation', dependencies=[interactive, Depends(require_action('products.square_manage', scope='write'))])
def activation(data: Activation, user=Depends(admin), db: Session = Depends(get_db)):
    owner = store.tenant(user)
    store.lock_owner(db, owner, user)
    doc, encoded, _ = connected(db, owner, data.revision)
    doc, encoded = store.fence(db, owner, doc, encoded)
    doc['stock_writes_enabled'] = data.stock_writes_enabled
    store.save(db, owner, doc, encoded)
    log_audit(db, 'square.stock_activation', 'system_config', None,
              {'owner': owner, 'attested_by': user['id'], 'enabled': data.stock_writes_enabled})
    db.commit()
    return store.public(db, owner, doc)


class BaselineInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str
    at: datetime
    quantity: StrictInt = Field(ge=0, le=1000000000)


class BaselineReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str
    answers: dict[str, Literal['counted', 'not_counted']]


@router.put('/{product_id}/square/baseline', dependencies=[interactive, Depends(require_action('products.square_manage', scope='write'))])
def baseline(product_id: int, data: BaselineInput, user=Depends(admin), db: Session = Depends(get_db)):
    from modules.orders.stock_baseline import set_baseline
    return set_baseline(db, user, product_id, data)


@router.post('/batches/additions/{addition_id}/baseline-review', dependencies=[interactive, Depends(require_action('products.square_manage', scope='write'))])
def baseline_review(addition_id: str, data: BaselineReview, user=Depends(admin), db: Session = Depends(get_db)):
    from modules.orders.stock_baseline import review
    return review(db, user, addition_id, data)


@router.post('/batches/additions/{addition_id}/send', dependencies=[Depends(require_production_operator()), Depends(require_action('products.square_send', scope='write:production'))])
def send_addition(addition_id: str, user=Depends(require_role('operator', scope='write:production')), db: Session = Depends(get_db)):
    from modules.orders.stock_recovery import execute_prepared
    return execute_prepared(db, user, addition_id)
