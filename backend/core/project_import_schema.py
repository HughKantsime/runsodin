"""Unregistered029 retained import journals. No filesystem/request capability.

SQLite apply is deliberately refusal-only pending real maintenance gate activation.
DDL generation also permits owned fresh-contract tests, not existing-table rebuilds.
"""
import hashlib
import re
from uuid import UUID
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

ROOT='project_import_root_generations'; OP='project_import_operations'; MEMBER='project_import_members'
CHECKPOINT='project_import_checkpoints'; CLEANUP='project_import_cleanup'
TABLES=(ROOT,OP,MEMBER,CHECKPOINT,CLEANUP)
SOURCE='project_import_nonce'
SOURCE_TABLES=('projects','print_archives','print_files')
MAX_INT=2**63-1
EMPTY_SHA=hashlib.sha256(b'').hexdigest()
FACTS=('device','inode','kind','mode','service_uid','link_count','ctime_ns','mtime_ns','birth_ns')
REGULAR_FACTS=(*FACTS,'byte_count','digest_sha256')

class ProjectImportMaintenanceRequired(MigrationError):pass

def _refuse():raise MigrationError('Project import storage contract differs') from None

def _execute_closed_ddl(connection, statement):
    """Transport fixed generated SQL without interpreting literal percent as binds."""
    clause=text(statement)
    compiled=clause.compile(dialect=connection.dialect)
    if compiled.params or compiled.binds:
        _refuse()
    return connection.execute(clause)

def hex_check(name,size,pg):
    return f"{name} ~ '^[0-9a-f]{{{size}}}$'" if pg else f"typeof({name})='text' AND length({name})={size} AND {name} NOT GLOB '*[^0-9a-f]*'"

def nonce_check(n,pg):return f"({hex_check(n,32,pg)} AND substr({n},13,1)='4' AND substr({n},17,1) IN ('8','9','a','b'))"
def uuid_check(n,pg):
    stripped="replace("+n+",'-','')"
    return f"({hex_check(stripped,32,pg)} AND length({n})=36 AND substr({n},9,1)='-' AND substr({n},14,1)='-' AND substr({n},19,1)='-' AND substr({n},24,1)='-' AND substr({n},15,1)='4' AND substr({n},20,1) IN ('8','9','a','b'))"

def time_check(n,pg):
    if pg:return f'isfinite({n})'
    return f"typeof({n})='text' AND length({n})=27 AND substr({n},5,1)='-' AND substr({n},8,1)='-' AND substr({n},11,1)='T' AND substr({n},14,1)=':' AND substr({n},17,1)=':' AND substr({n},20,1)='.' AND substr({n},27,1)='Z' AND replace(replace(replace(replace(replace({n},'-',''),'T',''),':',''),'.',''),'Z','') NOT GLOB '*[^0-9]*' AND substr({n},1,4) BETWEEN '0001' AND '9999' AND substr({n},12,2) BETWEEN '00' AND '23' AND substr({n},15,2) BETWEEN '00' AND '59' AND substr({n},18,2) BETWEEN '00' AND '59' AND date(substr({n},1,10),'+0 days') IS substr({n},1,10)"

def all_null(names,prefix=''):return ' AND '.join(prefix+n+' IS NULL' for n in names)
def all_present(names,prefix=''):return ' AND '.join(prefix+n+' IS NOT NULL' for n in names)
def equal(names,a='NEW.',b='OLD.'):return ' AND '.join(a+n+' IS NOT DISTINCT FROM '+b+n for n in names)

