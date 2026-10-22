"""Retained evidence for one admitted dispatch; no deletion cascades."""
from sqlalchemy import Column,Integer,String,DateTime,CheckConstraint,UniqueConstraint
from sqlalchemy.sql import func
from core.base import Base


class DispatchAttemptReceipt(Base):
    __tablename__='dispatch_attempt_receipts'
    claim_token=Column(String(32),primary_key=True,nullable=False)
    printer_id=Column(Integer,nullable=False)
    job_id=Column(Integer,nullable=False)
    configuration_key=Column(String(64),nullable=False)
    prepared_power_key=Column(String(64),nullable=False)
    observed_filename_key=Column(String(64),nullable=False)
    observed_print_job_id=Column(Integer,nullable=True)
    created_at=Column(DateTime(timezone=True),nullable=False,server_default=func.now())
    __table_args__=(
        UniqueConstraint('observed_print_job_id',name='uq_dispatch_receipt_observation'),
        CheckConstraint('printer_id>0 AND job_id>0',name='ck_dispatch_receipt_ids'),
        CheckConstraint('observed_print_job_id IS NULL OR observed_print_job_id>0',name='ck_dispatch_receipt_observation'),
        CheckConstraint('length(claim_token)=32 AND length(configuration_key)=64 AND length(prepared_power_key)=64 AND length(observed_filename_key)=64',name='ck_dispatch_receipt_keys'),
    )
