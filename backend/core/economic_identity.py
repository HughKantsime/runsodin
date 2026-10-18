"""Prospective identities, exclusively in the creator's transaction.

The private capability attests sanctioned fresh creation, not authorization for
an arbitrary caller or the lifetime safety of imported database rows.
"""
from dataclasses import dataclass
from uuid import uuid4
from sqlalchemy import inspect, text


class EconomicIdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class _FreshCreation:
    resource: object
    kind: str
    owner: tuple


_TABLES = {'order': 'orders', 'item': 'order_items', 'job': 'jobs'}


def _capture_fresh(resource, kind, *, owner_org_id, global_authority=False):
    if kind not in _TABLES or inspect(resource).mapper.local_table.name != _TABLES[kind] or not inspect(resource).transient or resource.id is not None:
        raise EconomicIdentityError('Fresh transient creation required')
    if owner_org_id is None:
        if not global_authority:
            raise EconomicIdentityError('Explicit global creation authority required')
        owner = ('global', 0)
    elif type(owner_org_id) is int and owner_org_id > 0:
        owner = ('org', owner_org_id)
    else:
        raise EconomicIdentityError('Invalid economic owner')
    return _FreshCreation(resource, kind, owner)


def _row(db, table, identifier):
    row = db.execute(text(f'SELECT * FROM {table} WHERE id=:id'), {'id': identifier}).mappings().one_or_none()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table comes from the fixed _TABLES map or literal callers; values are bound
    if row is None:
        raise EconomicIdentityError('Economic source no longer exists')
    return row


def _lock(db, table, identifier):
    if db.execute(text(f'UPDATE {table} SET id=id WHERE id=:id'), {'id': identifier}).rowcount != 1:  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table comes from the fixed _TABLES map or literal callers; values are bound
        raise EconomicIdentityError('Economic source no longer exists')
    return _row(db, table, identifier)


def _owner(org):
    return ('global', 0) if org is None else ('org', org)


def _validate(db, kind, row, owner, order=None, item=None):
    uid = row['economic_uid']
    registry = db.execute(text('SELECT resource_kind,resource_id,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:uid'), {'uid': uid}).one_or_none()
    if registry is None or tuple(registry) != (kind, row['id'], *owner):
        raise EconomicIdentityError('Economic registry association differs')
    if order is not None:
        membership = db.execute(text('SELECT order_uid,owner_scope,owner_key FROM cost_order_memberships WHERE child_uid=:uid AND child_kind=:kind'), {'uid': uid, 'kind': kind}).one_or_none()
        if membership is None or tuple(membership) != (order['economic_uid'], *owner):
            raise EconomicIdentityError('Economic membership differs')
    if item is not None:
        binding = db.execute(text('SELECT order_uid,item_uid,owner_scope,owner_key FROM cost_job_bindings WHERE job_uid=:uid'), {'uid': uid}).one_or_none()
        if binding is None or tuple(binding) != (order['economic_uid'], item['economic_uid'], *owner):
            raise EconomicIdentityError('Economic job binding differs')
    elif kind == 'job' and (db.execute(text('SELECT 1 FROM cost_job_bindings WHERE job_uid=:uid'), {'uid': uid}).first() or db.execute(text('SELECT 1 FROM cost_order_memberships WHERE child_uid=:uid'), {'uid':uid}).first()):
        raise EconomicIdentityError('Unlinked job has economic binding')
    return uid