def definition(pg):
    """Closed definitions: declarations, key/FK constraints and local predicates."""
    result={}; timestamp='TIMESTAMP WITH TIME ZONE' if pg else 'TEXT'
    now='CURRENT_TIMESTAMP' if pg else "(strftime('%Y-%m-%dT%H:%M:%f','now')||'000Z')"
    def table(name):
        columns=[]; constraints=[]; result[name]=(columns,constraints)
        def col(n,t='TEXT',nullable=False,check=None,default=None):
            sql=n+' '+t+('' if nullable else ' NOT NULL')+(' DEFAULT '+default if default is not None else '')
            if check:sql+=' CHECK('+((n+' IS NULL OR ('+check+')') if nullable else check)+')'
            columns.append(sql)
        def integer(n,nullable=False,low=0,high=MAX_INT,default=None):
            col(n,'BIGINT' if pg else 'INTEGER',nullable,f"{n} BETWEEN {low} AND {high}"+('' if pg else f" AND typeof({n})='integer'"),default)
        def text(n,nullable=False,check=None,default=None):col(n,'TEXT',nullable,check,default)
        def nonce(n,nullable=False):text(n,nullable,nonce_check(n,pg))
        def time(n,nullable=False,default=None):col(n,timestamp,nullable,time_check(n,pg),default)
        def facts(prefix,kind,nullable=True):
            for n in REGULAR_FACTS if kind=='regular' else FACTS:
                key=prefix+n
                if n=='kind':text(key,nullable,f"{key}='{kind}'")
                elif n=='digest_sha256':text(key,nullable,hex_check(key,64,pg))
                elif n=='mode':integer(key,nullable,448 if kind=='directory' else 384,448 if kind=='directory' else 384)
                elif n in ('inode','service_uid','link_count','birth_ns'):integer(key,True if n=='birth_ns' else nullable,1,1 if n=='link_count' and kind=='regular' else MAX_INT)
                elif n=='byte_count':integer(key,nullable,0,134217728)
                else:integer(key,nullable)
        def check(s):constraints.append('CHECK('+s+')')
        def fk(local,target,remote,deferred=False,noaction=False):constraints.append('FOREIGN KEY('+','.join(local)+') REFERENCES '+target+'('+','.join(remote)+') ON UPDATE '+('NO ACTION' if noaction else 'RESTRICT')+' ON DELETE '+('NO ACTION' if noaction else 'RESTRICT')+(' DEFERRABLE INITIALLY DEFERRED' if deferred else ''))
        return text,nonce,time,integer,facts,check,fk,constraints
    text,nonce,time,integer,facts,check,fk,k=table(ROOT)
    nonce('generation');k.append('PRIMARY KEY(generation)');nonce('enrollment_nonce');k.append('UNIQUE(enrollment_nonce)')
    text('command_id',check=uuid_check('command_id',pg));k.append('UNIQUE(command_id)')
    for n in ('configured_root_key','request_digest','expected_sha256'):text(n,check=hex_check(n,64,pg))
    integer('requested_by',low=1);nonce('namespace_birth_nonce');nonce('marker_birth_nonce')
    k.extend(('UNIQUE(namespace_birth_nonce)','UNIQUE(marker_birth_nonce)'))
    text('state',check="state IN ('enrolling','active','quarantined')");integer('lifecycle_revision',low=1,high=2)
    time('created_at',default=now);time('terminal_at',True);text('reason',True,"reason IN ('unknown_existing_object','identity_changed','metadata_uncertain','ack_unresolved','authority_changed')")
    text('cleanup_capability',check="cleanup_capability IN ('live_fd_only','birth_verified')");integer('expected_size',low=37,high=37)
    for phase in ('created','sealed','failure'):
        for kind in ('namespace','marker'):facts(kind+'_'+phase+'_','directory' if kind=='namespace' else 'regular')
        time(phase+'_checkpoint_at',True)
        required=tuple('namespace_'+phase+'_'+n for n in FACTS if n!='birth_ns')+tuple('marker_'+phase+'_'+n for n in REGULAR_FACTS if n!='birth_ns')+(phase+'_checkpoint_at',)
        optional=('namespace_'+phase+'_birth_ns','marker_'+phase+'_birth_ns')
        check('('+all_null((*required,*optional))+') OR ('+all_present(required)+')')
        check(f"marker_{phase}_byte_count IS NULL OR marker_{phase}_byte_count BETWEEN 0 AND 37")
    check("(state='enrolling' AND lifecycle_revision=1 AND terminal_at IS NULL AND reason IS NULL) OR (state IN ('active','quarantined') AND lifecycle_revision=2 AND terminal_at IS NOT NULL AND ((state='active' AND reason IS NULL) OR (state='quarantined' AND reason IS NOT NULL)))")
    check(f"created_checkpoint_at IS NULL OR (marker_created_byte_count=0 AND marker_created_digest_sha256='{EMPTY_SHA}')")
    check('sealed_checkpoint_at IS NULL OR (marker_sealed_byte_count=37 AND marker_sealed_digest_sha256=expected_sha256)')
    check("failure_checkpoint_at IS NULL OR state='quarantined'")
    check('sealed_checkpoint_at IS NULL OR created_checkpoint_at IS NOT NULL')
    check('failure_checkpoint_at IS NULL OR created_checkpoint_at IS NOT NULL')
    for kind in ('namespace','marker'):
        for n in ('device','inode','kind','mode','service_uid','birth_ns'):
            check(f'sealed_checkpoint_at IS NULL OR {kind}_sealed_{n} IS NOT DISTINCT FROM {kind}_created_{n}')
    check("state<>'active' OR (created_checkpoint_at IS NOT NULL AND sealed_checkpoint_at IS NOT NULL AND marker_sealed_byte_count=37 AND marker_sealed_digest_sha256=expected_sha256)")
    check("cleanup_capability<>'birth_verified' OR (namespace_created_birth_ns IS NOT NULL AND marker_created_birth_ns IS NOT NULL AND namespace_sealed_birth_ns IS NOT NULL AND marker_sealed_birth_ns IS NOT NULL)")
    text,nonce,time,integer,facts,check,fk,k=table(OP)
    text('operation_id',check=uuid_check('operation_id',pg));k.append('PRIMARY KEY(operation_id)')
    text('owner_scope',check="owner_scope IN ('org','global')");integer('owner_key');check("(owner_scope='org' AND owner_key>0) OR (owner_scope='global' AND owner_key=0)")
    integer('requested_by',low=1);text('manifest_sha256',check=hex_check('manifest_sha256',64,pg));text('relative_directory');k.append('UNIQUE(relative_directory)')
    check("relative_directory='.project-imports/'||replace(operation_id,'-','')")
    integer('operation_marker_size',low=70,high=70);text('operation_marker_sha256',check=hex_check('operation_marker_sha256',64,pg))
    integer('declared_members',high=128);nonce('storage_root_generation');fk(['storage_root_generation'],ROOT,['generation'])
    text('state',check="state IN ('staging','committed','abandoned','quarantined')");integer('lifecycle_revision',low=1,high=2)
    time('created_at',default=now);time('terminal_at',True);text('reason',True,"reason IN ('definite_failure','unknown_existing_object','identity_changed','metadata_uncertain','ack_unresolved','authority_changed')")
    for n in ('project_birth_nonce','directory_birth_nonce','operation_marker_birth_nonce'):nonce(n);k.append('UNIQUE('+n+')')
    integer('project_birth_id',True,1);k.append('UNIQUE(project_birth_id,project_birth_nonce)')
    k.append('UNIQUE(operation_id,state)')
    result[OP][0].append("required_operation_state TEXT GENERATED ALWAYS AS (CASE WHEN project_birth_id IS NOT NULL THEN 'committed' ELSE NULL END) STORED")
    fk(['operation_id','required_operation_state'],OP,['operation_id','state'],True,True)
    integer('directory_device',True);integer('directory_inode',True,1);text('directory_kind',True,"directory_kind='directory'")
    check('('+all_null(('directory_device','directory_inode','directory_kind'))+') OR ('+all_present(('directory_device','directory_inode','directory_kind'))+')')
    for n in ('directory_receipt_object_key','directory_receipt_domain'):text(n,True)
    integer('directory_receipt_revision',True,1,1)
    check('('+all_null(('directory_receipt_object_key','directory_receipt_domain','directory_receipt_revision'))+") OR (directory_receipt_object_key='directory' AND directory_receipt_domain='content' AND directory_receipt_revision=1)")
    fk(['operation_id','directory_receipt_object_key','directory_receipt_domain','directory_receipt_revision'],CHECKPOINT,['operation_id','object_key','checkpoint_domain','domain_revision'],True)
    check("(state='staging' AND lifecycle_revision=1 AND terminal_at IS NULL AND reason IS NULL) OR (state='committed' AND lifecycle_revision=2 AND terminal_at IS NOT NULL AND reason IS NULL AND project_birth_id IS NOT NULL AND directory_inode IS NOT NULL AND directory_receipt_revision=1) OR (state IN ('abandoned','quarantined') AND lifecycle_revision=2 AND terminal_at IS NOT NULL AND reason IS NOT NULL AND project_birth_id IS NULL)")
    text,nonce,time,integer,facts,check,fk,k=table(MEMBER)
    text('operation_id',check=uuid_check('operation_id',pg));integer('member_index',high=127);k.append('PRIMARY KEY(operation_id,member_index)');fk(['operation_id'],OP,['operation_id'])
    nonce('member_nonce');k.append('UNIQUE(member_nonce)');text('archive_key');check('archive_key=CAST(member_index AS TEXT)')
    text('protection',check="protection IN ('generic','education')")
    for n in ('expected_relative_path','expected_sha256','original_name'):text(n,True,hex_check(n,64,pg) if n=='expected_sha256' else None)
    integer('expected_size',True,high=134217728)
    quartet=('expected_relative_path','expected_sha256','expected_size','original_name')
    padded="lpad(CAST(member_index AS TEXT),3,'0')" if pg else "printf('%03d',member_index)"
    check('('+all_null(quartet)+') OR ('+all_present(quartet)+f" AND expected_relative_path IN ({padded}||'.3mf',{padded}||'.gcode',{padded}||'.bgcode') AND PLACEHOLDER_BYTES BETWEEN 1 AND 255 AND original_name NOT IN ('.','..') AND original_name NOT LIKE '%/%' AND original_name NOT LIKE '%\\%' AND instr(original_name,char(0))=0".replace('instr(original_name,char(0))=0',"position(chr(0) in original_name)=0" if pg else 'instr(original_name,char(0))=0')+')')
    k[-1]=k[-1].replace('PLACEHOLDER_BYTES','octet_length(original_name)' if pg else 'length(CAST(original_name AS BLOB))')
    # PostgreSQL text cannot contain NUL; chr(0) itself is invalid there.
    if pg:k[-1]=k[-1].replace(' AND position(chr(0) in original_name)=0','')
    control=' AND '.join((f'position(chr({code}) in original_name)=0' if pg else f'instr(original_name,char({code}))=0') for code in (*range(1 if pg else 0,32),127))
    slash="position(chr(92) in original_name)=0" if pg else "instr(original_name,char(92))=0"
    check('original_name IS NULL OR ('+control+' AND '+slash+')')
    for n in ('archive_birth_id','file_birth_id'):integer(n,True,1);k.extend(('UNIQUE('+n+')','UNIQUE('+n+',member_nonce)'))
    result[MEMBER][0].append("required_operation_state TEXT GENERATED ALWAYS AS (CASE WHEN archive_birth_id IS NOT NULL OR file_birth_id IS NOT NULL THEN 'committed' ELSE NULL END) STORED")
    fk(['operation_id','required_operation_state'],OP,['operation_id','state'],True,True)
    for n in ('file_device','file_inode','verified_size'):integer(n,True,1 if n=='file_inode' else 0,134217728 if n=='verified_size' else MAX_INT)
    text('file_kind',True,"file_kind='regular'");text('verified_sha256',True,hex_check('verified_sha256',64,pg))
    evidence=('file_device','file_inode','file_kind','verified_size','verified_sha256')
    check('('+all_null(evidence)+') OR ('+all_present(evidence)+' AND verified_size=expected_size AND verified_sha256=expected_sha256)')
    for n in ('sealed_object_key','sealed_domain'):text(n,True)
    integer('sealed_revision',True,1,1)
    check('('+all_null(('sealed_object_key','sealed_domain','sealed_revision'))+") OR (sealed_object_key=CAST(member_index AS TEXT) AND sealed_domain='content' AND sealed_revision=1)")
    fk(['operation_id','sealed_object_key','sealed_domain','sealed_revision'],CHECKPOINT,['operation_id','object_key','checkpoint_domain','domain_revision'],True)
    check('expected_relative_path IS NOT NULL OR ('+all_null(('file_birth_id',*evidence,'sealed_object_key','sealed_domain','sealed_revision'))+')')
    text,nonce,time,integer,facts,check,fk,k=table(CHECKPOINT)
    text('operation_id',check=uuid_check('operation_id',pg));text('object_key');text('checkpoint_domain',check="checkpoint_domain IN ('birth','content','failure_identity','owned_metadata','cleanup_metadata')");integer('domain_revision',low=1,high=129)
    k.append('PRIMARY KEY(operation_id,object_key,checkpoint_domain,domain_revision)');fk(['operation_id'],OP,['operation_id'])
    integer('member_index',True,high=127);fk(['operation_id','member_index'],MEMBER,['operation_id','member_index'])
    check("(object_key IN ('directory','operation_birth') AND member_index IS NULL) OR (member_index IS NOT NULL AND object_key=CAST(member_index AS TEXT))")
    nonce('root_generation');fk(['root_generation'],ROOT,['generation']);nonce('object_birth_nonce')
    # Dynamic kind/bounds; the closed selector defines directory versus regular.
    for n in REGULAR_FACTS:
        if n=='kind':text(n,check="kind IN ('directory','regular')")
        elif n=='digest_sha256':text(n,True,hex_check(n,64,pg))
        elif n=='mode':integer(n,low=384,high=448)
        elif n in ('inode','service_uid','link_count','birth_ns'):integer(n,n=='birth_ns',1)
        else:integer(n,n=='byte_count',high=134217728 if n=='byte_count' else MAX_INT)
    check("(object_key='directory' AND kind='directory' AND mode=448 AND byte_count IS NULL AND digest_sha256 IS NULL) OR (object_key<>'directory' AND kind='regular' AND mode=384 AND link_count=1 AND byte_count IS NOT NULL AND digest_sha256 IS NOT NULL)")
    time('recorded_at',default=now)
    for prefix in ('predecessor','related'):
        for n in ('object_key','domain'):text(prefix+'_'+n,True)
        integer(prefix+'_revision',True,1,129)
        ns=tuple(prefix+'_'+n for n in ('object_key','domain','revision'));check('('+all_null(ns)+') OR ('+all_present(ns)+')')
        fk(['operation_id',*ns],CHECKPOINT,['operation_id','object_key','checkpoint_domain','domain_revision'])
    text('cleanup_object_key',True);integer('cleanup_transition_revision',True,2,2)
    fk(['operation_id','cleanup_object_key'],CLEANUP,['operation_id','object_key'])
    check("(checkpoint_domain='cleanup_metadata' AND object_key='directory' AND cleanup_object_key IS NOT NULL AND cleanup_object_key<>'directory' AND cleanup_transition_revision=2 AND "+all_null(('related_object_key','related_domain','related_revision'))+") OR (checkpoint_domain<>'cleanup_metadata' AND cleanup_object_key IS NULL AND cleanup_transition_revision IS NULL)")
    k.append('UNIQUE(operation_id,cleanup_object_key,cleanup_transition_revision)')
    check("(checkpoint_domain IN ('birth','content','failure_identity') AND domain_revision=1) OR (object_key='directory' AND checkpoint_domain='owned_metadata' AND domain_revision BETWEEN 1 AND 128) OR (object_key='directory' AND checkpoint_domain='cleanup_metadata' AND domain_revision BETWEEN 1 AND 129)")
    text,nonce,time,integer,facts,check,fk,k=table(CLEANUP)
    text('operation_id',check=uuid_check('operation_id',pg));text('object_key');k.append('PRIMARY KEY(operation_id,object_key)');fk(['operation_id'],OP,['operation_id'])
    integer('member_index',True,high=127);fk(['operation_id','member_index'],MEMBER,['operation_id','member_index'])
    check("(object_key IN ('directory','operation_birth') AND member_index IS NULL) OR (member_index IS NOT NULL AND object_key=CAST(member_index AS TEXT))")
    text('checkpoint_domain',check="checkpoint_domain IN ('content','failure_identity')");integer('checkpoint_revision',low=1,high=1)
    fk(['operation_id','object_key','checkpoint_domain','checkpoint_revision'],CHECKPOINT,['operation_id','object_key','checkpoint_domain','domain_revision'])
    nonce('root_generation');fk(['root_generation'],ROOT,['generation']);nonce('object_birth_nonce')
    text('purpose',check="purpose='abandoned_cleanup'");text('state',check="state IN ('intent','unlinked','complete','uncertain')");integer('revision',low=1,high=3)
    time('created_at',default=now);time('changed_at',default=now);time('completion_at',True)
    text('uncertainty_code',True,"uncertainty_code IN ('root_changed','identity_changed','metadata_uncertain','io_uncertain','database_unreachable')")
    for n in ('parent_receipt_object_key','parent_receipt_domain'):text(n,True)
    integer('parent_receipt_revision',True,1,129)
    parent=('parent_receipt_object_key','parent_receipt_domain','parent_receipt_revision')
    check("(object_key='directory' AND "+all_null(parent)+") OR (object_key<>'directory' AND ((state IN ('intent','uncertain') AND revision IN (1,2) AND "+all_null(parent)+") OR ((state='unlinked' OR revision=3) AND parent_receipt_object_key='directory' AND parent_receipt_domain='cleanup_metadata' AND parent_receipt_revision IS NOT NULL)))")
    fk(['operation_id',*parent],CHECKPOINT,['operation_id','object_key','checkpoint_domain','domain_revision'],True)
    check("(state='intent' AND revision=1 AND completion_at IS NULL AND uncertainty_code IS NULL) OR (state='unlinked' AND revision=2 AND completion_at IS NULL AND uncertainty_code IS NULL) OR (state='complete' AND revision=3 AND completion_at IS NOT NULL AND uncertainty_code IS NULL) OR (state='uncertain' AND revision IN (2,3) AND completion_at IS NULL AND uncertainty_code IS NOT NULL)")
    return result


