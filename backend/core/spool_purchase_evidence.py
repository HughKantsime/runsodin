"""Owner-attested prospective evidence; no price estimation, commits or legacy adoption."""
from uuid import uuid4
from sqlalchemy import text
from fastapi import HTTPException
from core.spool_purchase_validation import PurchaseInput,MAX_REVISION


def _authorized(actor,scope,key):
    if not actor:return False
    group=actor.get('group_id')
    platform=actor.get('role')=='admin' and group is None
    return platform or (scope=='org' and type(group) is int and group>0 and group==key)


def _source(db,id,*,lock=False):
    if lock:
        connection=db.connection()
        if connection.dialect.name=='postgresql':
            from core.audit_actor import _bound_postgres_actor_locks
            _bound_postgres_actor_locks(connection)
            db.execute(text('SELECT id FROM spools WHERE id=:id FOR UPDATE'),{'id':id}).first()
        elif connection.dialect.name=='sqlite':
            if connection.connection.driver_connection.execute('PRAGMA foreign_keys').fetchone()[0]!=1:raise RuntimeError('Purchase foreign-key enforcement required')
            db.execute(text('UPDATE spools SET id=id WHERE id=:id'),{'id':id})
        else:raise RuntimeError('Unsupported purchase dialect')
    return db.execute(text('''SELECT s.id,s.org_id,s.economic_uid,s.economic_birth_nonce,s.purchase_evidence_nonce,s.purchase_revision,
        l.state,l.resource_kind,l.resource_id,l.identity_uid,l.owner_scope,l.owner_key,
        r.resource_kind AS registry_kind,r.resource_id AS registry_id,r.owner_scope AS registry_scope,r.owner_key AS registry_key,
        e.evidence_nonce,e.currency,e.price_decimal,e.net_weight_g_decimal,e.weight_basis,e.provenance,e.recorded_at,
        e.spool_uid AS evidence_uid,e.birth_nonce AS evidence_birth,e.revision AS evidence_revision,e.owner_scope AS evidence_scope,e.owner_key AS evidence_key
        FROM spools s LEFT JOIN cost_source_lifetimes l ON l.birth_nonce=s.economic_birth_nonce
        LEFT JOIN cost_identities r ON r.identity_uid=s.economic_uid LEFT JOIN spool_purchase_evidence e ON e.evidence_nonce=s.purchase_evidence_nonce WHERE s.id=:id'''),{'id':id}).mappings().one_or_none()


def _known(row):
    if not row or not row['economic_uid'] or not row['economic_birth_nonce']:return False
    scope,key=('global',0) if row['org_id'] is None else ('org',row['org_id'])
    if scope=='org' and (type(key) is not int or key<=0):return False
    return (row['state']=='known' and row['resource_kind']=='spool' and row['resource_id']==row['id'] and row['identity_uid']==row['economic_uid']
        and row['owner_scope']==scope and row['owner_key']==key and row['registry_kind']=='spool' and row['registry_id']==row['id'] and row['registry_scope']==scope and row['registry_key']==key)


def require_financial_owner(db,id,actor,*,legacy_allowed=False):
    from core.api_token_policy import token_scope_granted
    if not actor or actor.get('role') not in {'operator','admin'} or (actor.get('_auth_kind')=='user_api_token' and not token_scope_granted(actor.get('_token_scopes'),'write:spools')):
        raise HTTPException(403,'Spool purchase write permission required')
    row=_source(db,id,lock=True)
    if row is None:raise HTTPException(404,'Spool not found')
    if not _known(row):
        if legacy_allowed:
            from core.rbac import check_org_access
            if not check_org_access(actor,row['org_id']):raise HTTPException(404,'Spool not found')
            return row
        raise HTTPException(409,'Purchase evidence unavailable for this source lifetime')
    if not _authorized(actor,row['owner_scope'],row['owner_key']):raise HTTPException(404,'Spool purchase evidence not found')
    return row


def read_purchase(db,id,actor):
    row=_source(db,id)
    # Do not expose a revision/source/basis to shared or unproved readers.
    if not _known(row) or not _authorized(actor,row['owner_scope'],row['owner_key']):return {'state':'unavailable','can_write':False}
    revision=row['purchase_revision'] or 0
    writable=actor.get('role') in {'operator','admin'}
    if actor.get('_auth_kind')=='user_api_token':
        from core.api_token_policy import token_scope_granted
        writable=writable and token_scope_granted(actor.get('_token_scopes'), 'write:spools')
    if row['purchase_evidence_nonce'] is None:return {'state':'unknown','revision':revision,'can_write':writable}
    if (row['evidence_nonce']!=row['purchase_evidence_nonce'] or row['evidence_uid']!=row['economic_uid'] or row['evidence_birth']!=row['economic_birth_nonce'] or row['evidence_revision']!=revision or row['evidence_scope']!=row['owner_scope'] or row['evidence_key']!=row['owner_key']):
        raise RuntimeError('Purchase evidence read could not be confirmed')
    names=('evidence_nonce','currency','price_decimal','net_weight_g_decimal','weight_basis','provenance','recorded_at')
    return {'state':'recorded','revision':revision,'can_write':writable,**{n:row[n] for n in names}}



def write_purchase(db,id,actor,purchase,expected_revision,*,legacy_fields=None):
    if type(expected_revision) is not int or not 0<=expected_revision<MAX_REVISION:raise HTTPException(409,'Purchase revision exhausted or invalid')
    if purchase is not None and not isinstance(purchase,PurchaseInput):purchase=PurchaseInput.model_validate(purchase)
    row=require_financial_owner(db,id,actor)
    current=row['purchase_revision'] or 0
    if current!=expected_revision:raise HTTPException(409,'Purchase evidence changed; refresh and confirm again')
    # SAVEPOINT starts after the exact source lock; no outermost implicit commit.
    connection=db.connection()
    savepoint=connection.begin_nested()
    try:
        nonce=uuid4().hex if purchase is not None else None
        if purchase is not None:
            db.execute(text('''INSERT INTO spool_purchase_evidence(evidence_nonce,spool_uid,birth_nonce,owner_scope,owner_key,revision,currency,price_decimal,net_weight_g_decimal)
                VALUES(:nonce,:uid,:birth,:scope,:key,:revision,:currency,:price,:weight)'''),{'nonce':nonce,'uid':row['economic_uid'],'birth':row['economic_birth_nonce'],'scope':row['owner_scope'],'key':row['owner_key'],'revision':current+1,'currency':purchase.currency,'price':purchase.price,'weight':purchase.net_weight_g})
        updates={'purchase_evidence_nonce':nonce,'purchase_revision':current+1}
        if legacy_fields:
            if set(legacy_fields)-{'price','initial_weight_g'}:raise ValueError('Unsupported purchase legacy fields')
            updates.update(legacy_fields)
        assignments=','.join(name+'=:'+name for name in updates)
        result=db.execute(text('UPDATE spools SET '+assignments+' WHERE id=:id AND economic_uid=:uid AND economic_birth_nonce=:birth AND COALESCE(purchase_revision,0)=:expected'),{**updates,'id':id,'uid':row['economic_uid'],'birth':row['economic_birth_nonce'],'expected':current})  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- column names pass a fixed allowlist; values are bound
        if result.rowcount!=1:raise RuntimeError('Purchase transition could not be confirmed')
        savepoint.commit()
    except BaseException:
        savepoint.rollback();raise
    return current+1
