"""Short exact-attempt registration only; no ARM or recovery authorization."""
from dataclasses import dataclass,field
import re
import uuid
from sqlalchemy import insert,select,update
from sqlalchemy.sql import func
from sqlalchemy.exc import IntegrityError
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.dispatch_sender_schema import DispatchSenderRegistration

TABLE=DispatchSenderRegistration.__table__
CLAIMS=PrinterDispatchClaim.__table__
IDENTITY=('printer_id','job_id','physical_key','configuration_key','payload_sha256','owner_org_id')


@dataclass(frozen=True)
class SenderRegistration:
    status: str
    registration_key: str = field(default=None,repr=False)


def _valid(values):
    return (all(type(values[k]) is int and values[k]>0 for k in ('printer_id','job_id'))
        and (values['owner_org_id'] is None or type(values['owner_org_id']) is int and values['owner_org_id']>0)
        and all(type(values[k]) is str and re.fullmatch('[0-9a-f]{%d}'%size,values[k]) for k,size in (
            ('claim_token',32),('physical_key',64),('configuration_key',64),('payload_sha256',64),
            ('origin_key',32),('runtime_incarnation',32))))


def _lock(conn,statement):
    if conn.dialect.name=='postgresql':
        return statement.with_for_update(nowait=True)
    if conn.dialect.name=='sqlite':
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        return statement
    raise RuntimeError('Unsupported sender authority storage')


def register(engine,*,claim_token,printer_id,job_id,physical_key,configuration_key,payload_sha256,
             owner_org_id,origin_key,runtime_incarnation):
    values=dict(claim_token=claim_token,printer_id=printer_id,job_id=job_id,physical_key=physical_key,
        configuration_key=configuration_key,payload_sha256=payload_sha256,owner_org_id=owner_org_id,
        origin_key=origin_key,runtime_incarnation=runtime_incarnation)
    if not _valid(values):
        return SenderRegistration('refused')
    registration_key=uuid.uuid4().hex
    try:
        with engine.begin() as conn:
            row=conn.execute(_lock(conn,select(CLAIMS).where(CLAIMS.c.token==claim_token))).mappings().one_or_none()
            if row is None or row['state']!='preparing' or any(row[k]!=values[k] for k in IDENTITY):
                return SenderRegistration('refused')
            # Historical unresolved senders remain fences even after claim loss.
            if conn.execute(select(TABLE.c.claim_token).where(TABLE.c.physical_key==physical_key,
                    TABLE.c.claim_token!=claim_token,TABLE.c.state.in_(('active','cleanup_uncertain')))).first():
                return SenderRegistration('refused')
            conn.execute(insert(TABLE).values(**values,registration_key=registration_key,state='active'))
        return SenderRegistration('registered',registration_key)
    except IntegrityError:
        return SenderRegistration('conflict')
    except Exception:
        return SenderRegistration('uncertain')


def finish_uncertain(engine,*,claim_token,printer_id,job_id,physical_key,configuration_key,payload_sha256,
                     owner_org_id,origin_key,runtime_incarnation,registration_key):
    values=dict(claim_token=claim_token,printer_id=printer_id,job_id=job_id,physical_key=physical_key,
        configuration_key=configuration_key,payload_sha256=payload_sha256,owner_org_id=owner_org_id,
        origin_key=origin_key,runtime_incarnation=runtime_incarnation)
    if not _valid(values) or type(registration_key) is not str or not re.fullmatch('[0-9a-f]{32}',registration_key):
        return False
    try:
        with engine.begin() as conn:
            predicate=[TABLE.c[k].is_(None) if value is None else TABLE.c[k]==value for k,value in values.items()]
            result=conn.execute(update(TABLE).where(*predicate,TABLE.c.registration_key==registration_key,
                TABLE.c.state=='active').values(state='cleanup_uncertain',finished_at=func.now(),updated_at=func.now()))
            changed=result.rowcount==1
        return changed
    except Exception:
        return False