def ddl(table,pg,*,foreign=True):
    columns,constraints=definition(pg)[table]
    return 'CREATE TABLE '+table+'('+','.join((*columns,*(x for x in constraints if foreign or not x.startswith('FOREIGN KEY'))))+')'


def columns(table,pg=False):return tuple(x.split()[0] for x in definition(pg)[table][0])


def owner_sql(alias,source):return f"(({alias}.owner_scope='org' AND {source}.org_id={alias}.owner_key) OR ({alias}.owner_scope='global' AND {source}.org_id IS NULL))"


def marker_sha(generation,operation=None,directory_nonce=None):
    data=(str(UUID(hex=generation))+'\n') if operation is None else operation+'\n'+directory_nonce+'\n'
    return hashlib.sha256(data.encode('ascii')).hexdigest()


def _source_binding(kind,n='NEW'):
    if kind=='projects':
        return f"EXISTS(SELECT 1 FROM {OP} o WHERE o.project_birth_nonce={n}.{SOURCE} AND o.state='staging' AND o.project_birth_id IS NULL AND {owner_sql('o',n)} AND {n}.created_by=o.requested_by)"
    id_column='archive_birth_id' if kind=='print_archives' else 'file_birth_id'
    base=f"EXISTS(SELECT 1 FROM {MEMBER} m JOIN {OP} o USING(operation_id) WHERE m.member_nonce={n}.{SOURCE} AND m.{id_column} IS NULL AND o.state='staging' AND {owner_sql('o',n)}"
    if kind=='print_files':base+=f" AND m.expected_relative_path IS NOT NULL AND {n}.created_by=o.requested_by AND {n}.model_id IS NULL AND {n}.job_id IS NULL AND {n}.stored_path LIKE '%/'||o.relative_directory||'/'||m.expected_relative_path"
    else:base+=f" AND {n}.project_id=o.project_birth_id AND {n}.ownership_state='known' AND {n}.job_id IS NULL AND {n}.print_job_id IS NULL AND {n}.printer_id IS NULL AND {n}.user_id IS NULL AND ((m.expected_relative_path IS NULL AND {n}.print_file_id IS NULL AND {n}.file_path IS NULL) OR (m.expected_relative_path IS NOT NULL AND {n}.print_file_id=m.file_birth_id AND EXISTS(SELECT 1 FROM print_files f WHERE f.id=m.file_birth_id AND f.{SOURCE}=m.member_nonce AND f.stored_path={n}.file_path)))"
    return base+')'


