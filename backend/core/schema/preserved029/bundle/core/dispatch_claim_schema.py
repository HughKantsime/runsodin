"""Durable dispatch fences retain history independently of live resource deletion."""
from sqlalchemy import Column,Integer,String,DateTime,CheckConstraint,UniqueConstraint
from sqlalchemy.sql import func
from core.base import Base


class PrinterDispatchClaim(Base):
    __tablename__='printer_dispatch_claims'
    printer_id=Column(Integer,primary_key=True,nullable=False,autoincrement=False)
    job_id=Column(Integer,nullable=False)
    physical_key=Column(String(64),nullable=False)
    configuration_key=Column(String(64),nullable=False)
    payload_sha256=Column(String(64),nullable=False)
    token=Column(String(32),nullable=False)
    owner_org_id=Column(Integer,nullable=True)
    state=Column(String(16),nullable=False)
    created_at=Column(DateTime,nullable=False,server_default=func.now())
    updated_at=Column(DateTime,nullable=False,server_default=func.now())
    __table_args__=(
        UniqueConstraint('job_id',name='uq_dispatch_claim_job'),
        UniqueConstraint('physical_key',name='uq_dispatch_claim_physical'),
        UniqueConstraint('token',name='uq_dispatch_claim_token'),
        CheckConstraint('printer_id>0 AND job_id>0',name='ck_dispatch_claim_ids'),
        CheckConstraint('owner_org_id IS NULL OR owner_org_id>0',name='ck_dispatch_claim_owner'),
        CheckConstraint("state='preparing' OR state='adapter_entered' OR state='accepted' OR state='uncertain'",name='ck_dispatch_claim_state'),
        CheckConstraint('length(physical_key)=64 AND length(configuration_key)=64 AND length(payload_sha256)=64 AND length(token)=32',name='ck_dispatch_claim_keys'),
    )
