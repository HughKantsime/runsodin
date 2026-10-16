"""Retained exact-attempt sender identities; no expiry or deletion cascades."""
from sqlalchemy import Column,Integer,String,DateTime,CheckConstraint
from sqlalchemy.sql import func
from core.base import Base


def _hex(column,size):
    expression=column
    for digit in '0123456789abcdef':
        expression="replace(%s,'%s','')"%(expression,digit)
    return "length(%s)=%s AND %s=''"%(column,size,expression)


class DispatchSenderRegistration(Base):
    __tablename__='dispatch_sender_registrations'
    claim_token=Column(String(32),primary_key=True,nullable=False)
    printer_id=Column(Integer,nullable=False)
    job_id=Column(Integer,nullable=False)
    physical_key=Column(String(64),nullable=False)
    configuration_key=Column(String(64),nullable=False)
    payload_sha256=Column(String(64),nullable=False)
    owner_org_id=Column(Integer,nullable=True)
    origin_key=Column(String(32),nullable=False)
    runtime_incarnation=Column(String(32),nullable=False)
    registration_key=Column(String(32),nullable=False)
    state=Column(String(24),nullable=False)
    created_at=Column(DateTime(timezone=True),nullable=False,server_default=func.now())
    updated_at=Column(DateTime(timezone=True),nullable=False,server_default=func.now())
    finished_at=Column(DateTime(timezone=True),nullable=True)
    cleanup_evidence_key=Column(String(64),nullable=True)
    __table_args__=(
        CheckConstraint('printer_id>0 AND job_id>0',name='ck_sender_ids'),
        CheckConstraint('owner_org_id IS NULL OR owner_org_id>0',name='ck_sender_owner'),
        CheckConstraint(' AND '.join(_hex(key,size) for key,size in (
            ('claim_token',32),('physical_key',64),('configuration_key',64),
            ('payload_sha256',64),('origin_key',32),('runtime_incarnation',32),
            ('registration_key',32))),name='ck_sender_keys'),
        CheckConstraint("(state='active' AND finished_at IS NULL AND cleanup_evidence_key IS NULL) OR "
            "(state='cleanup_uncertain' AND finished_at IS NOT NULL AND cleanup_evidence_key IS NULL) OR "
            "(state='quiesced' AND finished_at IS NOT NULL AND cleanup_evidence_key IS NOT NULL AND ("+
            _hex('cleanup_evidence_key',64)+"))",name='ck_sender_finish'),
    )
