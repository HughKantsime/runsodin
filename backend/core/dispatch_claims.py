"""Short committed dispatch-fence transactions; no age-based recovery or I/O."""
from dataclasses import dataclass,field
import re
import uuid
from sqlalchemy import insert,update,delete,select,text
from sqlalchemy.sql import func
from sqlalchemy.exc import IntegrityError
from core.dispatch_claim_schema import PrinterDispatchClaim

TABLE=PrinterDispatchClaim.__table__


@dataclass(frozen=True)
class ClaimAcquisition:
    status: str
    token: str = field(default=None,repr=False)


def acquire(engine,printer_id,job_id,physical_key,configuration_key,payload_sha256,owner_org_id=None, *, token=None):
    if (type(printer_id) is not int or printer_id<=0 or type(job_id) is not int or job_id<=0
            or owner_org_id is not None and (type(owner_org_id) is not int or owner_org_id<=0)
            or any(not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key) for key in (physical_key,configuration_key,payload_sha256))):
        return ClaimAcquisition('refused')
    token=token or uuid.uuid4().hex
    if not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token):
        return ClaimAcquisition("refused")
    try:
        with engine.begin() as conn:
            # Serialize a new preparation against reassignment/rescheduling.
            # This short transaction never acquires production parents later.
            # Adapter admission separately revalidates the full authority graph.
            statement = (
                'SELECT status,printer_id FROM jobs WHERE id=:id FOR UPDATE NOWAIT'
                if conn.dialect.name == 'postgresql' else
                'UPDATE jobs SET status=status WHERE id=:id RETURNING status,printer_id'
            )
            job = conn.execute(text(statement), {'id': job_id}).mappings().one_or_none()
            if job is None or job['status'] not in ('pending', 'scheduled') or job['printer_id'] != printer_id:
                return ClaimAcquisition('refused')
            conn.execute(insert(TABLE).values(printer_id=printer_id,job_id=job_id,physical_key=physical_key,
                configuration_key=configuration_key,payload_sha256=payload_sha256,owner_org_id=owner_org_id,state='preparing',token=token))
        return ClaimAcquisition('acquired',token)
    except IntegrityError:
        return ClaimAcquisition('conflict')
    except Exception:
        return ClaimAcquisition('uncertain')


def _change(engine,token,statement):
    if not isinstance(token,str) or not re.fullmatch('[0-9a-f]{32}',token):
        return False
    try:
        with engine.begin() as conn:
            result=conn.execute(statement.where(TABLE.c.token==token))
            changed=result.rowcount==1
        return changed
    except Exception:
        return False


def enter_adapter(engine,token):
    return _change(engine,token,update(TABLE).where(TABLE.c.state=='preparing').values(state='adapter_entered',updated_at=func.now()))


def release_preparing(engine,token):
    return _change(engine,token,delete(TABLE).where(TABLE.c.state=='preparing'))


def release_unstarted(engine, token):
    """Only for an owned adapter's confirmed gate refusal before START."""
    return _change(engine, token, delete(TABLE).where(TABLE.c.state == 'adapter_entered'))


def record_outcome(engine,token,status):
    if status not in ('accepted','uncertain'):
        return False
    return _change(engine,token,update(TABLE).where(TABLE.c.state=='adapter_entered').values(state=status,updated_at=func.now()))


# Closed set of (table, no-op lock column) pairs retire_terminal_locked locks.
_TERMINAL_LOCK_TARGETS=frozenset({('printers','is_active'),('print_jobs','status'),('jobs','status')})


def _lock_terminal_resource(conn,table,identifier,column):
    if (table,column) not in _TERMINAL_LOCK_TARGETS:
        raise ValueError('Unsupported terminal lock target')
    statement=(f'SELECT 1 FROM {table} WHERE id=:id FOR UPDATE NOWAIT'
               if conn.dialect.name=='postgresql' else
               f'UPDATE {table} SET {column}={column} WHERE id=:id RETURNING 1')
    if conn.execute(text(statement),{'id':identifier}).first() is None:  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/column checked against the _TERMINAL_LOCK_TARGETS allowlist; values are bound
        raise RuntimeError('Terminal resource unavailable')