def _allocate_created(db, capability):
    if not isinstance(capability, _FreshCreation) or inspect(capability.resource).session is not db or inspect(capability.resource).deleted:
        raise EconomicIdentityError('Creator session capability required')
    db.flush()
    kind, identifier, owner = capability.kind, capability.resource.id, capability.owner
    table = _TABLES[kind]
    initial = _row(db, table, identifier)
    order = item = None
    if kind == 'item':
        order = _lock(db, 'orders', initial['order_id'])
    elif kind == 'job' and initial['order_item_id'] is not None:
        peek = _row(db, 'order_items', initial['order_item_id'])
        order = _lock(db, 'orders', peek['order_id'])
        item = _lock(db, 'order_items', initial['order_item_id'])
        if item['order_id'] != order['id']:
            raise EconomicIdentityError('Parent changed; rollback and retry')
    row = _lock(db, table, identifier)
    if kind == 'item' and row['order_id'] != order['id'] or kind == 'job' and row['order_item_id'] != initial['order_item_id']:
        raise EconomicIdentityError('Parent changed; rollback and retry')
    if kind == 'order':
        if _owner(row['org_id']) != owner:
            raise EconomicIdentityError('Order owner differs')
    if order is not None:
        if _owner(order['org_id']) != owner:
            raise EconomicIdentityError('Root owner differs')
        if order['economic_uid'] is None:
            # Legacy roots do not acquire historical identities by child creation.
            return None
        _validate(db, 'order', order, owner)
        if item is not None:
            if item['economic_uid'] is None:
                return None
            _validate(db, 'item', item, owner, order)
    if kind == 'job' and (_owner(row['charged_to_org_id']) != owner or row['charged_to_org_id'] is None and owner != ('global', 0)):
        raise EconomicIdentityError('Job billed owner differs')
    if row['economic_uid'] is not None:
        return _validate(db, kind, row, owner, order, item)
    uid = str(uuid4())
    db.execute(text('INSERT INTO cost_identities(identity_uid,resource_kind,resource_id,owner_scope,owner_key) VALUES(:uid,:kind,:id,:scope,:owner)'), {'uid': uid, 'kind': kind, 'id': identifier, 'scope': owner[0], 'owner': owner[1]})
    if order is not None:
        db.execute(text('INSERT INTO cost_order_memberships(child_uid,child_kind,order_uid,owner_scope,owner_key) VALUES(:uid,:kind,:order,:scope,:owner)'), {'uid': uid, 'kind': kind, 'order': order['economic_uid'], 'scope': owner[0], 'owner': owner[1]})
    if item is not None:
        db.execute(text('INSERT INTO cost_job_bindings(job_uid,order_uid,item_uid,owner_scope,owner_key) VALUES(:uid,:order,:item,:scope,:owner)'), {'uid': uid, 'order': order['economic_uid'], 'item': item['economic_uid'], 'scope': owner[0], 'owner': owner[1]})
    if db.execute(text(f'UPDATE {table} SET economic_uid=:uid WHERE id=:id AND economic_uid IS NULL'), {'uid': uid, 'id': identifier}).rowcount != 1:  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table comes from the fixed _TABLES map or literal callers; values are bound
        raise EconomicIdentityError('Economic allocation lost race; rollback required')
    return uid


def _add_created(db, resource, kind, *, owner_org_id, global_authority=False):
    capability = _capture_fresh(resource, kind, owner_org_id=owner_org_id, global_authority=global_authority)
    db.add(resource)
    _allocate_created(db, capability)
    return resource


def _validate_existing(db, kind, identifier, *, owner_org_id, global_authority=False):
    """Locked replay only. A missing UID never grants permission to allocate."""
    if kind not in _TABLES:
        raise EconomicIdentityError('Unknown economic kind')
    if owner_org_id is None and not global_authority:
        raise EconomicIdentityError('Explicit global authority required')
    owner = _owner(owner_org_id)
    initial = _row(db, _TABLES[kind], identifier)
    order = item = None
    if kind == 'item':
        order = _lock(db, 'orders', initial['order_id'])
    elif kind == 'job' and initial['order_item_id'] is not None:
        peek = _row(db, 'order_items', initial['order_item_id'])
        order = _lock(db, 'orders', peek['order_id'])
        item = _lock(db, 'order_items', peek['id'])
        if item['order_id'] != order['id']:
            raise EconomicIdentityError('Parent changed; rollback and retry')
    row = _lock(db, _TABLES[kind], identifier)
    if kind == 'item' and row['order_id'] != initial['order_id'] or kind == 'job' and row['order_item_id'] != initial['order_item_id']:
        raise EconomicIdentityError('Parent changed; rollback and retry')
    if kind == 'order' and _owner(row['org_id']) != owner or kind == 'job' and _owner(row['charged_to_org_id']) != owner:
        raise EconomicIdentityError('Economic owner differs')
    if row['economic_uid'] is None:
        raise EconomicIdentityError('Legacy source has no prospective identity')
    if order is not None:
        if _owner(order['org_id']) != owner or order['economic_uid'] is None:
            raise EconomicIdentityError('Economic root differs')
        _validate(db, 'order', order, owner)
        if item is not None:
            _validate(db, 'item', item, owner, order)
    return _validate(db, kind, row, owner, order, item)
