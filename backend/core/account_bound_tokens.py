"""UNWIRED private token staging. No HTTP authority, enrollment or auth activation.

Caller owns authorization, physical root transaction, COMMIT/rollback and delivery.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import re
from core import account_lifetime_schema as s
from core.account_lifetime_creation import _account_creation_scope,_create_account
from core.account_lifetime_validation import _time

class AccountTokenError(s.AccountLifetimeError):pass
_SEAL=object();_EPOCH=datetime(1970,1,1,tzinfo=timezone.utc)
_RESERVED=frozenset(('sub','role','group_id','user_id','account_nonce','account_version','account_purpose','jti','iat','exp','issued_epoch_ms','expires_epoch_ms','ws','mfa_pending','mfa_setup_required'))

@dataclass(frozen=True)
class _AccountClock:
    epoch_ms:int
    instant:datetime
    observed_epoch_ms:int

@dataclass(frozen=True)
class _StagedToken:
    encoded:str
    account_nonce:str
    user_id:int
    purpose:str
    jti:str
    issued_epoch_ms:int
    expires_epoch_ms:int

class _Boundary:
    def __init__(self,executor,scope,seal,session_oids=None,sessions=False):
        if seal is not _SEAL:raise AccountTokenError('Private account token boundary required')
        self.executor=executor;self.scope=scope;self.alive=True;self.clock=None;self.tokens={};self.session_oids=session_oids;self.sessions=sessions;self.session_handles=set()
    def current(self,executor):
        if not self.alive or executor is not self.executor:raise AccountTokenError('Account token boundary expired')
        try:
            c=self.scope.check(executor)
            if self.sessions:
                from core.account_bound_session_schema import relation_oids
                if relation_oids(c)!=self.session_oids:raise AccountTokenError('Account session relation changed')
            return c
        except Exception:
            self.alive=False;raise AccountTokenError('Account token boundary expired') from None
    def poison(self):self.alive=False;self.scope.alive=False

@contextmanager
def _account_token_boundary(executor,*,sessions=False):
    # Inactive source foundation only. This boundary is NOT authenticated request authority.
    if type(sessions) is not bool:raise AccountTokenError('Exact session boundary mode required')
    session_oids=None
    if sessions:
        from core import account_bound_session_schema as session_schema
        from core.account_bound_session_validation import validate
        c=s.connection(executor)
        session_oids=session_schema.relation_oids(c)
        if s.dialect(c)=='postgresql':
            s.bounded_timeout(c)
            for table in session_schema.FENCES:session_schema.execute(c,'LOCK TABLE '+table+' IN SHARE ROW EXCLUSIVE MODE NOWAIT')
            if session_schema.relation_oids(c)!=session_oids:raise AccountTokenError('Account session relation changed')
        else:session_schema.execute(c,'UPDATE active_sessions SET id=id WHERE 0')
        validate(c)
    with _account_creation_scope(executor) as scope:
        boundary=_Boundary(executor,scope,_SEAL,session_oids,sessions)
        try:yield boundary
        finally:
            boundary.alive=False;boundary.executor=None;boundary.scope=None;boundary.clock=None;boundary.tokens.clear();boundary.session_handles.clear();boundary.session_oids=None

def _milliseconds(value):
    if type(value) is not datetime or value.tzinfo is None:raise AccountTokenError('Trusted UTC database clock required')
    delta=value.astimezone(timezone.utc)-_EPOCH
    return delta.days*86400000+delta.seconds*1000+delta.microseconds//1000

def _instant(value):
    if type(value) is not int or value%1000:raise AccountTokenError('Whole-second integer epoch milliseconds required')
    try:return _EPOCH+timedelta(milliseconds=value)
    except (OverflowError,ValueError):raise AccountTokenError('Token timestamp outside supported calendar') from None

def _now(c):
    if s.dialect(c)=='postgresql':return _milliseconds(s.one(c,'SELECT clock_timestamp()')[0])
    return _milliseconds(_time(s.one(c,"SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")[0]))

def capture_account_clock(executor,boundary):
    if type(boundary) is not _Boundary:raise AccountTokenError('Private account token boundary required')
    c=boundary.current(executor)
    if boundary.clock is not None:return boundary.clock
    observed=_now(c);aligned=observed-observed%1000
    clock=_AccountClock(aligned,_instant(aligned),observed);boundary.clock=clock
    return clock

def _create_fresh_bound_account(executor,boundary,values):
    clock=capture_account_clock(executor,boundary)
    return _create_account(executor,boundary.scope,values,born_at=clock.instant)

def _owner(c,nonce,identifier):
    if type(identifier) is not int or not 1<=identifier<=2147483647 or type(nonce) is not str or not re.fullmatch('[0-9a-f]{32}',nonce):raise AccountTokenError('Invalid account token owner')
    row=s.one(c,"SELECT u.username,u.role,u.group_id,l.born_at FROM users u JOIN account_bindings b ON b.user_id=u.id AND b.account_nonce=u.account_nonce JOIN account_lifetimes l ON l.account_nonce=b.account_nonce AND l.current_user_id=u.id WHERE u.id=? AND u.account_nonce=? AND u.is_active IS TRUE AND l.state='active'",(identifier,nonce))
    if row is None:raise AccountTokenError('Current active account binding required')
    if type(row[0]) is not str or not row[0] or row[1] not in ('viewer','operator','admin') or (row[2] is not None and (type(row[2]) is not int or row[2]<=0)):raise AccountTokenError('Current account metadata is invalid')
    return row

def _sign(payload):
    import jwt
    from core import auth
    return jwt.encode(payload,auth.SECRET_KEY,algorithm=auth.ALGORITHM)

def stage_account_token(executor,boundary,*,account_nonce,user_id,purpose,issued_epoch_ms,expires_epoch_ms,claims):
    if type(boundary) is not _Boundary:raise AccountTokenError('Private account token boundary required')
    c=boundary.current(executor)
    try:
        if purpose not in s.PURPOSES or type(purpose) is not str:raise AccountTokenError('Invalid account token purpose')
        clock=boundary.clock
        if clock is None or type(issued_epoch_ms) is not int or issued_epoch_ms!=clock.epoch_ms:raise AccountTokenError('Exact captured issuance clock required')
        issued=_instant(issued_epoch_ms);expires=_instant(expires_epoch_ms)
        if expires_epoch_ms<=issued_epoch_ms:raise AccountTokenError('Token expiry must follow issuance')
        now=_now(c)
        if now<clock.observed_epoch_ms or now-clock.observed_epoch_ms>2000 or expires_epoch_ms<=now:raise AccountTokenError('Account issuance clock expired')
        if type(claims) is not dict or set(claims)&_RESERVED or set(claims)-{'capability_snapshot_id'}:raise AccountTokenError('Reserved or unsupported account token claims')
        if purpose=='websocket':
            value=claims.get('capability_snapshot_id')
            if type(value) is not str or not value or len(value)>200 or not value.isascii():raise AccountTokenError('WebSocket capability snapshot required')
        elif claims:raise AccountTokenError('Claims do not match token purpose')
        username,role,org,born=_owner(c,account_nonce,user_id)
        if _time(born)>issued:raise AccountTokenError('Account birth follows issuance clock; retry with a fresh transaction')
        jti=uuid4().hex
        payload=dict(claims,sub=username,role=role,group_id=org,user_id=user_id,account_nonce=account_nonce,account_version=2,account_purpose=purpose,jti=jti,iat=issued_epoch_ms//1000,exp=expires_epoch_ms//1000,issued_epoch_ms=issued_epoch_ms,expires_epoch_ms=expires_epoch_ms)
        flags={'mfa_login':'mfa_pending','mfa_enrollment':'mfa_setup_required','websocket':'ws'}
        if purpose in flags:payload[flags[purpose]]=True
        encoded=_sign(payload)
        pg=s.dialect(c)=='postgresql'
        serialize=lambda t:t if pg else t.isoformat(timespec='milliseconds').replace('+00:00','Z')
        s.execute(c,'INSERT INTO account_authorities(jti,account_nonce,purpose,issued_at,expires_at) VALUES(?,?,?,?,?)',(jti,account_nonce,purpose,serialize(issued),serialize(expires)))
        row=s.one(c,'SELECT account_nonce,purpose,issued_at,expires_at FROM account_authorities WHERE jti=?',(jti,))
        if row is None or row[0:2]!=(account_nonce,purpose) or _time(row[2])!=issued or _time(row[3])!=expires:raise AccountTokenError('Staged authority readback differs')
        handle=_StagedToken(encoded,account_nonce,user_id,purpose,jti,issued_epoch_ms,expires_epoch_ms);boundary.tokens[jti]=handle
        return handle
    except Exception:
        boundary.poison();raise

def validate_staged_account_token(executor,boundary,staged,*,purpose):
    if type(boundary) is not _Boundary:raise AccountTokenError('Private account token boundary required')
    c=boundary.current(executor)
    if type(staged) is not _StagedToken or boundary.tokens.get(staged.jti) is not staged or staged.purpose!=purpose:raise AccountTokenError('Exact staged token handle required')
    _owner(c,staged.account_nonce,staged.user_id)
    if _now(c)>=staged.expires_epoch_ms:raise AccountTokenError('Staged token expired')
    row=s.one(c,'SELECT account_nonce,purpose,issued_at,expires_at FROM account_authorities WHERE jti=?',(staged.jti,))
    if row is None or row[0:2]!=(staged.account_nonce,purpose) or _milliseconds(_time(row[2]))!=staged.issued_epoch_ms or _milliseconds(_time(row[3]))!=staged.expires_epoch_ms:raise AccountTokenError('Staged token authority differs')
    return c

def stage_bound_session(executor,boundary,staged,*,ip,user_agent,enrollment_jti=None):
    if type(boundary) is not _Boundary:raise AccountTokenError('Private account token boundary required')
    try:
        c=validate_staged_account_token(executor,boundary,staged,purpose='access')
        from core import account_bound_session_schema as schema
        from core.account_bound_session_validation import validate
        validate(c)
        if not boundary.sessions:raise AccountTokenError('Session predicate boundary required')
        if staged.jti in boundary.session_handles:raise AccountTokenError('Staged session already consumed')
        if enrollment_jti is not None:raise AccountTokenError('Explicit nonce-bound enrollment schema required')
        if ip is not None and (type(ip) is not str or not ip.isascii() or len(ip)>45):raise AccountTokenError('Invalid session IP metadata')
        if user_agent is not None and type(user_agent) is not str:raise AccountTokenError('Invalid session agent metadata')
        if schema.one(c,'SELECT 1 FROM token_blacklist WHERE jti=?',(staged.jti,)) is not None:raise AccountTokenError('Staged token revoked')
        row=schema.one(c,'INSERT INTO active_sessions(user_id,token_jti,account_nonce,ip_address,user_agent) VALUES(?,?,?,?,?) RETURNING id',(staged.user_id,staged.jti,staged.account_nonce,ip,None if user_agent is None else user_agent[:500]))
        proof=schema.one(c,'SELECT id,user_id,token_jti,account_nonce FROM active_sessions WHERE id=?',(row[0],))
        if tuple(proof)!=(row[0],staged.user_id,staged.jti,staged.account_nonce):raise AccountTokenError('Staged session readback differs')
        boundary.session_handles.add(staged.jti)
        return row[0]
    except Exception as error:
        boundary.poison()
        if isinstance(error,s.AccountLifetimeError) and str(error)=='Account session table shape differs':raise AccountTokenError('Explicit nonce-bound session schema is not installed') from None
        raise