SOURCE_IMMUTABLE={
 'projects':('id',SOURCE,'org_id','created_by'),
 'print_files':('id',SOURCE,'stored_path','org_id','created_by','model_id','job_id'),
 'print_archives':('id',SOURCE,'project_id','print_file_id','file_path','org_id','ownership_state','job_id','print_job_id','printer_id','user_id'),
}


def _members_valid(n='NEW'):
    owner=owner_sql('o','a');fileowner=owner_sql('o','f')
    return f"(SELECT count(*) FROM {MEMBER} m WHERE m.operation_id={n}.operation_id)={n}.declared_members AND NOT EXISTS(SELECT 1 FROM {MEMBER} m JOIN {OP} o USING(operation_id) LEFT JOIN print_archives a ON a.id=m.archive_birth_id AND a.{SOURCE}=m.member_nonce LEFT JOIN print_files f ON f.id=m.file_birth_id AND f.{SOURCE}=m.member_nonce WHERE m.operation_id={n}.operation_id AND (m.member_index>={n}.declared_members OR a.id IS NULL OR ({owner}) IS NOT TRUE OR a.project_id IS DISTINCT FROM {n}.project_birth_id OR a.ownership_state IS DISTINCT FROM 'known' OR a.job_id IS NOT NULL OR a.print_job_id IS NOT NULL OR a.printer_id IS NOT NULL OR a.user_id IS NOT NULL OR (m.expected_relative_path IS NOT NULL AND (f.id IS NULL OR ({fileowner}) IS NOT TRUE OR f.created_by IS DISTINCT FROM o.requested_by OR f.model_id IS NOT NULL OR f.job_id IS NOT NULL OR m.sealed_revision IS NULL OR m.verified_sha256 IS DISTINCT FROM m.expected_sha256 OR m.verified_size IS DISTINCT FROM m.expected_size OR a.file_path IS DISTINCT FROM f.stored_path OR a.print_file_id IS DISTINCT FROM f.id)) OR (m.expected_relative_path IS NULL AND (a.file_path IS NOT NULL OR a.print_file_id IS NOT NULL))))"


