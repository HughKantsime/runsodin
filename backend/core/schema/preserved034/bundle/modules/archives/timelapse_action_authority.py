"""Request-local media action evidence and short, insertion-safe DML fences.

No route activation. Evidence is never serializable or accepted from a principal.
"""
from dataclasses import dataclass,field
from datetime import datetime,timezone
import hashlib,time,os,hmac
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy import text
from sqlalchemy.engine import Engine,Connection
from core.dependencies import validate_access_token,_principal
from core.auth import verify_password
from core.api_token_policy import parse_token_scopes
from core.audit_actor import RequestAuditScope,register_request_session,bind_verified_actor
from modules.archives.timelapse_retirement import _floor,_refuse

def _as_utc_datetime(value):
    if value is None:return None
    try:
        result=datetime.fromisoformat(value) if isinstance(value,str) else value
        if not isinstance(result,datetime):return None
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError,TypeError):return None

_PROOF=object()
_SCOPE='odin.private.audit_actor_scope'
RELATIONS=tuple(sorted(('api_tokens','education_cost_center_grants','education_cost_centers',
 'education_submissions','education_upload_operations','jobs','models','print_files','printers',
 'timelapse_frame_bindings','timelapse_frame_namespaces','timelapse_media_lifetimes','timelapse_media_versions',
 'timelapse_ownership_anchors','timelapse_publication_facts','timelapse_publications',
 'timelapse_retained_versions','timelapses','token_blacklist','users')))
RETIREMENT_PROOF_RELATIONS=tuple(sorted(('timelapse_retirements','timelapse_retirement_objects',
 'timelapse_retirement_parents','timelapse_retirement_moves','timelapse_retirement_quarantine',
 'timelapse_retirement_unlinks','timelapse_retirement_lifetime_bindings',
 'timelapse_retirement_members','timelapse_retirement_container_receipts')))

def _retirement_catalog(c):
    values=','.join("('"+name+"')" for name in RETIREMENT_PROOF_RELATIONS)
    rows=c.execute(text("SELECT n.name,to_regclass(n.name)::oid,to_regclass('public.'||n.name)::oid,k.relkind,k.relpersistence FROM (VALUES "+values+") n(name) LEFT JOIN pg_class k ON k.oid=to_regclass('public.'||n.name) ORDER BY n.name COLLATE \"C\"")).all()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- VALUES list built from the RETIREMENT_PROOF_RELATIONS constants
    if (len(rows)!=len(RETIREMENT_PROOF_RELATIONS) or tuple(r[0] for r in rows)!=RETIREMENT_PROOF_RELATIONS
        or any(type(r[1]) is not int or r[1]<=0 or r[1]!=r[2] or (r[3],r[4])!=('r','p') for r in rows)):_refuse('unknown_authority')
    return tuple(tuple(r) for r in rows)

@dataclass(frozen=True,repr=False)
class ActionCredential:
    request:object=field(repr=False)
    scope:object=field(repr=False)
    kind:str=field(repr=False)
    user_id:int=field(repr=False)
    subject:str=field(repr=False)
    expiry:int|None=field(repr=False)
    jti:str|None=field(repr=False)
    token_id:int|None=field(repr=False)
    fingerprint:bytes|None=field(repr=False)
    database:object=field(repr=False)
    proof:object=field(repr=False)
    purpose:str=field(default="retire",repr=False)
    def __repr__(self):return '<request-local media credential>'


