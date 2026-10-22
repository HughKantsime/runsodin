"""Frozen additive publication journal. Empty schema grants no writer authority."""
from sqlalchemy import (MetaData,Table,Column,String,Text,Integer,BigInteger,DateTime,
    CheckConstraint,ForeignKeyConstraint,UniqueConstraint,func)
from core.timelapse_ownership_schema import anchors
from core.timelapse_retirement_schema import intent

metadata=MetaData()
anchors.to_metadata(metadata);intent.to_metadata(metadata)


def fk(columns,target,remote):
    return ForeignKeyConstraint(columns,[target+'.'+name for name in remote],ondelete='RESTRICT')


def created():
    return Column('created_at',DateTime(timezone=True),nullable=False,server_default=func.current_timestamp())


def hexadecimal(column):
    value=column
    for character in '0123456789abcdef':value="replace("+value+",'"+character+"','')"
    return column+' IS NOT NULL AND length('+column+')=64 AND length('+value+')=0'


lifetimes=Table('timelapse_media_lifetimes',metadata,
    Column('lifetime_nonce',String(64),primary_key=True,nullable=False),
    Column('original_timelapse_id',Integer,nullable=False),Column('original_printer_id',Integer,nullable=False),
    *(Column('root_'+name,BigInteger,nullable=False) for name in ('device','inode','uid','gid','mode')),created(),
    fk(['lifetime_nonce'],'timelapse_ownership_anchors',['operation_id']),
    CheckConstraint('length(lifetime_nonce) BETWEEN 1 AND 64 AND original_timelapse_id>0 AND original_printer_id>0',name='tl_identity'),
    CheckConstraint('root_device>=0 AND root_inode>0 AND root_uid>=0 AND root_gid>=0 AND root_mode>=0 AND (root_mode & 61440)=16384 AND (root_mode & 18)=0',name='tl_root'),
)
versions=Table('timelapse_media_versions',metadata,
    Column('version_nonce',String(64),primary_key=True,nullable=False),Column('lifetime_nonce',String(64),nullable=False),
    fk(['version_nonce'],'timelapse_ownership_anchors',['operation_id']),fk(['lifetime_nonce'],lifetimes.name,['lifetime_nonce']),
    UniqueConstraint('version_nonce','lifetime_nonce',name='tv_lifetime'),
    CheckConstraint('length(version_nonce) BETWEEN 1 AND 64 AND length(lifetime_nonce) BETWEEN 1 AND 64',name='tv_identity'),
)
_namespace_facts=('device','inode','uid','gid','mode')
_empty=' AND '.join(name+' IS NULL' for name in _namespace_facts)
_complete=' AND '.join(name+' IS NOT NULL' for name in _namespace_facts)+ ' AND device>=0 AND inode>0 AND uid>=0 AND gid>=0 AND mode=16832'
namespaces=Table('timelapse_frame_namespaces',metadata,
    Column('namespace_id',String(64),primary_key=True,nullable=False),Column('lifetime_nonce',String(64),nullable=False),
    Column('relative_path',Text,nullable=False),Column('phase',String(16),nullable=False),
    *(Column(name,BigInteger) for name in _namespace_facts),created(),
    fk(['lifetime_nonce'],lifetimes.name,['lifetime_nonce']),UniqueConstraint('relative_path',name='tn_path'),
    CheckConstraint('length(namespace_id) BETWEEN 1 AND 64 AND length(lifetime_nonce) BETWEEN 1 AND 64 AND length(relative_path) BETWEEN 1 AND 128',name='tn_identity'),
    CheckConstraint("(phase='planned' AND "+_empty+") OR (phase IN ('bound','retired') AND "+_complete+") OR (phase='blocked' AND (("+_empty+") OR ("+_complete+")))",name='tn_binding'),
)
bindings=Table('timelapse_frame_bindings',metadata,
    Column('lifetime_nonce',String(64),primary_key=True,nullable=False),Column('namespace_id',String(64),nullable=False),
    Column('next_number',BigInteger,nullable=False),
    fk(['lifetime_nonce'],lifetimes.name,['lifetime_nonce']),fk(['namespace_id'],namespaces.name,['namespace_id']),
    UniqueConstraint('namespace_id',name='tb_namespace'),
    CheckConstraint('length(lifetime_nonce) BETWEEN 1 AND 64 AND length(namespace_id) BETWEEN 1 AND 64 AND next_number BETWEEN 1 AND 9223372036854775807',name='tb_counter'),
)
_reasons="'unknown_authority','ownership_changed','unsafe_path','unsupported_platform','object_changed','file_io','phase_uncertain','manifest_limit','writer_active'"
publications=Table('timelapse_publications',metadata,
    Column('operation_id',String(64),primary_key=True,nullable=False),
    Column('source_nonce',String(64),nullable=False),Column('source_lifetime',String(64),nullable=False),
    Column('recipe_version',Integer,nullable=False),Column('encode_fps',Integer),
    Column('speed_numerator',Integer),Column('speed_denominator',Integer),
    Column('trim_start',String(340)),Column('trim_end',String(340)),
    Column('kind',String(16),nullable=False),Column('source_timelapse_id',Integer,nullable=False),Column('source_printer_id',Integer,nullable=False),
    Column('source_filename',Text,nullable=False),Column('source_status',String(16),nullable=False),Column('source_preclaim_status',String(16)),Column('actor_id',Integer),
    Column('frame_namespace_id',String(64)),Column('frame_number',BigInteger),Column('payload_byte_count',BigInteger),Column('payload_digest_sha256',String(64)),
    Column('private_directory',Text,nullable=False),Column('private_name',Text,nullable=False),Column('destination_path',Text,nullable=False),
    Column('result_nonce',String(64),nullable=False),Column('result_lifetime',String(64),nullable=False),Column('result_tid',Integer,nullable=False),
    Column('phase',String(24),nullable=False),Column('state',String(16),nullable=False),Column('revision',Integer,nullable=False),
    Column('reason_code',String(64)),Column('published_ctime_ns',BigInteger),created(),Column('completed_at',DateTime(timezone=True)),
    fk(['source_nonce','source_lifetime'],versions.name,['version_nonce','lifetime_nonce']),
    fk(['result_nonce','result_lifetime'],versions.name,['version_nonce','lifetime_nonce']),
    fk(['frame_namespace_id'],namespaces.name,['namespace_id']),
    UniqueConstraint('private_directory',name='tp_private'),UniqueConstraint('destination_path',name='tp_destination'),
    UniqueConstraint('frame_namespace_id','frame_number',name='tp_frame'),
    CheckConstraint("recipe_version=1 AND ((kind='frame' AND encode_fps IS NULL AND speed_numerator IS NULL AND speed_denominator IS NULL AND trim_start IS NULL AND trim_end IS NULL) OR (kind='encode' AND encode_fps IS NOT NULL AND encode_fps=30 AND speed_numerator IS NULL AND speed_denominator IS NULL AND trim_start IS NULL AND trim_end IS NULL) OR (kind='speed' AND encode_fps IS NULL AND speed_numerator IS NOT NULL AND speed_denominator IS NOT NULL AND ((speed_numerator=1 AND speed_denominator IN (1,2)) OR (speed_numerator=3 AND speed_denominator=2) OR (speed_numerator IN (2,4,8) AND speed_denominator=1)) AND trim_start IS NULL AND trim_end IS NULL) OR (kind='trim' AND encode_fps IS NULL AND speed_numerator IS NULL AND speed_denominator IS NULL AND trim_start IS NOT NULL AND trim_end IS NOT NULL AND length(trim_start) BETWEEN 1 AND 340 AND length(trim_end) BETWEEN 1 AND 340))",name='tp_recipe'),
    CheckConstraint('length(operation_id) BETWEEN 1 AND 64 AND length(source_nonce) BETWEEN 1 AND 64 AND length(source_lifetime) BETWEEN 1 AND 64 AND source_timelapse_id>0 AND source_printer_id>0 AND length(source_filename) BETWEEN 1 AND 4096 AND revision>0',name='tp_identity'),
    CheckConstraint('length(result_nonce) BETWEEN 1 AND 64 AND length(result_lifetime) BETWEEN 1 AND 64 AND result_tid>0 AND length(private_directory) BETWEEN 1 AND 128 AND length(destination_path) BETWEEN 1 AND 4096',name='tp_result'),
    CheckConstraint("(kind='frame' AND source_status='capturing' AND source_preclaim_status IS NULL AND actor_id IS NULL AND frame_namespace_id IS NOT NULL AND length(frame_namespace_id) BETWEEN 1 AND 64 AND frame_number IS NOT NULL AND frame_number>0 AND payload_byte_count IS NOT NULL AND payload_byte_count BETWEEN 1 AND 16777216 AND "+hexadecimal('payload_digest_sha256')+" AND private_name='output.jpg' AND result_nonce=source_nonce AND result_lifetime=source_lifetime AND result_tid=source_timelapse_id) OR (kind='encode' AND source_status='encoding' AND source_preclaim_status IS NOT NULL AND source_preclaim_status='capturing' AND actor_id IS NULL AND frame_namespace_id IS NULL AND frame_number IS NULL AND payload_byte_count IS NULL AND payload_digest_sha256 IS NULL AND private_name='output.mp4' AND result_nonce<>source_nonce AND result_lifetime=source_lifetime AND result_tid=source_timelapse_id) OR (kind IN ('trim','speed') AND source_status='ready' AND source_preclaim_status IS NULL AND actor_id IS NOT NULL AND actor_id>0 AND frame_namespace_id IS NULL AND frame_number IS NULL AND payload_byte_count IS NULL AND payload_digest_sha256 IS NULL AND private_name='output.mp4' AND result_nonce<>source_nonce AND ((kind='trim' AND result_lifetime=source_lifetime AND result_tid=source_timelapse_id) OR (kind='speed' AND result_lifetime=result_nonce AND result_lifetime<>source_lifetime AND result_tid<>source_timelapse_id)))",name='tp_kind'),
    CheckConstraint("phase IN ('admitted','private_created','worker_admitted','output_ready','publish_pending','published','complete') AND (kind<>'frame' OR phase<>'worker_admitted')",name='tp_phase'),
    CheckConstraint("(phase='complete' AND state='complete' AND completed_at IS NOT NULL AND reason_code IS NULL) OR (phase<>'complete' AND completed_at IS NULL AND ((state='active' AND reason_code IS NULL) OR (state='blocked' AND reason_code IS NOT NULL)))",name='tp_state'),
    CheckConstraint("(phase IN ('published','complete') AND published_ctime_ns IS NOT NULL AND published_ctime_ns>=0) OR (phase NOT IN ('published','complete') AND published_ctime_ns IS NULL)",name='tp_published'),
    CheckConstraint('reason_code IS NULL OR reason_code IN ('+_reasons+')',name='tp_reason'),
)
_file_facts=('device','inode','uid','gid','mode','link_count','byte_count','mtime_ns','ctime_ns')
_file=' AND '.join(name+' IS NOT NULL AND '+name+'>=0' for name in _file_facts)+" AND inode>0 AND link_count=1 AND (mode & 61440)=32768 AND (mode & 18)=0 AND "+hexadecimal('digest_sha256')
_dir=' AND '.join(name+' IS NOT NULL' for name in _namespace_facts)+ ' AND device>=0 AND inode>0 AND uid>=0 AND gid>=0 AND mode>=0 AND (mode & 61440)=16384 AND (mode & 18)=0 AND '+ ' AND '.join(name+' IS NULL' for name in ('link_count','byte_count','mtime_ns','ctime_ns','digest_sha256'))
facts=Table('timelapse_publication_facts',metadata,
    Column('operation_id',String(64),primary_key=True,nullable=False),Column('role',String(24),primary_key=True,nullable=False),Column('relative_path',Text,primary_key=True,nullable=False),
    Column('kind',String(16),nullable=False),*(Column(name,BigInteger) for name in _file_facts),Column('digest_sha256',String(64)),
    fk(['operation_id'],publications.name,['operation_id']),
    CheckConstraint("length(operation_id) BETWEEN 1 AND 64 AND length(relative_path) BETWEEN 0 AND 4096 AND role IN ('root','source','source_parent','private_root','private_creation','output','output_parent')",name='tf_identity'),
    CheckConstraint("(kind='file' AND "+_file+") OR (kind='directory' AND "+_dir+")",name='tf_facts'),
    CheckConstraint("(role IN ('root','source_parent','output_parent') AND kind='directory') OR (role='private_root' AND kind='directory' AND mode=16832) OR (role='source') OR (role='output' AND kind='file') OR (role='private_creation' AND kind='file' AND mode=33152 AND byte_count=0 AND digest_sha256='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')",name='tf_role'),
)
retained=Table('timelapse_retained_versions',metadata,
    Column('version_nonce',String(64),primary_key=True,nullable=False),Column('lifetime_nonce',String(64),nullable=False),Column('retained_by_operation',String(64),nullable=False),
    Column('filename',Text,nullable=False),Column('state',String(16),nullable=False),Column('retire_operation_id',String(64)),
    fk(['version_nonce','lifetime_nonce'],versions.name,['version_nonce','lifetime_nonce']),fk(['retained_by_operation'],publications.name,['operation_id']),fk(['retire_operation_id'],'timelapse_retirements',['operation_id']),
    CheckConstraint('length(version_nonce) BETWEEN 1 AND 64 AND length(lifetime_nonce) BETWEEN 1 AND 64 AND length(retained_by_operation) BETWEEN 1 AND 64 AND length(filename) BETWEEN 1 AND 4096',name='trv_identity'),
    CheckConstraint("(state='retained' AND retire_operation_id IS NULL) OR (state='retired' AND retire_operation_id IS NOT NULL AND length(retire_operation_id) BETWEEN 1 AND 64)",name='trv_retirement'),
)
TABLES=(lifetimes,versions,namespaces,bindings,publications,facts,retained)