def predicates(pg):
    """Exact row admission/transition predicates; retained validator rechecks them."""
    out={}
    out[ROOT]={'insert':"NEW.state='enrolling' AND NEW.lifecycle_revision=1 AND NEW.cleanup_capability='live_fd_only' AND NEW.created_checkpoint_at IS NULL AND NEW.sealed_checkpoint_at IS NULL AND NEW.failure_checkpoint_at IS NULL",
               'update':"OLD.state='enrolling' AND "+equal(tuple(n for n in columns(ROOT,pg) if n not in ('cleanup_capability','state','lifecycle_revision','terminal_at','reason') and not any(n.startswith(k+'_') for k in ('namespace_created','marker_created','namespace_sealed','marker_sealed','namespace_failure','marker_failure')) and n not in ('created_checkpoint_at','sealed_checkpoint_at','failure_checkpoint_at')))}
    out[ROOT]['update']+=" AND (NEW.cleanup_capability IS NOT DISTINCT FROM OLD.cleanup_capability OR (OLD.state='enrolling' AND OLD.lifecycle_revision=1 AND OLD.cleanup_capability='live_fd_only' AND OLD.sealed_checkpoint_at IS NULL AND NEW.state='active' AND NEW.lifecycle_revision=2 AND NEW.cleanup_capability='birth_verified' AND NEW.sealed_checkpoint_at IS NOT NULL AND NEW.namespace_created_birth_ns IS NOT NULL AND NEW.marker_created_birth_ns IS NOT NULL AND NEW.namespace_sealed_birth_ns=NEW.namespace_created_birth_ns AND NEW.marker_sealed_birth_ns=NEW.marker_created_birth_ns))"
    for phase in ('created','sealed','failure'):
        if phase in ('sealed','failure'):out[ROOT]['update']+=f' AND (NEW.{phase}_checkpoint_at IS NULL OR OLD.created_checkpoint_at IS NOT NULL)'
        fields=tuple(n for n in columns(ROOT,pg) if n.startswith(('namespace_'+phase+'_','marker_'+phase+'_')) or n==phase+'_checkpoint_at')
        out[ROOT]['update']+=' AND (OLD.'+phase+'_checkpoint_at IS NULL OR ('+equal(fields)+'))'
    out[OP]={'insert':f"NEW.state='staging' AND NEW.lifecycle_revision=1 AND NEW.project_birth_id IS NULL AND NEW.directory_inode IS NULL AND NEW.directory_receipt_revision IS NULL AND EXISTS(SELECT 1 FROM {ROOT} r WHERE r.generation=NEW.storage_root_generation AND r.state='active')",
      'update':"OLD.state='staging' AND "+equal(tuple(n for n in columns(OP,pg) if n not in ('required_operation_state','state','lifecycle_revision','terminal_at','reason','project_birth_id','directory_device','directory_inode','directory_kind','directory_receipt_object_key','directory_receipt_domain','directory_receipt_revision')))}
    out[OP]['update']+=" AND (OLD.project_birth_id IS NULL OR NEW.project_birth_id IS NOT DISTINCT FROM OLD.project_birth_id) AND (OLD.directory_inode IS NULL OR ("+equal(('directory_device','directory_inode','directory_kind'))+")) AND (OLD.directory_receipt_revision IS NULL OR ("+equal(('directory_receipt_object_key','directory_receipt_domain','directory_receipt_revision'))+"))"
    out[OP]['update']+=f" AND (NEW.directory_inode IS NULL OR EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key='directory' AND c.checkpoint_domain='birth' AND c.domain_revision=1 AND c.device=NEW.directory_device AND c.inode=NEW.directory_inode AND c.kind=NEW.directory_kind))"
    out[OP]['update']+=f" AND (NEW.project_birth_id IS NULL OR EXISTS(SELECT 1 FROM projects p WHERE p.id=NEW.project_birth_id AND p.{SOURCE}=NEW.project_birth_nonce AND {owner_sql('NEW','p')} AND p.created_by=NEW.requested_by))"
    out[OP]['update']+=f" AND (NEW.state<>'committed' OR ({_members_valid()})) AND (NEW.state NOT IN ('abandoned','quarantined') OR (NOT EXISTS(SELECT 1 FROM projects p WHERE p.{SOURCE}=NEW.project_birth_nonce) AND NOT EXISTS(SELECT 1 FROM {MEMBER} m JOIN print_files f ON f.{SOURCE}=m.member_nonce WHERE m.operation_id=NEW.operation_id) AND NOT EXISTS(SELECT 1 FROM {MEMBER} m JOIN print_archives a ON a.{SOURCE}=m.member_nonce WHERE m.operation_id=NEW.operation_id)))"
    mutable=('required_operation_state','archive_birth_id','file_birth_id','file_device','file_inode','file_kind','verified_size','verified_sha256','sealed_object_key','sealed_domain','sealed_revision')
    out[MEMBER]={'insert':f"EXISTS(SELECT 1 FROM {OP} o WHERE o.operation_id=NEW.operation_id AND o.state='staging' AND NEW.member_index<o.declared_members) AND "+all_null(tuple(n for n in mutable if n!='required_operation_state'),'NEW.'),
      'update':f"EXISTS(SELECT 1 FROM {OP} o WHERE o.operation_id=NEW.operation_id AND o.state='staging') AND "+equal(tuple(n for n in columns(MEMBER,pg) if n not in mutable))+' AND '+' AND '.join(f'(OLD.{n} IS NULL OR NEW.{n} IS NOT DISTINCT FROM OLD.{n})' for n in mutable if n!='required_operation_state')}
    out[MEMBER]['update']+=f" AND (NEW.file_birth_id IS NULL OR EXISTS(SELECT 1 FROM print_files f JOIN {OP} o ON o.operation_id=NEW.operation_id WHERE f.id=NEW.file_birth_id AND f.{SOURCE}=NEW.member_nonce AND {owner_sql('o','f')} AND f.created_by=o.requested_by AND f.model_id IS NULL AND f.job_id IS NULL AND f.stored_path LIKE '%/'||o.relative_directory||'/'||NEW.expected_relative_path))"
    out[MEMBER]['update']+=f" AND (NEW.archive_birth_id IS NULL OR EXISTS(SELECT 1 FROM print_archives a JOIN {OP} o ON o.operation_id=NEW.operation_id WHERE a.id=NEW.archive_birth_id AND a.{SOURCE}=NEW.member_nonce AND {owner_sql('o','a')} AND a.project_id=o.project_birth_id))"
    out[MEMBER]['update']+=f" AND (NEW.sealed_revision IS NULL OR EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key=CAST(NEW.member_index AS TEXT) AND c.checkpoint_domain='content' AND c.domain_revision=1 AND c.device=NEW.file_device AND c.inode=NEW.file_inode AND c.kind=NEW.file_kind AND c.byte_count=NEW.verified_size AND c.digest_sha256=NEW.verified_sha256))"
    out[CHECKPOINT]={'insert':_checkpoint_admission(pg),'update':'0=1'}
    immutable=tuple(n for n in columns(CLEANUP,pg) if n not in ('state','revision','changed_at','completion_at','uncertainty_code','parent_receipt_object_key','parent_receipt_domain','parent_receipt_revision'))
    parent=('parent_receipt_object_key','parent_receipt_domain','parent_receipt_revision')
    out[CLEANUP]={'insert':f"NEW.state='intent' AND NEW.revision=1 AND EXISTS(SELECT 1 FROM {OP} o JOIN {CHECKPOINT} c ON c.operation_id=o.operation_id WHERE o.operation_id=NEW.operation_id AND o.state='abandoned' AND c.object_key=NEW.object_key AND c.checkpoint_domain=NEW.checkpoint_domain AND c.domain_revision=NEW.checkpoint_revision AND c.root_generation=NEW.root_generation AND c.object_birth_nonce=NEW.object_birth_nonce) AND (NEW.object_key<>'directory' OR NOT EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key<>'directory' AND c.checkpoint_domain='birth' AND NOT EXISTS(SELECT 1 FROM {CLEANUP} j WHERE j.operation_id=c.operation_id AND j.object_key=c.object_key AND j.state='complete')))",
       'update':equal(immutable)+" AND ((OLD.state='intent' AND OLD.revision=1 AND NEW.revision=2 AND NEW.state IN ('unlinked','uncertain')) OR (OLD.state='unlinked' AND OLD.revision=2 AND NEW.revision=3 AND NEW.state IN ('complete','uncertain'))) AND (OLD.parent_receipt_revision IS NULL OR ("+equal(parent)+"))"}
    out[CLEANUP]['update']+=f" AND (NEW.revision<>3 OR NEW.object_key='directory' OR EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key='directory' AND c.checkpoint_domain='cleanup_metadata' AND c.domain_revision=NEW.parent_receipt_revision AND c.cleanup_object_key=NEW.object_key AND c.cleanup_transition_revision=2))"
    return out


