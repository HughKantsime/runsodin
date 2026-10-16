"""Frozen additive recovery evidence. No backfill or physical action authority."""
from sqlalchemy import MetaData,Table,Column,String,Text,Integer,BigInteger,CheckConstraint,ForeignKeyConstraint
from core.timelapse_retirement_schema import intent,objects

metadata=MetaData()
intent.to_metadata(metadata)
objects.to_metadata(metadata)


def directory_facts():
    return tuple(Column(name,BigInteger,nullable=False) for name in ('device','inode','uid','gid','mode'))


def directory_check(name,*,quarantine=False):
    mode='mode=16832' if quarantine else '(mode & 61440)=16384 AND (mode & 18)=0'
    return CheckConstraint('device>=0 AND inode>0 AND uid>=0 AND gid>=0 AND mode>=0 AND '+mode,name=name)


parents=Table('timelapse_retirement_parents',metadata,
    Column('operation_id',String(64),primary_key=True,nullable=False),
    Column('relative_path',Text,primary_key=True,nullable=False),
    *directory_facts(),
    ForeignKeyConstraint(['operation_id'],['timelapse_retirements.operation_id'],ondelete='RESTRICT'),
    CheckConstraint('length(operation_id) BETWEEN 1 AND 64 AND length(relative_path) BETWEEN 0 AND 4096',name='tp_identity'),
    directory_check('tp_directory'),
)
quarantine=Table('timelapse_retirement_quarantine',metadata,
    Column('operation_id',String(64),primary_key=True,nullable=False),
    *directory_facts(),
    ForeignKeyConstraint(['operation_id'],['timelapse_retirements.operation_id'],ondelete='RESTRICT'),
    CheckConstraint('length(operation_id) BETWEEN 1 AND 64',name='tq_identity'),
    directory_check('tq_directory',quarantine=True),
)
_hex='digest_sha256'
for _character in '0123456789abcdef':_hex="replace("+_hex+",'"+_character+"','')"
moves=Table('timelapse_retirement_moves',metadata,
    Column('operation_id',String(64),primary_key=True,nullable=False),
    Column('object_index',Integer,primary_key=True,nullable=False,autoincrement=False),
    Column('kind',String(16),nullable=False),
    Column('digest_sha256',String(64)),
    Column('phase',String(24),nullable=False),
    Column('verified_ctime_ns',BigInteger),
    ForeignKeyConstraint(['operation_id','object_index'],['timelapse_retirement_objects.operation_id','timelapse_retirement_objects.object_index'],ondelete='RESTRICT'),
    CheckConstraint('length(operation_id) BETWEEN 1 AND 64 AND object_index>=0',name='tm_identity'),
    CheckConstraint("(kind='file' AND digest_sha256 IS NOT NULL AND length(digest_sha256)=64 AND length("+_hex+")=0) OR (kind='directory' AND digest_sha256 IS NULL)",name='tm_digest'),
    CheckConstraint("(phase IN ('planned','rename_pending') AND verified_ctime_ns IS NULL) OR (phase='staged' AND verified_ctime_ns IS NOT NULL AND verified_ctime_ns>=0)",name='tm_phase'),
)
TABLES=(parents,quarantine,moves)
