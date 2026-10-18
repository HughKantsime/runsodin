"""Request-local trusted actor snapshots and caller-transaction erasure fencing."""
from dataclasses import dataclass
import ipaddress
import weakref
from sqlalchemy import text

HUMAN_KINDS=frozenset(('session_jwt','bearer_jwt','user_api_token','password_login','mfa_login'))
_BINDING=object()
_SCOPE_KEY='odin.private.audit_actor_scope'

@dataclass(frozen=True)
class ActorSnapshot:
    user_id: int | None
    username: str | None
    auth_kind: str


def valid_snapshot(user_id, username, auth_kind):
    if auth_kind=='legacy_global_api_key':return user_id is None and username is None
    return (auth_kind in HUMAN_KINDS and type(user_id) is int and 1<=user_id<=2147483647
            and isinstance(username,str) and 1<=len(username)<=128 and not any(ord(c)<32 or ord(c)==127 for c in username))


def canonical_ip(value):
    try:
        if not isinstance(value,str) or '%' in value:return None
        parsed=str(ipaddress.ip_address(value))
        return parsed if len(parsed)<=45 else None
    except ValueError:return None

class RequestAuditScope:
    def __init__(self, peer):
        self.peer=canonical_ip(peer)
        self.live=True
        self.sessions=weakref.WeakSet()
    def register(self,db):
        if self.live:
            db.info[_BINDING]=(self,None)
            self.sessions.add(db)
    def clear(self):
        self.live=False
        for db in list(self.sessions):clear_session(db)
        self.sessions.clear()


def register_request_session(db,request):
    scope=request.scope.get(_SCOPE_KEY) if request is not None else None
    if type(scope) is RequestAuditScope:scope.register(db)


def clear_session(db):db.info.pop(_BINDING,None)


def clear_actor(db):
    value=db.info.get(_BINDING)
    if value is not None:db.info[_BINDING]=(value[0],None)


def bind_verified_actor(db,principal,kind=None):
    value=db.info.get(_BINDING)
    if value is None or not value[0].live or db not in value[0].sessions:return
    kind=kind or principal.get('_auth_kind')
    uid,name=(None,None) if kind=='legacy_global_api_key' else (principal.get('id'),principal.get('username'))
    db.info[_BINDING]=(value[0],ActorSnapshot(uid,name,kind) if valid_snapshot(uid,name,kind) else None)


def stage_actor(db,entry,explicit_ip=None):
    binding=db.info.get(_BINDING)
    live=binding is not None and binding[0].live and db in binding[0].sessions
    snapshot=binding[1] if live else None
    entry.ip_address=binding[0].peer if live else canonical_ip(explicit_ip)
    entry._odin_staged_actor=(weakref.ref(db),snapshot)



def _bound_postgres_actor_locks(connection):
    """At most2s per actor lock statement; preserve a stricter positive setting."""
    if connection.get_execution_options().get('isolation_level')=='AUTOCOMMIT' or getattr(connection.connection.driver_connection,'autocommit',False):
        raise RuntimeError('Audit actor locks require a caller transaction')
    setting,unit=connection.execute(text("SELECT setting,unit FROM pg_settings WHERE name='lock_timeout'")).one()
    if unit!='ms' or not isinstance(setting,str) or not setting.isascii() or not setting.isdecimal():
        raise RuntimeError('Audit actor lock timeout unavailable')
    current=int(setting)
    effective=current if 0<current<=2000 else 2000
    if effective!=current:
        actual=connection.execute(text("SELECT set_config('lock_timeout',:bound,true)"),{'bound':str(effective)+'ms'}).scalar_one()
        # set_config returns a presentation string; canonical setting is rechecked separately.
        saved,saved_unit=connection.execute(text("SELECT setting,unit FROM pg_settings WHERE name='lock_timeout'")).one()
        if saved!=str(effective) or saved_unit!='ms':raise RuntimeError('Audit actor lock timeout could not be confirmed')
    return effective