def capture_action_credential(request,db,credential,*,kind,carrier,purpose="retire",selected=None):
    """Revalidate the actual carrier; no secret survives this function.

    Caller supplies the exact selected request carrier after normal route auth.
    The route adapter must choose the same carrier, never fall back on failure.
    """
    scope=request.scope.get(_SCOPE) if request is not None else None
    if type(scope) is not RequestAuditScope or not scope.live:_refuse('unknown_authority')
    actual={'cookie':request.cookies.get('session'),'bearer':get_authorization_scheme_param(request.headers.get('authorization'))[1] if get_authorization_scheme_param(request.headers.get('authorization'))[0].lower()=='bearer' else '','query':request.query_params.get('token'),'api_key':request.headers.get('x-api-key')}.get(carrier)
    allowed={'session_jwt':{'cookie'},'bearer_jwt':{'bearer','query'},'user_api_token':{'api_key'},'legacy_global_api_key':{'api_key'}}
    if carrier not in allowed.get(kind,set()) or type(credential) is not str or not credential or actual!=credential:_refuse('unknown_authority')
    if purpose not in ('read','retire','edit'):_refuse('unknown_authority')
    if kind=='legacy_global_api_key':
        configured=os.getenv('API_KEY','')
        if not configured or not hmac.compare_digest(credential,configured):_refuse('unknown_authority')
        row=db.execute(text("SELECT * FROM users WHERE role='admin' AND is_active IS TRUE ORDER BY id LIMIT 1")).first()
        principal=_principal(row,kind)
        if principal is None:_refuse('unknown_authority')
        evidence=ActionCredential(request,scope,kind,principal['id'],principal['username'],None,None,None,hashlib.sha256(configured.encode()).digest(),db.get_bind(),_PROOF)
    elif kind in ('session_jwt','bearer_jwt'):
        principal=validate_access_token(credential,db)
        # decode_token's public shape is not trusted as a complete claim set.
        import jwt
        import core.auth as auth
        try:claims=jwt.decode(credential,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])
        except Exception:_refuse('unknown_authority')
        if principal is None or type(claims.get('exp')) is not int or type(claims.get('jti')) is not str or not claims['jti']:_refuse('unknown_authority')
        principal['_auth_kind']=kind
        evidence=ActionCredential(request,scope,kind,principal['id'],principal['username'],claims['exp'],claims['jti'],None,None,db.get_bind(),_PROOF)
    elif kind=='user_api_token':
        if not credential.startswith('odin_'):_refuse('unknown_authority')
        rows=db.execute(text('SELECT id,user_id,token_hash,scopes,expires_at FROM api_tokens WHERE token_prefix=:prefix LIMIT 16385'),{'prefix':credential[:10]}).mappings().all()
        if len(rows)>16384:_refuse('manifest_limit')
        matches=[]
        for row in rows:
            matched=verify_password(credential,row['token_hash'])
            if matched:matches.append(row)
        if len(matches)!=1:_refuse('unknown_authority')
        row=matches[0];expiry=_as_utc_datetime(row['expires_at'])
        if row['expires_at'] is not None and (expiry is None or expiry<=datetime.now(timezone.utc)):_refuse('unknown_authority')
        scopes=parse_token_scopes(row['scopes'])
        user=db.execute(text('SELECT * FROM users WHERE id=:id'),{'id':row['user_id']}).first()
        principal=_principal(user,kind,token_id=row['id'],token_scopes=scopes)
        if principal is None or scopes is None:_refuse('unknown_authority')
        evidence=ActionCredential(request,scope,kind,principal['id'],principal['username'],None,None,row['id'],hashlib.sha256(row['token_hash'].encode()).digest(),db.get_bind(),_PROOF)
    else:_refuse('unknown_authority')
    if selected is not None and any(principal.get(k)!=selected.get(k) for k in ('id','_auth_kind','_token_id')):_refuse('unknown_authority')
    from dataclasses import replace
    evidence=replace(evidence,purpose=purpose)
    _purpose_floor(principal,purpose)
    return evidence


def _purpose_floor(principal,purpose):
    if purpose=="retire":return _floor(principal)
    if purpose=="edit":
        from core.rbac import has_permission,token_scope_granted
        if not principal or type(principal.get("id")) is not int or principal["id"]<=0 or not has_permission(principal["role"],"operator"):_refuse("unknown_authority")
        scopes=principal.get("_token_scopes",[])
        if (scopes or principal.get("_auth_kind")=="user_api_token") and not token_scope_granted(scopes,"write:printers"):_refuse("unknown_authority")
        return
    from core.rbac import has_permission,token_scope_granted
    if not principal or type(principal.get("id")) is not int or principal["id"]<=0 or not has_permission(principal["role"],"viewer"):_refuse("unknown_authority")
    scopes=principal.get("_token_scopes",[])
    if (scopes or principal.get("_auth_kind")=="user_api_token") and not token_scope_granted(scopes,"read:printers"):_refuse("unknown_authority")

