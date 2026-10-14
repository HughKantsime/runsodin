"""Frozen additive unlink admission/progress evidence; no filesystem action."""
from sqlalchemy import MetaData, Table, Column, String, Integer, CheckConstraint, ForeignKeyConstraint
from core.timelapse_retirement_schema import intent, objects

metadata = MetaData()
intent.to_metadata(metadata)
objects.to_metadata(metadata)
unlinks = Table('timelapse_retirement_unlinks', metadata,
    Column('operation_id', String(64), primary_key=True, nullable=False),
    Column('object_index', Integer, primary_key=True, nullable=False, autoincrement=False),
    Column('phase', String(24), nullable=False),
    ForeignKeyConstraint(['operation_id', 'object_index'],
        ['timelapse_retirement_objects.operation_id', 'timelapse_retirement_objects.object_index'], ondelete='RESTRICT'),
    CheckConstraint('length(operation_id) BETWEEN 1 AND 64 AND object_index>=0', name='tu_identity'),
    CheckConstraint("phase IN ('unlink_pending','unlink_confirmed','fsync_confirmed','retired')", name='tu_phase'),
)
TABLES = (unlinks,)