def _postgres_relations(connection):
    if connection.execute(text('SELECT current_schema()')).scalar_one()!='public':raise RuntimeError('Audit actor schema differs')
    for table in ('users','audit_logs','audit_actor_erasures'):
        oid,qualified=connection.execute(text('SELECT to_regclass(:t)::oid,to_regclass(:q)::oid'),{'t':table,'q':'public.'+table}).one()
        if oid is None or oid!=qualified:raise RuntimeError('Audit actor relation differs')
        if connection.execute(text('SELECT relkind,relpersistence FROM pg_class WHERE oid=:oid'),{'oid':oid}).one()!=('r','p'):raise RuntimeError('Audit actor relation differs')

def _erased(connection,uid):
    pg=connection.dialect.name=='postgresql'
    if pg:
        _bound_postgres_actor_locks(connection)
        _postgres_relations(connection)
        if connection.get_isolation_level().upper()!='READ COMMITTED':raise RuntimeError('Audit actor requires READ COMMITTED')
        found=connection.execute(text('SELECT id FROM users WHERE id=:uid FOR UPDATE'),{'uid':uid}).first()
        if found is None:connection.execute(text('LOCK TABLE audit_actor_erasures IN SHARE MODE'))
    elif connection.dialect.name!='sqlite':raise RuntimeError('Unsupported audit actor dialect')
    return connection.execute(text('SELECT 1 FROM audit_actor_erasures WHERE user_id=:uid'),{'uid':uid}).first() is not None


def stamp_actor(mapper,connection,entry):
    from sqlalchemy.orm import object_session
    staged=getattr(entry,'_odin_staged_actor',None)
    if staged is None:return
    origin,snapshot=staged
    same=origin() is object_session(entry)
    if snapshot is None and same:return  # no binding: compatible with pre024 schema
    suppressed=not same
    if snapshot is not None:
        if not valid_snapshot(snapshot.user_id,snapshot.username,snapshot.auth_kind):raise RuntimeError('Invalid staged audit actor')
        if same and snapshot.user_id is not None:suppressed=_erased(connection,snapshot.user_id)
    if suppressed:
        connection.execute(text('UPDATE audit_logs SET ip_address=NULL WHERE id=:id'),{'id':entry.id})
        # Cross-Session unknown needs no actor SQL on legacy schema.
        if not same:return
    result=connection.execute(text('UPDATE audit_logs SET actor_user_id=:uid,actor_username=:name,actor_auth_kind=:kind'+(',ip_address=NULL' if suppressed else '')+' WHERE id=:id AND actor_user_id IS NULL AND actor_username IS NULL AND actor_auth_kind IS NULL'),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only a constant ip_address clause is optionally appended; values are bound
        {'id':entry.id,'uid':None if suppressed else snapshot.user_id,'name':None if suppressed else snapshot.username,'kind':None if suppressed else snapshot.auth_kind})
    if result.rowcount!=1:raise RuntimeError('Audit actor stamp could not be confirmed')


def erase_actor_history(db,user_id):
    """Call after actual locked users UPDATE; caller owns the erasure transaction."""
    if type(user_id) is not int or not 1<=user_id<=2147483647:raise ValueError('Invalid erased user ID')
    connection=db.connection()
    if connection.dialect.name=='postgresql':
        _bound_postgres_actor_locks(connection)
        _postgres_relations(connection)
        if connection.get_isolation_level().upper()!='READ COMMITTED':raise RuntimeError('Audit erasure requires READ COMMITTED')
    db.execute(text('INSERT INTO audit_actor_erasures(user_id) VALUES (:uid) ON CONFLICT(user_id) DO NOTHING'),{'uid':user_id})
    if db.execute(text('SELECT 1 FROM audit_actor_erasures WHERE user_id=:uid'),{'uid':user_id}).first() is None:raise RuntimeError('Audit erasure marker could not be confirmed')
    db.execute(text('UPDATE audit_logs SET actor_user_id=NULL,actor_username=NULL,actor_auth_kind=NULL,ip_address=NULL WHERE actor_user_id=:uid'),{'uid':user_id})


def actor_is_erased(db,user_id):
    return db.execute(text('SELECT 1 FROM audit_actor_erasures WHERE user_id=:uid'),{'uid':user_id}).first() is not None
