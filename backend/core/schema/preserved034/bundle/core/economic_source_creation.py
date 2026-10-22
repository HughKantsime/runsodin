"""Private prospective source INSERT factories. Caller owns transaction/authorization.

No ORM events, legacy adoption, internal commit, numeric-ID recovery or DB fallback.
"""
from dataclasses import dataclass
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session


class SourceCreationError(RuntimeError):pass


@dataclass(frozen=True)
class CreatedSource:
    id:int
    birth_nonce:str
    identity_uid:str|None
    state:str


_UNKNOWN=object()
_SPOOL_FIELDS=frozenset(('filament_id','qr_code','rfid_tag','color_hex','initial_weight_g','remaining_weight_g','spool_weight_g','price','purchase_date','vendor','lot_number','storage_location','notes','pa_profile','low_stock_threshold_g','status','org_id','location_printer_id','location_slot','spoolman_spool_id'))
_ATTEMPT_FIELDS=frozenset(('printer_id','job_id','filename','job_name','started_at','status','total_layers','bed_temp_target','nozzle_temp_target','observed_received_at','scheduled_job_id'))


def _connection(executor):
    if isinstance(executor,Session):
        if not executor.in_transaction():raise SourceCreationError('Caller transaction required')
        executor=executor.connection()
    if isinstance(executor,Connection):
        if not executor.in_transaction():raise SourceCreationError('Caller transaction required')
        if executor.dialect.name=='sqlite':
            driver=executor.connection.driver_connection
            if driver.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
                raise SourceCreationError('SQLite foreign-key enforcement required')
            if not driver.in_transaction:executor.exec_driver_sql('BEGIN IMMEDIATE')
        return executor
    raw=getattr(executor,'_connection',executor)
    driver=getattr(raw,'driver_connection',raw)
    if hasattr(driver,'info'):
        active=int(driver.info.transaction_status) in (2,3)
    else:
        active=bool(getattr(driver,'in_transaction',False))
        if active and driver.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
            raise SourceCreationError('SQLite foreign-key enforcement required')
    if not active:raise SourceCreationError('Caller transaction required')
    return executor


def _execute(executor,statement,parameters=()):
    connection=_connection(executor)
    if isinstance(connection,Connection):
        if connection.dialect.name=='postgresql':statement=statement.replace('?', '%s') # Only fixed internal SQL templates reach this adapter.
        return connection.exec_driver_sql(statement,tuple(parameters))
    return connection.execute(statement,tuple(parameters))


def _one(executor,statement,parameters=()):return _execute(executor,statement,parameters).fetchone()


def _lock(executor,table,identifier):
    if _execute(executor,f'UPDATE {table} SET id=id WHERE id=?',(identifier,)).rowcount!=1:
        raise SourceCreationError('Economic parent no longer exists')


def _attempt_owner(executor,job_id):
    if job_id is None:return None
    if type(job_id) is not int or job_id<=0:raise SourceCreationError('Invalid attempt job')
    initial=_one(executor,'SELECT order_item_id FROM jobs WHERE id=?',(job_id,))
    if initial is None or initial[0] is None:return None
    item_id=initial[0]
    parent=_one(executor,'SELECT order_id FROM order_items WHERE id=?',(item_id,))
    if parent is None:return None
    order_id=parent[0]
    _lock(executor,'orders',order_id);_lock(executor,'order_items',item_id);_lock(executor,'jobs',job_id)
    row=_one(executor,'SELECT j.economic_uid,j.charged_to_org_id,j.order_item_id,i.economic_uid,i.order_id,o.economic_uid,o.org_id FROM jobs j JOIN order_items i ON i.id=j.order_item_id JOIN orders o ON o.id=i.order_id WHERE j.id=?',(job_id,))
    if row is None or row[2]!=item_id or row[4]!=order_id:raise SourceCreationError('Economic parent changed; rollback and retry')
    job_uid,org,_,item_uid,_,order_uid,root_org=row
    if not all((job_uid,item_uid,order_uid)):return None
    if org!=root_org:raise SourceCreationError('Economic root owner differs')
    owner=('global',0) if org is None else ('org',org)
    if owner[0]=='org' and (type(org) is not int or org<=0):raise SourceCreationError('Invalid economic owner')
    for uid,kind,identifier in ((job_uid,'job',job_id),(item_uid,'item',item_id),(order_uid,'order',order_id)):
        registry=_one(executor,'SELECT resource_kind,resource_id,owner_scope,owner_key FROM cost_identities WHERE identity_uid=?',(uid,))
        if registry is None or tuple(registry)!=(kind,identifier,*owner):raise SourceCreationError('Economic parent registry differs')
    binding=_one(executor,'SELECT order_uid,item_uid,owner_scope,owner_key FROM cost_job_bindings WHERE job_uid=?',(job_uid,))
    if binding is None or tuple(binding)!=(order_uid,item_uid,*owner):raise SourceCreationError('Economic parent binding differs')
    return (*owner,job_uid,order_uid,item_uid)