def _checkpoint_admission(pg):
    n='NEW'; o='o'
    birth=f"((NEW.object_key='directory' AND NEW.object_birth_nonce=o.directory_birth_nonce) OR (NEW.object_key='operation_birth' AND NEW.object_birth_nonce=o.operation_marker_birth_nonce) OR EXISTS(SELECT 1 FROM {MEMBER} m WHERE m.operation_id=o.operation_id AND m.member_index=NEW.member_index AND m.member_nonce=NEW.object_birth_nonce AND m.expected_relative_path IS NOT NULL))"
    base=f"EXISTS(SELECT 1 FROM {OP} o WHERE o.operation_id=NEW.operation_id AND NEW.root_generation=o.storage_root_generation AND {birth} AND ((NEW.checkpoint_domain='cleanup_metadata' AND o.state='abandoned') OR (NEW.checkpoint_domain<>'cleanup_metadata' AND o.state='staging')))"
    base+=f" AND EXISTS(SELECT 1 FROM {ROOT} r WHERE r.generation=NEW.root_generation AND r.state='active' AND r.namespace_sealed_device=NEW.device AND r.namespace_sealed_service_uid=NEW.service_uid)"
    base+=f" AND (SELECT count(*) FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id)<647"
    inherited=f"EXISTS(SELECT 1 FROM {CHECKPOINT} b WHERE b.operation_id=NEW.operation_id AND b.object_key=NEW.object_key AND b.checkpoint_domain='birth' AND b.domain_revision=1 AND "+' AND '.join(f'b.{v} IS NOT DISTINCT FROM NEW.{v}' for v in ('root_generation','object_birth_nonce','device','inode','kind','mode','service_uid','birth_ns'))+')'
    null_pre=all_null(('predecessor_object_key','predecessor_domain','predecessor_revision'),'NEW.')
    null_rel=all_null(('related_object_key','related_domain','related_revision'),'NEW.')
    birth_case=f"NEW.checkpoint_domain='birth' AND {null_pre} AND {null_rel} AND (NEW.kind='directory' OR (NEW.byte_count=0 AND NEW.digest_sha256='{EMPTY_SHA}'))"
    content=f"NEW.checkpoint_domain='content' AND {inherited} AND NEW.predecessor_object_key=NEW.object_key AND {null_rel} AND NOT EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key=NEW.object_key AND c.checkpoint_domain='failure_identity') AND ((NEW.object_key<>'directory' AND NEW.predecessor_domain='birth' AND NEW.predecessor_revision=1) OR (NEW.object_key='directory' AND ((NEW.predecessor_domain='birth' AND NEW.predecessor_revision=1 AND NOT EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.checkpoint_domain='owned_metadata')) OR (NEW.predecessor_domain='owned_metadata' AND NEW.predecessor_revision=(SELECT count(*) FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.checkpoint_domain='owned_metadata')))))"
    content+=f" AND ((NEW.object_key='directory' AND EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key='operation_birth' AND c.checkpoint_domain='content' AND c.domain_revision=1) AND (SELECT count(*) FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.checkpoint_domain='owned_metadata')=(SELECT count(*) FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.checkpoint_domain='birth' AND c.object_key NOT IN ('directory','operation_birth')) AND NOT EXISTS(SELECT 1 FROM {MEMBER} m WHERE m.operation_id=NEW.operation_id AND m.expected_relative_path IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=m.operation_id AND c.object_key=CAST(m.member_index AS TEXT) AND c.checkpoint_domain='content' AND c.domain_revision=1))) OR (NEW.object_key='operation_birth' AND EXISTS(SELECT 1 FROM {OP} o WHERE o.operation_id=NEW.operation_id AND NEW.byte_count=o.operation_marker_size AND NEW.digest_sha256=o.operation_marker_sha256)) OR EXISTS(SELECT 1 FROM {MEMBER} m WHERE m.operation_id=NEW.operation_id AND m.member_index=NEW.member_index AND m.expected_size=NEW.byte_count AND m.expected_sha256=NEW.digest_sha256))"
    failure=f"NEW.checkpoint_domain='failure_identity' AND {inherited} AND NEW.predecessor_object_key=NEW.object_key AND NEW.predecessor_revision=1 AND ((NEW.predecessor_domain='content' AND EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key=NEW.object_key AND c.checkpoint_domain='content')) OR (NEW.predecessor_domain='birth' AND NOT EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key=NEW.object_key AND c.checkpoint_domain='content'))) AND {null_rel} AND NOT EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.object_key=NEW.object_key AND c.checkpoint_domain='content' AND (c.byte_count IS DISTINCT FROM NEW.byte_count OR c.digest_sha256 IS DISTINCT FROM NEW.digest_sha256))"
    owned=f"NEW.checkpoint_domain='owned_metadata' AND {inherited} AND NEW.domain_revision=(SELECT count(*)+1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.checkpoint_domain='owned_metadata') AND NEW.related_domain='birth' AND NEW.related_revision=1 AND NEW.related_object_key NOT IN ('directory','operation_birth') AND NOT EXISTS(SELECT 1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.checkpoint_domain='owned_metadata' AND c.related_object_key=NEW.related_object_key)"
    owned+=f" AND NEW.predecessor_object_key='directory' AND ((NEW.domain_revision=1 AND NEW.predecessor_domain='birth' AND NEW.predecessor_revision=1) OR (NEW.domain_revision>1 AND NEW.predecessor_domain='owned_metadata' AND NEW.predecessor_revision=NEW.domain_revision-1))"
    cleanup=f"NEW.checkpoint_domain='cleanup_metadata' AND {inherited} AND NEW.domain_revision=(SELECT count(*)+1 FROM {CHECKPOINT} c WHERE c.operation_id=NEW.operation_id AND c.checkpoint_domain='cleanup_metadata') AND EXISTS(SELECT 1 FROM {CLEANUP} j WHERE j.operation_id=NEW.operation_id AND j.object_key=NEW.cleanup_object_key AND j.state='unlinked' AND j.revision=2 AND j.parent_receipt_revision=NEW.domain_revision AND j.root_generation=NEW.root_generation)"
    cleanup+=f" AND NEW.predecessor_object_key='directory' AND ((NEW.domain_revision=1 AND NEW.predecessor_domain IN ('content','failure_identity') AND NEW.predecessor_revision=1) OR (NEW.domain_revision>1 AND NEW.predecessor_domain='cleanup_metadata' AND NEW.predecessor_revision=NEW.domain_revision-1))"
    return base+' AND (('+') OR ('.join((birth_case,content,failure,owned,cleanup))+'))'


