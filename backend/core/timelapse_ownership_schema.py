"""Immutable capture-time media ownership contract, not current-printer authority."""
from sqlalchemy import (MetaData,Table,Column,String,Text,Integer,Boolean,DateTime,
                        CheckConstraint,func)
metadata=MetaData()
anchors=Table('timelapse_ownership_anchors',metadata,
    Column('operation_id',String(64),primary_key=True,nullable=False),
    Column('timelapse_id',Integer,nullable=False),
    Column('printer_id',Integer,nullable=False),
    Column('scheduled_job_id',Integer),
    Column('filename',Text,nullable=False),
    Column('owner_state',String(16),nullable=False),
    Column('owner_org_id',Integer),
    Column('ownership_source',String(32),nullable=False),
    Column('print_file_id',Integer),
    Column('education_submission_id',Integer),
    Column('capture_printer_org_id',Integer),
    Column('capture_printer_shared',Boolean,nullable=False),
    Column('created_at',DateTime(timezone=True),nullable=False,server_default=func.current_timestamp()),
    CheckConstraint('capture_printer_shared IN (TRUE,FALSE)',name='to_sharing'),
    CheckConstraint('length(operation_id) BETWEEN 1 AND 64 AND timelapse_id > 0 AND printer_id > 0 AND length(filename) BETWEEN 1 AND 4096',name='to_identity'),
    CheckConstraint('(scheduled_job_id IS NULL OR scheduled_job_id > 0) AND (print_file_id IS NULL OR print_file_id > 0) AND (education_submission_id IS NULL OR education_submission_id > 0 OR education_submission_id=-1) AND (capture_printer_org_id IS NULL OR capture_printer_org_id > 0)',name='to_references'),
    CheckConstraint("(owner_state='known' AND owner_org_id IS NOT NULL AND owner_org_id > 0 AND ((capture_printer_org_id IS NOT NULL AND capture_printer_org_id=owner_org_id) OR capture_printer_shared IS TRUE)) OR (owner_state='shared' AND owner_org_id IS NULL AND capture_printer_shared IS TRUE AND education_submission_id IS NULL) OR (owner_state='unresolved' AND owner_org_id IS NULL)",name='to_owner'),
    CheckConstraint("(owner_state IN ('known','shared') AND ownership_source='first_capture_job_file' AND scheduled_job_id IS NOT NULL AND print_file_id IS NOT NULL AND (education_submission_id IS NULL OR education_submission_id > 0)) OR (owner_state='unresolved' AND ownership_source='capture_unresolved')",name='to_provenance'),
)