def retire_terminal_locked(engine,*,printer_id,physical_key,configuration_key,owner_org_id,target_handle,key,expected_observation_id=None):
    """Retire one accepted, exactly observed terminal under target exclusion.

    This is normal next-dispatch reconciliation, not uncertain-send recovery.
    Historical receipts remain. An uncertain commit refuses this request even
    if deletion actually committed; a later request must inspect storage anew.
    """
    from datetime import datetime,timezone
    from sqlalchemy import text
    from core.order_production import prepare_job_write
    from core.dispatch_receipts import observed_filename_matches
    if (type(printer_id) is not int or printer_id<=0 or type(key) is not bytes or len(key)<32
            or any(type(v) is not str or re.fullmatch('[0-9a-f]{64}',v) is None for v in (physical_key,configuration_key))
            or owner_org_id is not None and (type(owner_org_id) is not int or owner_org_id<=0)):
        return 'uncertain'
    def stamp(value):
        if value is None:raise ValueError('Missing terminal timestamp')
        result=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    result='retained'
    try:
        if not target_handle.held():return 'uncertain'
        with engine.begin() as conn:
            if conn.dialect.name=='sqlite':conn.exec_driver_sql('BEGIN IMMEDIATE')
            elif conn.dialect.name!='postgresql':return 'uncertain'
            rows=conn.execute(text('SELECT * FROM printer_dispatch_claims WHERE printer_id=:pid OR physical_key=:physical'),{'pid':printer_id,'physical':physical_key}).mappings().all()
            if not rows:result='clear'
            elif len(rows)==1:
                peek=rows[0]
                if (peek['printer_id']!=printer_id or peek['physical_key']!=physical_key
                        or peek['configuration_key']!=configuration_key or peek['owner_org_id']!=owner_org_id
                        or peek['state']!='accepted'):
                    return 'retained'
                prepare_job_write(conn,[peek['job_id']])
                statement=select(TABLE).where(TABLE.c.token==peek['token'])
                if conn.dialect.name=='postgresql':statement=statement.with_for_update(nowait=True)
                claim=conn.execute(statement).mappings().one_or_none()
                identity_fields=('printer_id','job_id','physical_key','configuration_key','payload_sha256','token','owner_org_id','state')
                if claim is None or any(claim[k]!=peek[k] for k in identity_fields):return 'retained'
                _lock_terminal_resource(conn,'printers',printer_id,'is_active')
                from core.dispatch_receipt_schema import DispatchAttemptReceipt
                receipts=DispatchAttemptReceipt.__table__
                query=select(receipts).where(receipts.c.claim_token==claim['token'])
                if conn.dialect.name=='postgresql':query=query.with_for_update(nowait=True)
                receipt=conn.execute(query).mappings().one_or_none()
                if (receipt is None or receipt['printer_id']!=printer_id or receipt['job_id']!=claim['job_id']
                        or receipt['configuration_key']!=configuration_key or receipt['observed_print_job_id'] is None):
                    return 'retained'
                if expected_observation_id is not None and receipt['observed_print_job_id']!=expected_observation_id:return 'retained'
                _lock_terminal_resource(conn,'print_jobs',receipt['observed_print_job_id'],'status')
                _lock_terminal_resource(conn,'jobs',claim['job_id'],'status')
                observed=conn.execute(text('SELECT printer_id,scheduled_job_id,status,ended_at,started_at,filename,job_name FROM print_jobs WHERE id=:id'),{'id':receipt['observed_print_job_id']}).mappings().one()
                job=conn.execute(text('SELECT printer_id,status,actual_end,actual_start,payload_sha256 FROM jobs WHERE id=:id'),{'id':claim['job_id']}).mappings().one()
                if (observed['printer_id']!=printer_id or observed['scheduled_job_id']!=claim['job_id']
                        or job['printer_id']!=printer_id
                        or observed['status'] not in ('completed','cancelled','failed') or job['status']!=observed['status']
                        or job['payload_sha256'] is not None and job['payload_sha256']!=claim['payload_sha256']):
                    return 'retained'
                # Billed tenancy is not printer ownership. Exact terminal cleanup
                # does not authorize another job; new dispatch rechecks ownership.
                end=stamp(observed['ended_at'])
                if end!=stamp(job['actual_end']) or end<stamp(observed['started_at']) or end<stamp(job['actual_start']):return 'retained'
                protocol=conn.execute(text('SELECT api_type FROM printers WHERE id=:pid'),{'pid':printer_id}).scalar_one()
                name=observed['job_name'] if protocol=='bambu' else observed['filename']
                if not observed_filename_matches(key,claim['token'],protocol,name,receipt['observed_filename_key']):
                    return 'retained'
                from core.dispatch_sender_schema import DispatchSenderRegistration
                senders=DispatchSenderRegistration.__table__
                query=select(senders).where((senders.c.printer_id==printer_id)|(senders.c.physical_key==physical_key))
                if conn.dialect.name=='postgresql':query=query.with_for_update(nowait=True)
                if any(row['state']!='quiesced' for row in conn.execute(query).mappings()):return 'retained'
                if not target_handle.held():return 'uncertain'
                predicate=[TABLE.c[k].is_(None) if claim[k] is None else TABLE.c[k]==claim[k] for k in identity_fields]
                deleted=conn.execute(delete(TABLE).where(*predicate))
                if deleted.rowcount!=1:raise RuntimeError('Terminal claim changed')
                result='retired'
            if not target_handle.held():raise RuntimeError('Target lock changed')
        return result
    except Exception:
        return 'uncertain'