def source_predicates(table,pg):
    insert=f"(NEW.{SOURCE} IS NULL AND NOT EXISTS(SELECT 1 FROM {table} p WHERE p.id=NEW.id AND p.{SOURCE} IS NOT NULL)) OR (NEW.{SOURCE} IS NOT NULL AND {nonce_check('NEW.'+SOURCE,pg)} AND NOT EXISTS(SELECT 1 FROM {table} p WHERE p.id=NEW.id OR p.{SOURCE}=NEW.{SOURCE}) AND {_source_binding(table)})"
    update=f"NEW.{SOURCE} IS NOT DISTINCT FROM OLD.{SOURCE} AND (OLD.{SOURCE} IS NULL OR ({equal(SOURCE_IMMUTABLE[table])}))"
    return {'insert':insert,'update':update}


def sqlite_triggers():
    result={}
    for table,key in ((ROOT,'root'),(OP,'operation'),(MEMBER,'member'),(CHECKPOINT,'checkpoint'),(CLEANUP,'cleanup')):
        p=predicates(False)[table]
        for action in ('insert','update','delete'):
            name='zip029_'+key+'_'+action
            anti='NOT EXISTS(SELECT 1 FROM '+table+' p WHERE '+('p.operation_id=NEW.operation_id AND p.object_key=NEW.object_key AND p.checkpoint_domain=NEW.checkpoint_domain AND p.domain_revision=NEW.domain_revision' if table==CHECKPOINT else 'p.operation_id=NEW.operation_id AND p.object_key=NEW.object_key' if table==CLEANUP else 'p.operation_id=NEW.operation_id AND p.member_index=NEW.member_index OR p.member_nonce=NEW.member_nonce' if table==MEMBER else 'p.operation_id=NEW.operation_id OR p.relative_directory=NEW.relative_directory OR p.project_birth_nonce=NEW.project_birth_nonce OR p.directory_birth_nonce=NEW.directory_birth_nonce OR p.operation_marker_birth_nonce=NEW.operation_marker_birth_nonce' if table==OP else 'p.generation=NEW.generation OR p.command_id=NEW.command_id OR p.enrollment_nonce=NEW.enrollment_nonce OR p.namespace_birth_nonce=NEW.namespace_birth_nonce OR p.marker_birth_nonce=NEW.marker_birth_nonce')+')'
            condition=(anti+' AND ('+p['insert']+')') if action=='insert' else p['update'] if action=='update' else '0=1'
            result[name]=f"CREATE TRIGGER {name} BEFORE {action.upper()} ON {table} BEGIN SELECT CASE WHEN NOT ({condition}) THEN RAISE(ABORT,'project import row refused') END; END"
    for table,key in zip(SOURCE_TABLES,('projects','archives','files')):
        for action,predicate in source_predicates(table,False).items():
            name='zip029_'+key+'_'+action
            result[name]=f"CREATE TRIGGER {name} BEFORE {action.upper()} ON {table} BEGIN SELECT CASE WHEN NOT ({predicate}) THEN RAISE(ABORT,'project import source refused') END; END"
    return result