def _insert_inside(executor,table,values,owner):
    _connection(executor)
    allowed=_SPOOL_FIELDS if table=='spools' else _ATTEMPT_FIELDS
    if not isinstance(values,dict) or set(values)-allowed:raise SourceCreationError('Unsupported fresh source fields')
    if table=='print_jobs' and values.get('status')!='running':raise SourceCreationError('Fresh running attempt required')
    nonce=uuid4().hex
    names=list(values)+['economic_birth_nonce']
    row=_one(executor,f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)}) RETURNING id",(*values.values(),nonce))
    identifier=row[0]
    kind='spool' if table=='spools' else 'attempt'
    uid=str(uuid4()) if owner is not None else None
    scope=key=job_uid=order_uid=item_uid=attempt_uid=None
    if owner is not None:
        scope,key,*parents=owner
        _execute(executor,'INSERT INTO cost_identities(identity_uid,resource_kind,resource_id,owner_scope,owner_key) VALUES(?,?,?,?,?)',(uid,kind,identifier,scope,key))
        if kind=='attempt':
            job_uid,order_uid,item_uid=parents
            attempt_uid=uid
            _execute(executor,'INSERT INTO cost_attempt_bindings(attempt_uid,job_uid,order_uid,item_uid,owner_scope,owner_key) VALUES(?,?,?,?,?,?)',(uid,job_uid,order_uid,item_uid,scope,key))
    state='known' if uid else 'unresolved'
    _execute(executor,'INSERT INTO cost_source_lifetimes(birth_nonce,resource_kind,resource_id,state,identity_uid,owner_scope,owner_key,job_uid,order_uid,item_uid,attempt_uid) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(nonce,kind,identifier,state,uid,scope,key,job_uid,order_uid,item_uid,attempt_uid))
    if uid:
        if _execute(executor,f'UPDATE {table} SET economic_uid=? WHERE id=? AND economic_uid IS NULL',(uid,identifier)).rowcount!=1:
            raise SourceCreationError('Economic source assignment lost race')
    return CreatedSource(identifier,nonce,uid,state)


def _insert(executor,table,values,owner):
    # A failed factory cannot leave half evidence even if its caller catches
    # the exception and commits unrelated work. Savepoints never commit.
    savepoint='economic_birth_'+uuid4().hex
    _execute(executor,'SAVEPOINT '+savepoint)
    try:
        result=_insert_inside(executor,table,values,owner)
    except BaseException:
        _execute(executor,'ROLLBACK TO SAVEPOINT '+savepoint)
        _execute(executor,'RELEASE SAVEPOINT '+savepoint)
        raise
    _execute(executor,'RELEASE SAVEPOINT '+savepoint)
    return result


def _create_spool(executor,values,*,owner_org_id=_UNKNOWN,global_authority=False):
    owner=None
    if owner_org_id is not _UNKNOWN:
        actual=values.get('org_id')
        if owner_org_id is None:
            if not global_authority or actual is not None:raise SourceCreationError('Explicit global source authority required')
            owner=('global',0)
        elif type(owner_org_id) is int and owner_org_id>0 and actual==owner_org_id:
            owner=('org',owner_org_id)
        else:raise SourceCreationError('Source owner differs')
    return _insert(executor,'spools',values,owner)


def _create_attempt(executor,values):
    # Parent locks precede INSERT/attempt locks, in both database styles.
    owner=_attempt_owner(executor,values.get('scheduled_job_id'))
    return _insert(executor,'print_jobs',values,owner)


def _validate_existing_source(executor,kind,identifier):
    table={'spool':'spools','attempt':'print_jobs'}.get(kind)
    if table is None:raise SourceCreationError('Unknown economic source kind')
    _lock(executor,table,identifier)
    row=_one(executor,f'SELECT economic_birth_nonce,economic_uid FROM {table} WHERE id=?',(identifier,))
    if row[0] is None:raise SourceCreationError('Legacy source has no birth')
    lifetime=_one(executor,'SELECT resource_kind,resource_id,state,identity_uid FROM cost_source_lifetimes WHERE birth_nonce=?',(row[0],))
    if lifetime is None or tuple(lifetime[:2])!=(kind,identifier) or lifetime[3]!=row[1]:raise SourceCreationError('Economic source lifetime differs')
    return CreatedSource(identifier,row[0],row[1],lifetime[2])
