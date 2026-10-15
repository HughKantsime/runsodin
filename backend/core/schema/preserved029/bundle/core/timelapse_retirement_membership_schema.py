"""Additive lifetime retirement authority; no physical action or inferred history."""
from sqlalchemy import MetaData,Table,Column,String,Text,Integer,DateTime,ForeignKeyConstraint,CheckConstraint,UniqueConstraint
from core.timelapse_retirement_schema import intent,objects
from core.timelapse_publication_schema import lifetimes,versions,namespaces,publications,facts,retained
metadata=MetaData()
for table in (intent,objects,lifetimes,versions,namespaces,publications,facts,retained):table.to_metadata(metadata)
def fk(local,target,remote):return ForeignKeyConstraint(local,[target+'.'+c for c in remote],ondelete='RESTRICT')
bindings=Table('timelapse_retirement_lifetime_bindings',metadata,
 Column('operation_id',String(64),primary_key=True,nullable=False),Column('version_nonce',String(64),nullable=False),Column('lifetime_nonce',String(64),nullable=False),
 Column('phase',String(16),nullable=False),Column('object_count',Integer,nullable=False),Column('created_at',DateTime(timezone=True),nullable=False),Column('sealed_at',DateTime(timezone=True)),
 fk(['operation_id'],intent.name,['operation_id']),fk(['version_nonce','lifetime_nonce'],versions.name,['version_nonce','lifetime_nonce']),
 CheckConstraint('length(operation_id) BETWEEN 1 AND 64 AND length(version_nonce) BETWEEN 1 AND 64 AND length(lifetime_nonce) BETWEEN 1 AND 64 AND object_count BETWEEN 0 AND 16384',name='tmb_identity'),
 CheckConstraint("(phase='building' AND sealed_at IS NULL) OR (phase='sealed' AND sealed_at IS NOT NULL)",name='tmb_phase'))
nulls=['version_nonce','retained_version_nonce','namespace_id','publication_id','fact_role','fact_path']
roles={
 'current_video':({'version_nonce','publication_id','fact_role','fact_path'},"fact_role='output'"),
 'retained_video':({'version_nonce','retained_version_nonce','publication_id','fact_role','fact_path'},"fact_role='source' AND retained_version_nonce=version_nonce"),
 'frame_namespace':({'namespace_id'},'1=1'),
 'completed_frame':({'namespace_id','publication_id','fact_role','fact_path'},"fact_role='output'"),
 'private_directory':({'publication_id','fact_role','fact_path'},"fact_role='private_root'"),
 'private_output':({'publication_id','fact_role','fact_path'},"fact_role='output'")}
bundles=' OR '.join("(role='"+role+"' AND "+' AND '.join(c+(' IS NOT NULL' if c in nonnull else ' IS NULL') for c in nulls)+' AND '+extra+')' for role,(nonnull,extra) in roles.items())
members=Table('timelapse_retirement_members',metadata,
 Column('operation_id',String(64),primary_key=True,nullable=False),Column('object_index',Integer,primary_key=True,nullable=False,autoincrement=False),Column('role',String(24),nullable=False),
 *(Column(c,Text if c=='fact_path' else String(24) if c=='fact_role' else String(64)) for c in nulls),
 fk(['operation_id','object_index'],objects.name,['operation_id','object_index']),fk(['operation_id'],bindings.name,['operation_id']),
 fk(['version_nonce'],versions.name,['version_nonce']),fk(['retained_version_nonce'],retained.name,['version_nonce']),fk(['namespace_id'],namespaces.name,['namespace_id']),
 fk(['publication_id'],publications.name,['operation_id']),fk(['publication_id','fact_role','fact_path'],facts.name,['operation_id','role','relative_path']),
 UniqueConstraint('operation_id','role','version_nonce',name='tmm_version'),UniqueConstraint('operation_id','role','publication_id','fact_role','fact_path',name='tmm_fact'),
 CheckConstraint('length(operation_id) BETWEEN 1 AND 64 AND object_index>=0 AND (fact_path IS NULL OR length(fact_path) BETWEEN 0 AND 4096)',name='tmm_identity'),CheckConstraint(bundles,name='tmm_role'))
receipts=Table('timelapse_retirement_container_receipts',metadata,
 Column('operation_id',String(64),primary_key=True,nullable=False),Column('object_index',Integer,primary_key=True,nullable=False,autoincrement=False),Column('role',String(24),nullable=False),Column('phase',String(16),nullable=False),Column('completed_at',DateTime(timezone=True),nullable=False),
 fk(['operation_id','object_index'],members.name,['operation_id','object_index']),
 CheckConstraint("length(operation_id) BETWEEN 1 AND 64 AND object_index>=0 AND role IN ('frame_namespace','private_directory') AND phase='complete'",name='tmr_identity'))
TABLES=(bindings,members,receipts)