def pg_functions():
    result={}
    for table,key in ((ROOT,'root'),(OP,'operation'),(MEMBER,'member'),(CHECKPOINT,'checkpoint'),(CLEANUP,'cleanup')):
        p=predicates(True)[table]
        parent_lock=(f'PERFORM 1 FROM public.{OP} WHERE operation_id=NEW.operation_id FOR UPDATE NOWAIT; ' if table in (MEMBER,CHECKPOINT,CLEANUP) else '')
        result['odin_project_import_'+key+'_guard']=f"BEGIN {parent_lock}IF TG_OP='DELETE' THEN RAISE EXCEPTION 'project import history is immutable' USING ERRCODE='23514'; END IF; IF TG_OP='INSERT' THEN IF NOT ({p['insert']}) THEN RAISE EXCEPTION 'project import row refused' USING ERRCODE='23514'; END IF; ELSE IF NOT ({p['update']}) THEN RAISE EXCEPTION 'project import row refused' USING ERRCODE='23514'; END IF; END IF; RETURN NEW; END"
    for table,key in zip(SOURCE_TABLES,('projects','archives','files')):
        p=source_predicates(table,True)
        result['odin_project_import_'+key+'_guard']=f"BEGIN IF TG_OP='INSERT' THEN IF NOT ({p['insert']}) THEN RAISE EXCEPTION 'project import source refused' USING ERRCODE='23514'; END IF; ELSE IF NOT ({p['update']}) THEN RAISE EXCEPTION 'project import source refused' USING ERRCODE='23514'; END IF; END IF; RETURN NEW; END"
    result['odin_project_import_truncate_guard']="BEGIN RAISE EXCEPTION 'project import history is immutable' USING ERRCODE='23514'; END"
    names='|'.join(sorted((*TABLES,*SOURCE_TABLES),key=len,reverse=True))
    return {name:re.sub(r'(?<![A-Za-z0-9_.])('+names+r')(?![A-Za-z0-9_])',r'public.\1',body) for name,body in result.items()}


def pg_triggers():
    result={}
    for table,key in ((ROOT,'root'),(OP,'operation'),(MEMBER,'member'),(CHECKPOINT,'checkpoint'),(CLEANUP,'cleanup')):
        name='zip029_'+key+'_row';result[(table,name)]=f'CREATE TRIGGER {name} BEFORE INSERT OR DELETE OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION odin_project_import_{key}_guard()'
        name='zip029_'+key+'_truncate';result[(table,name)]=f'CREATE TRIGGER {name} BEFORE TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION odin_project_import_truncate_guard()'
    for table,key in zip(SOURCE_TABLES,('projects','archives','files')):
        name='zip029_'+key+'_row';result[(table,name)]=f'CREATE TRIGGER {name} BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION odin_project_import_{key}_guard()'
    return result


def apply(connection):
    if connection.dialect.name=='sqlite':
        raise ProjectImportMaintenanceRequired('029 requires separately qualified SQLite maintenance before any transaction')
    if connection.dialect.name!='postgresql' or not connection.in_transaction() or connection.connection.driver_connection.autocommit:
        _refuse()
    setting=int(connection.exec_driver_sql("SELECT setting FROM pg_settings WHERE name='lock_timeout'").scalar_one())
    if setting<0:_refuse()
    if setting==0 or setting>2000:connection.exec_driver_sql("SELECT set_config('lock_timeout','2000ms',true)")
    for table in sorted(SOURCE_TABLES):
        if connection.exec_driver_sql('SELECT current_schema()').scalar_one()!='public':_refuse()
        row=connection.exec_driver_sql("SELECT to_regclass(%s)::oid,to_regclass(%s)::oid",(table,'public.'+table)).one()
        if row[0] is None or row[0]!=row[1]:_refuse()
        connection.exec_driver_sql('LOCK TABLE public.'+table+' IN ACCESS EXCLUSIVE MODE NOWAIT')
    footprint=any(inspect(connection).has_table(t,schema='public') for t in TABLES) or any(SOURCE in {c['name'] for c in inspect(connection).get_columns(t,schema='public')} for t in SOURCE_TABLES)
    if footprint:
        from core.project_import_validation import validate
        validate(connection);return
    for table in TABLES:
        row=connection.exec_driver_sql('SELECT to_regclass(%s)::oid,to_regclass(%s)::oid',(table,'public.'+table)).one()
        if row[0]!=row[1]:_refuse()
        _execute_closed_ddl(connection,ddl(table,True,foreign=False).replace('CREATE TABLE '+table,'CREATE TABLE public.'+table,1))
    for table in TABLES:
        for constraint in definition(True)[table][1]:
            if constraint.startswith('FOREIGN KEY'):connection.exec_driver_sql('ALTER TABLE public.'+table+' ADD '+re.sub(r'REFERENCES (\w+)',r'REFERENCES public.\1',constraint))
    for table in SOURCE_TABLES:
        connection.exec_driver_sql('ALTER TABLE public.'+table+' ADD COLUMN '+SOURCE+' TEXT')
        connection.exec_driver_sql('ALTER TABLE public.'+table+' ADD CONSTRAINT zip029_'+table+'_source_key UNIQUE(id,'+SOURCE+')')
        target=OP if table=='projects' else MEMBER;idcol='project_birth_id' if table=='projects' else 'archive_birth_id' if table=='print_archives' else 'file_birth_id';noncecol='project_birth_nonce' if table=='projects' else 'member_nonce'
        connection.exec_driver_sql(f'ALTER TABLE public.{table} ADD CONSTRAINT zip029_{table}_source_binding FOREIGN KEY(id,{SOURCE}) REFERENCES public.{target}({idcol},{noncecol}) ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED')
    for name,body in pg_functions().items():_execute_closed_ddl(connection,'CREATE FUNCTION public.'+name+'() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog, public AS $$'+body+'$$')
    for sql in pg_triggers().values():connection.exec_driver_sql(sql)
    from core.project_import_validation import validate
    validate(connection)