def fence_authorization(db,*,preparation=False,retirement_proofs=False):
    """Must run before any predicate/row lock in the owned action transaction."""
    if type(retirement_proofs) is not bool:_refuse('unknown_authority')
    c=db.connection()
    if c.dialect.name=='sqlite':
        if c.exec_driver_sql('PRAGMA foreign_keys').scalar_one()!=1:_refuse('unknown_authority')
        if getattr(c.connection.driver_connection,'in_transaction',False):_refuse('unknown_authority')
        c.exec_driver_sql('BEGIN IMMEDIATE')
    elif c.dialect.name=='postgresql':
        if c.get_isolation_level()!='READ COMMITTED' or c.execute(text('SELECT current_schema()')).scalar_one()!='public':_refuse('unknown_authority')
        before=_retirement_catalog(c) if retirement_proofs else None
        extra=set(RETIREMENT_PROOF_RELATIONS) if retirement_proofs else set()
        for relation in sorted(set(RELATIONS)|extra|({"audit_logs"} if preparation else set())):
            if relation not in extra:
                oid,qualified=c.execute(text('SELECT to_regclass(:r)::oid,to_regclass(:q)::oid'),{'r':relation,'q':'public.'+relation}).one()
                if oid is None or oid!=qualified or c.execute(text('SELECT relkind,relpersistence FROM pg_class WHERE oid=:id'),{'id':oid}).one()!=('r','p'):_refuse('unknown_authority')
            c.execute(text('LOCK TABLE public.'+relation+' IN SHARE MODE NOWAIT'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- relation comes from the RELATIONS constant set
        if retirement_proofs and _retirement_catalog(c)!=before:_refuse('unknown_authority')
    else:_refuse('unknown_authority')


_LEASE_KEY=object()
_LEASE_PROOF=object()

class _OwnedActionLease:
    def __init__(self,db,evidence,connection,proxy,driver,proof):
        if proof is not _LEASE_PROOF:_refuse('unknown_authority')
        self.db=db;self.evidence=evidence;self.connection=connection;self.proxy=proxy;self.driver=driver
        self.root=None;self.session_transaction=None;self.live=True

def _admit_action_lease(db,evidence,connection):
    if (type(evidence) is not ActionCredential or evidence.proof is not _PROOF
        or not isinstance(evidence.database,Engine) or not isinstance(connection,Connection)
        or connection.engine is not evidence.database or db.get_bind() is not connection
        or connection.closed or connection.invalidated or connection.in_transaction()
        or connection.in_nested_transaction() or db.in_transaction()
        or connection.get_execution_options().get('isolation_level')=='AUTOCOMMIT'):_refuse('unknown_authority')
    proxy=connection.connection;driver=proxy.driver_connection
    if getattr(driver,'autocommit',False):_refuse('unknown_authority')
    if connection.dialect.name=='sqlite':
        if driver.in_transaction or driver.isolation_level is None:_refuse('unknown_authority')
    elif connection.dialect.name=='postgresql':
        if int(driver.info.transaction_status)!=0:_refuse('unknown_authority')
    else:_refuse('unknown_authority')
    return _OwnedActionLease(db,evidence,connection,proxy,driver,_LEASE_PROOF)

def _mint_action_lease(lease):
    if type(lease) is not _OwnedActionLease or not lease.live or lease.root is not None:_refuse('unknown_authority')
    connection=lease.connection;db=lease.db
    root=connection.get_transaction();transaction=db.get_transaction()
    if (db.get_bind() is not connection or connection.closed or connection.invalidated
        or connection.connection is not lease.proxy or lease.proxy.driver_connection is not lease.driver
        or root is None or not root.is_active or transaction is None or not transaction.is_active
        or connection.in_nested_transaction() or db.in_nested_transaction()):_refuse('unknown_authority')
    lease.root=root;lease.session_transaction=transaction;db.info[_LEASE_KEY]=lease

def _expire_action_lease(db,lease):
    if type(lease) is _OwnedActionLease:lease.live=False
    if db.info.get(_LEASE_KEY) is lease:db.info.pop(_LEASE_KEY,None)

def _action_database_matches(db,evidence):
    bound=db.get_bind()
    if bound is evidence.database:return True
    lease=db.info.get(_LEASE_KEY)
    return (type(lease) is _OwnedActionLease and lease.live and lease.db is db and lease.evidence is evidence
        and isinstance(evidence.database,Engine) and lease.connection is bound and bound.engine is evidence.database
        and not bound.closed and not bound.invalidated and bound.connection is lease.proxy
        and lease.proxy.driver_connection is lease.driver and bound.get_transaction() is lease.root
        and lease.root is not None and lease.root.is_active
        and db.get_transaction() is lease.session_transaction and lease.session_transaction.is_active
        and not bound.in_nested_transaction() and not db.in_nested_transaction())

def fresh_action_principal(db,evidence):
    if type(evidence) is not ActionCredential or evidence.proof is not _PROOF or not _action_database_matches(db,evidence) or not evidence.scope.live or evidence.request.scope.get(_SCOPE) is not evidence.scope:_refuse('unknown_authority')
    row=db.execute(text('SELECT * FROM users WHERE id=:id'),{'id':evidence.user_id}).first()
    if row is None or not (row._mapping['is_active'] is True or (type(row._mapping['is_active']) is int and row._mapping['is_active']==1)):_refuse('unknown_authority')
    if evidence.kind=='legacy_global_api_key':
        configured=os.getenv('API_KEY','')
        if not configured or not hmac.compare_digest(hashlib.sha256(configured.encode()).digest(),evidence.fingerprint):_refuse('unknown_authority')
        first=db.execute(text("SELECT id FROM users WHERE role='admin' AND is_active IS TRUE ORDER BY id LIMIT 1")).scalar()
        if first!=evidence.user_id:_refuse('unknown_authority')
        principal=_principal(row,evidence.kind)
    elif evidence.kind in ('session_jwt','bearer_jwt'):
        if time.time()>=evidence.expiry or db.execute(text('SELECT 1 FROM token_blacklist WHERE jti=:jti'),{'jti':evidence.jti}).first():_refuse('unknown_authority')
        principal=_principal(row,evidence.kind)
        if principal is None or principal['username']!=evidence.subject:_refuse('unknown_authority')
    else:
        token=db.execute(text('SELECT user_id,token_hash,scopes,expires_at FROM api_tokens WHERE id=:id'),{'id':evidence.token_id}).mappings().first()
        if token is None or token['user_id']!=evidence.user_id or hashlib.sha256(token['token_hash'].encode()).digest()!=evidence.fingerprint:_refuse('unknown_authority')
        expiry=_as_utc_datetime(token['expires_at'])
        if token['expires_at'] is not None and (expiry is None or expiry<=datetime.now(timezone.utc)):_refuse('unknown_authority')
        scopes=parse_token_scopes(token['scopes'])
        if scopes is None:_refuse('unknown_authority')
        principal=_principal(row,evidence.kind,token_id=evidence.token_id,token_scopes=scopes)
    _purpose_floor(principal,evidence.purpose)
    if principal.get('group_id') is not None:
        from core.registry import registry
        from core.interfaces.education_policy import generic_archive_audience_sql
        provider=registry.get_provider('EducationPolicyProvider')
        audience=getattr(provider,'archive_audience_sql',None)
        if callable(audience):
            from core.education_archive_audience import archive_audience_sql
            if audience() not in (generic_archive_audience_sql(),archive_audience_sql()):_refuse('unknown_authority')
    register_request_session(db,evidence.request);bind_verified_actor(db,principal)
    return principal


def lock_media_sources(db,groups):
    """Exact selected019 rows, sorted table/primary key, after predicate fences."""
    from sqlalchemy import select,and_
    from core.timelapse_publication_schema import lifetimes,versions,publications,facts
    supported=(lifetimes,versions,publications,facts)
    for table,rows in sorted(groups,key=lambda pair:pair[0].name):
        if not any(table is candidate for candidate in supported):_refuse('unknown_authority')
        keys=tuple(table.primary_key.columns)
        if len(rows)>16384:_refuse('manifest_limit')
        seen=set()
        for expected in sorted(rows,key=lambda row:tuple(row[key.name] for key in keys)):
            identity=tuple(expected[key.name] for key in keys)
            if identity in seen:_refuse('phase_uncertain')
            seen.add(identity)
            query=select(table).where(and_(*(key==expected[key.name] for key in keys))).with_for_update(read=True,nowait=True)
            actual=db.execute(query).mappings().one_or_none()
            if actual is None or dict(actual)!=expected:_refuse('ownership_changed')