def retire_observed_terminal(engine,*,printer_id,print_job_id=None):
    """Best-effort exact committed terminal retirement outside monitor DB locks.

    Also called after dispatch closes its target handle, covering a terminal
    observation that arrived before the accepted outcome was committed.
    """
    from sqlalchemy import text
    from core.dispatch_target_lock import acquire_target_lock
    if (type(printer_id) is not int or printer_id<=0
            or print_job_id is not None and (type(print_job_id) is not int or print_job_id<=0)):
        return 'uncertain'
    handle=None;result='retained'
    try:
        with engine.connect() as conn:
            hint=conn.execute(text('SELECT c.physical_key,r.observed_print_job_id FROM printer_dispatch_claims c JOIN dispatch_attempt_receipts r ON r.claim_token=c.token AND r.printer_id=c.printer_id AND r.job_id=c.job_id WHERE c.printer_id=:pid'),{'pid':printer_id}).mappings().one_or_none()
        if hint is None or hint['observed_print_job_id'] is None:return 'retained'
        if print_job_id is not None and hint['observed_print_job_id']!=print_job_id:return 'retained'
        acquired=acquire_target_lock(engine,hint['physical_key'])
        if acquired.status!='acquired':return 'retained'
        handle=acquired.handle
        from core.auth import SECRET_KEY
        from core.dispatch_identity import build_identity
        from modules.printers import dispatch
        creds=dispatch._get_printer_info(printer_id)
        key=SECRET_KEY.encode() if isinstance(SECRET_KEY,str) else None
        identity=build_identity(creds,key)
        if identity is not None and identity.target_key==hint['physical_key'] and handle.held():
            result=retire_terminal_locked(engine,printer_id=printer_id,physical_key=identity.target_key,
                configuration_key=identity.configuration_key,owner_org_id=creds.get('org_id'),
                target_handle=handle,key=key,expected_observation_id=hint['observed_print_job_id'])
    except Exception:
        result='uncertain'
    finally:
        if handle is not None:
            try:
                if not handle.close().released:result='uncertain'
            except Exception:result='uncertain'
    return result
