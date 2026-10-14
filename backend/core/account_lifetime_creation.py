"""Private prospective account storage factory; caller authorization and outer transaction required."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime,timezone
from uuid import uuid4
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from core.account_lifetime_schema import AccountLifetimeError,BOUNDARY,connection,dialect,execute,one,bounded_timeout,physical,persistent_oids

@dataclass(frozen=True)
class CreatedAccount:
    id:int
    account_nonce:str

_SEAL=object()

class _Scope:
    def __init__(self,executor,seal):
        if seal is not _SEAL:raise AccountLifetimeError('Private account creation scope required')
        self.executor=executor;self.connection=connection(executor);self.alive=True;self.marker='account028_'+uuid4().hex
        self.driver=physical(self.connection);self.oids=persistent_oids(self.connection)
        self.root=self.connection.get_transaction() if isinstance(self.connection,Connection) else None
        self.session_root=executor.get_transaction() if isinstance(executor,Session) else None
        execute(self.connection,'SAVEPOINT '+self.marker)
    def check(self,executor):
        if not self.alive or executor is not self.executor:raise AccountLifetimeError('Account creation scope expired')
        try:
            c=connection(executor)
            if c is not self.connection or physical(c) is not self.driver or (isinstance(c,Connection) and c.get_transaction() is not self.root) or (isinstance(executor,Session) and executor.get_transaction() is not self.session_root):raise AccountLifetimeError('Account creation scope expired')
            if persistent_oids(c)!=self.oids:raise AccountLifetimeError('Account persistent relation changed')
            execute(c,'RELEASE SAVEPOINT '+self.marker)
            execute(c,'SAVEPOINT '+self.marker)
        except Exception as error:
            self.alive=False
            raise AccountLifetimeError('Account creation scope expired') from error
        return c

@contextmanager
def _account_creation_scope(executor):
    c=connection(executor)
    if dialect(c)=='postgresql':
        expected=persistent_oids(c)
        bounded_timeout(c)
        for table in BOUNDARY:execute(c,'LOCK TABLE public.'+table+' IN SHARE ROW EXCLUSIVE MODE NOWAIT')
        if persistent_oids(c)!=expected:raise AccountLifetimeError('Account persistent relation changed')
    else:
        # Caller already owns physical transaction; exact write upgrades its writer admission.
        execute(c,'UPDATE users SET id=id WHERE 0')
    scope=_Scope(executor,_SEAL)
    try:yield scope
    finally:
        if scope.connection is not None:
            try:execute(scope.connection,'RELEASE SAVEPOINT '+scope.marker)
            except Exception:pass # Uncertain/expired outer TX is never restarted or rolled back here.
        scope.alive=False;scope.executor=None;scope.connection=None;scope.root=None;scope.session_root=None;scope.marker=None;scope.driver=None;scope.oids=None

def _timestamp(value,pg):
    if not isinstance(value,datetime) or value.tzinfo is None or value.microsecond%1000:raise AccountLifetimeError('Canonical UTC millisecond timestamp required')
    value=value.astimezone(timezone.utc)
    return value if pg else value.isoformat(timespec='milliseconds').replace('+00:00','Z')

_FIELDS=frozenset(('id','username','password_hash','role','group_id','is_active','email','display_name'))
def _create_account(executor,scope,values,*,born_at):
    if not isinstance(scope,_Scope):raise AccountLifetimeError('Private account creation scope required')
    c=scope.check(executor)
    if type(values) is not dict or not values or set(values)-_FIELDS or not all(x in values for x in ('username','password_hash')):raise AccountLifetimeError('Unsupported prospective account fields')
    if 'id' in values and (type(values['id']) is not int or not 1<=values['id']<=2147483647):raise AccountLifetimeError('Invalid prospective account identifier')
    nonce=uuid4().hex;born=_timestamp(born_at,dialect(c)=='postgresql');marker='account_birth_'+uuid4().hex
    execute(c,'SAVEPOINT '+marker)
    try:
        names=tuple(values)+('account_nonce',)
        row=one(c,'INSERT INTO users('+','.join(names)+') VALUES('+','.join('?' for _ in names)+') RETURNING id',(*values.values(),nonce))
        uid=row[0]
        execute(c,"INSERT INTO account_lifetimes(account_nonce,birth_user_id,current_user_id,state,born_at) VALUES(?,?,?,'active',?)",(nonce,uid,uid,born))
        execute(c,'INSERT INTO account_bindings(account_nonce,user_id) VALUES(?,?)',(nonce,uid))
        if tuple(one(c,'SELECT u.id,l.account_nonce FROM users u JOIN account_bindings b ON b.user_id=u.id AND b.account_nonce=u.account_nonce JOIN account_lifetimes l ON l.account_nonce=b.account_nonce AND l.current_user_id=u.id WHERE u.id=?',(uid,)))!=(uid,nonce):raise AccountLifetimeError('Prospective account readback differs')
    except Exception:
        scope.alive=False
        execute(c,'ROLLBACK TO SAVEPOINT '+marker);execute(c,'RELEASE SAVEPOINT '+marker)
        raise
    execute(c,'RELEASE SAVEPOINT '+marker)
    return CreatedAccount(uid,nonce)
