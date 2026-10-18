"""Independent 029 schema and retained-data validation, never FD qualification."""
import re
from uuid import UUID
from sqlalchemy import inspect,text
from core import project_import_schema as schema
from core.schema.migrator import MigrationError


def _refuse():raise MigrationError('Project import retained schema or data differs') from None

def normal(value):
    return ''.join(piece if piece.startswith("'") else re.sub(r'\s+','',piece).lower()
                   for piece in re.findall(r"'(?:''|[^'])*'|[^']+",str(value))).rstrip(';')


def _foreign_expected(table,pg):
    result=[]
    for constraint in schema.definition(pg)[table][1]:
        match=re.fullmatch(r'FOREIGN KEY\(([^)]+)\) REFERENCES (\w+)\(([^)]+)\) ON UPDATE (RESTRICT|NO ACTION) ON DELETE (RESTRICT|NO ACTION)( DEFERRABLE INITIALLY DEFERRED)?',constraint)
        if match:
            local,target,remote,update,delete,deferred=match.groups();result.append((tuple(local.split(',')),target,tuple(remote.split(',')),bool(deferred),'r' if update=='RESTRICT' else 'a','r' if delete=='RESTRICT' else 'a'))
    return sorted(result)


def _pg_foreign(connection,table,expected):
    rows=connection.execute(text("SELECT c.confrelid,n.nspname,p.relname,ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY x(num,pos) JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=x.num ORDER BY x.pos),ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY x(num,pos) JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=x.num ORDER BY x.pos),c.convalidated,c.condeferrable,c.condeferred,c.confmatchtype,c.confupdtype,c.confdeltype,p.relkind,p.relpersistence FROM pg_constraint c JOIN pg_class p ON p.oid=c.confrelid JOIN pg_namespace n ON n.oid=p.relnamespace WHERE c.conrelid=to_regclass(:table) AND c.contype='f'"),{'table':'public.'+table}).all()
    actual=[]
    for row in rows:
        oid,namespace,target,local,remote,valid,deferred,initial,match,update,delete,kind,persistence=row
        if namespace!='public' or (valid,initial,match,kind,persistence)!=(True,deferred,'s','r','p'):_refuse()
        exact=connection.execute(text('SELECT to_regclass(:name)::oid'),{'name':'public.'+target}).scalar_one()
        if exact!=oid:_refuse()
        actual.append((tuple(local),target,tuple(remote),deferred,update,delete))
    if sorted(actual)!=sorted(expected):_refuse()


def _shape(connection):
    pg=connection.dialect.name=='postgresql';i=inspect(connection)
    for table in schema.TABLES:
        if not i.has_table(table,schema='public' if pg else None):_refuse()
        if not pg:
            actual=connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),{'n':table}).scalar_one_or_none()
            if normal(actual)!=normal(schema.ddl(table,False)):_refuse()
            if connection.exec_driver_sql('PRAGMA foreign_key_check('+table+')').first():_refuse()
            if connection.execute(text("SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name=:n AND sql IS NOT NULL"),{'n':table}).first():_refuse()
            continue
        identity=connection.execute(text("SELECT to_regclass(:n)::oid,to_regclass(:p)::oid,c.relkind,c.relpersistence FROM pg_class c WHERE c.oid=to_regclass(:n)"),{'n':table,'p':'public.'+table}).one_or_none()
        if identity is None or identity[0]!=identity[1] or tuple(identity[2:])!=('r','p'):_refuse()
        ref='zip029_reference_'+table
        schema._execute_closed_ddl(connection,schema.ddl(table,True,foreign=False).replace('CREATE TABLE '+table,'CREATE TEMPORARY TABLE '+ref,1))
        try:
            temp=connection.exec_driver_sql('SELECT pg_my_temp_schema()::regnamespace::text').scalar_one()
            def cols(n,namespace):return [(v['name'],str(v['type']),v['nullable'],normal(v.get('default')),v.get('identity'),v.get('computed')) for v in inspect(connection).get_columns(n,schema=namespace)]
            if cols(table,'public')!=cols(ref,temp):_refuse()
            def keys(n,namespace):return (inspect(connection).get_pk_constraint(n,schema=namespace)['constrained_columns'],sorted(tuple(v['column_names']) for v in inspect(connection).get_unique_constraints(n,schema=namespace)))
            if keys(table,'public')!=keys(ref,temp):_refuse()
            def checks(n):return sorted((normal(row[0]),*row[1:]) for row in connection.execute(text("SELECT pg_get_constraintdef(oid),convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid=to_regclass(:n) AND contype='c'"),{'n':n}))
            if checks('public.'+table)!=checks('pg_temp.'+ref):_refuse()
            if any(not v.get('duplicates_constraint') for v in inspect(connection).get_indexes(table,schema='public')):_refuse()
            states=connection.execute(text("SELECT convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid=to_regclass(:n) AND contype IN ('p','u')"),{'n':'public.'+table}).all()
            if any(tuple(v)!=(True,False,False) for v in states):_refuse()
            states=connection.execute(text('SELECT indisvalid,indisready,indimmediate,indnullsnotdistinct,indpred IS NULL,indexprs IS NULL,indnkeyatts=indnatts FROM pg_index WHERE indrelid=to_regclass(:n)'),{'n':'public.'+table}).all()
            if any(tuple(v)!=(True,True,True,False,True,True,True) for v in states):_refuse()
            _pg_foreign(connection,table,_foreign_expected(table,True))
        finally:connection.exec_driver_sql('DROP TABLE '+ref)
    if pg:
        for name,body in schema.pg_functions().items():
            rows=connection.execute(text("SELECT p.prosrc,p.prosecdef,p.proconfig,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,p.prokind,p.proretset,p.pronargs,p.pronargdefaults,p.procost,p.prorows,p.prosupport::text,pg_get_function_result(p.oid),l.lanname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='public' AND p.proname=:n"),{'n':name}).all()
            if rows!=[(body,False,['search_path=pg_catalog, public'],'v','u',False,False,'f',False,0,0,100.0,0.0,'-','trigger','plpgsql')]:_refuse()
        for (table,name),sql in schema.pg_triggers().items():
            row=connection.execute(text("SELECT pg_get_triggerdef(oid),tgenabled,tgnargs,tgqual IS NULL,tgattr::text,tgconstraint,tgdeferrable,tginitdeferred,encode(tgargs,'hex'),tgfoid FROM pg_trigger WHERE tgrelid=to_regclass(:t) AND tgname=:n"),{'t':'public.'+table,'n':name}).one_or_none()
            function=sql.split('EXECUTE FUNCTION ',1)[1][:-2]
            oid=connection.execute(text('SELECT to_regprocedure(:n)::oid'),{'n':'public.'+function+'()'}).scalar_one()
            if row is None or tuple(row)!=(sql,'O',0,True,'',0,False,False,'',oid):_refuse()
        for table in schema.TABLES:
            actual=set(connection.execute(text('SELECT tgname FROM pg_trigger WHERE tgrelid=to_regclass(:t) AND NOT tgisinternal'),{'t':'public.'+table}).scalars())
            if actual!={n for (t,n) in schema.pg_triggers() if t==table}:_refuse()
    else:
        expected=schema.sqlite_triggers();actual=dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (tbl_name IN ('project_import_root_generations','project_import_operations','project_import_members','project_import_checkpoints','project_import_cleanup') OR name LIKE 'zip029_%')")).all())
        if {k:normal(v) for k,v in actual.items()}!={k:normal(v) for k,v in expected.items()}:_refuse()
    for table in schema.SOURCE_TABLES:
        if pg:
            row=connection.execute(text("SELECT to_regclass(:n)::oid,to_regclass(:p)::oid,c.relkind,c.relpersistence FROM pg_class c WHERE c.oid=to_regclass(:n)"),{'n':table,'p':'public.'+table}).one_or_none()
            if row is None or row[0]!=row[1] or tuple(row[2:])!=('r','p'):_refuse()
        col={v['name']:v for v in i.get_columns(table,schema='public' if pg else None)}.get(schema.SOURCE)
        if not col or str(col['type']).upper()!='TEXT' or not col['nullable'] or col.get('default') is not None or col.get('computed') or col.get('identity'):_refuse()
        target=schema.OP if table=='projects' else schema.MEMBER
        idcol='project_birth_id' if table=='projects' else 'archive_birth_id' if table=='print_archives' else 'file_birth_id';nonce='project_birth_nonce' if table=='projects' else 'member_nonce'
        # Validate only dedicated029 additions; existing source definitions survive unchanged.
        fks=i.get_foreign_keys(table,schema='public' if pg else None)
        selected=[f for f in fks if schema.SOURCE in f['constrained_columns']]
        if len(selected)!=1 or selected[0]['constrained_columns']!=['id',schema.SOURCE] or selected[0]['referred_table']!=target or selected[0]['referred_columns']!=[idcol,nonce]:_refuse()
        options=selected[0].get('options',{})
        if options.get('onupdate')!='RESTRICT' or options.get('ondelete')!='RESTRICT' or options.get('deferrable') is not True or options.get('initially')!='DEFERRED':_refuse()
        if pg:
            row=connection.execute(text("SELECT c.confrelid,n.nspname,p.relname,c.convalidated,c.condeferrable,c.condeferred,c.confmatchtype,c.confupdtype,c.confdeltype FROM pg_constraint c JOIN pg_class p ON p.oid=c.confrelid JOIN pg_namespace n ON n.oid=p.relnamespace WHERE c.conrelid=to_regclass(:t) AND c.contype='f' AND c.conname=:n"),{'t':'public.'+table,'n':'zip029_'+table+'_source_binding'}).one_or_none()
            oid=connection.execute(text('SELECT to_regclass(:n)::oid'),{'n':'public.'+target}).scalar_one()
            if row is None or tuple(row)!=(oid,'public',target,True,True,True,'s','r','r'):_refuse()
        unique=i.get_unique_constraints(table,schema='public' if pg else None)
        if not any(v['column_names']==['id',schema.SOURCE] for v in unique):_refuse()


def _rows(connection,table):
    cursor=connection.execute(text('SELECT * FROM '+table).execution_options(stream_results=True,max_row_buffer=128)).mappings()
    try:
        while True:
            rows=cursor.fetchmany(128)
            if not rows:break
            yield from rows
    finally:cursor.close()


def validate_data(connection):
    pg=connection.dialect.name=='postgresql'
    for table,(_,constraints) in schema.definition(pg).items():
        for predicate in constraints:
            if predicate.startswith('CHECK('):
                if connection.execute(text('SELECT 1 FROM '+table+' WHERE ('+predicate[6:-1]+') IS NOT TRUE LIMIT 1')).first():_refuse()
    for row in _rows(connection,schema.ROOT):
        if row['expected_sha256']!=schema.marker_sha(row['generation']):_refuse()
    for row in _rows(connection,schema.OP):
        if row['operation_marker_sha256']!=schema.marker_sha(row['storage_root_generation'],row['operation_id'],row['directory_birth_nonce']):_refuse()
        count=connection.execute(text('SELECT count(*) FROM '+schema.MEMBER+' WHERE operation_id=:op'),{'op':row['operation_id']}).scalar_one()
        if count>row['declared_members'] or (row['state']=='committed' and count!=row['declared_members']):_refuse()
        if connection.execute(text('SELECT 1 FROM '+schema.MEMBER+' WHERE operation_id=:op AND member_index>=:count LIMIT 1'),{'op':row['operation_id'],'count':row['declared_members']}).first():_refuse()
        if row['state']=='committed':
            for member in connection.execute(text('SELECT * FROM '+schema.MEMBER+' WHERE operation_id=:op'),{'op':row['operation_id']}).mappings():
                if member['archive_birth_id'] is None or (member['expected_relative_path'] is not None and (member['file_birth_id'] is None or member['sealed_revision']!=1 or member['verified_size']!=member['expected_size'] or member['verified_sha256']!=member['expected_sha256'])):_refuse()
        elif row['project_birth_id'] is not None:_refuse()
    for table in schema.SOURCE_TABLES:
        target=schema.OP if table=='projects' else schema.MEMBER
        idcol='project_birth_id' if table=='projects' else 'archive_birth_id' if table=='print_archives' else 'file_birth_id';nonce='project_birth_nonce' if table=='projects' else 'member_nonce'
        sql=f'SELECT 1 FROM {table} s LEFT JOIN {target} b ON b.{idcol}=s.id AND b.{nonce}=s.{schema.SOURCE} '+('' if table=='projects' else 'LEFT JOIN '+schema.OP+' o ON o.operation_id=b.operation_id ')+f"WHERE s.{schema.SOURCE} IS NOT NULL AND (b.{nonce} IS NULL OR "+("b.state<>'committed'" if table=='projects' else "o.state<>'committed'")+') LIMIT 1'
        if connection.execute(text(sql)).first():_refuse()
    _retained_links(connection)
    for row in _rows(connection,schema.CLEANUP):
        if row['object_key']=='directory':continue
        linked=connection.execute(text('SELECT * FROM '+schema.CHECKPOINT+" WHERE operation_id=:op AND object_key='directory' AND checkpoint_domain='cleanup_metadata' AND cleanup_object_key=:key"),{'op':row['operation_id'],'key':row['object_key']}).mappings().all()
        if row['state']=='intent' or (row['state']=='uncertain' and row['revision']==2):
            if linked:_refuse()
        elif len(linked)!=1 or linked[0]['domain_revision']!=row['parent_receipt_revision'] or linked[0]['cleanup_transition_revision']!=2 or linked[0]['root_generation']!=row['root_generation']:_refuse()
    if connection.execute(text('SELECT 1 FROM '+schema.CHECKPOINT+" c LEFT JOIN "+schema.CLEANUP+" j ON j.operation_id=c.operation_id AND j.object_key=c.cleanup_object_key WHERE c.checkpoint_domain='cleanup_metadata' AND (j.object_key IS NULL OR j.state NOT IN ('unlinked','complete','uncertain') OR (j.state='uncertain' AND j.revision<>3) OR j.parent_receipt_revision IS DISTINCT FROM c.domain_revision) LIMIT 1")).first():_refuse()



def _retained_links(connection):
    for source in schema.SOURCE_TABLES:
        if source=='projects':
            sql=f"SELECT 1 FROM projects p JOIN {schema.OP} o ON o.project_birth_nonce=p.project_import_nonce WHERE p.project_import_nonce IS NOT NULL AND (p.id IS DISTINCT FROM o.project_birth_id OR ({schema.owner_sql('o','p')}) IS NOT TRUE OR p.created_by IS DISTINCT FROM o.requested_by) LIMIT 1"
        elif source=='print_files':
            sql=f"SELECT 1 FROM print_files f JOIN {schema.MEMBER} m ON m.member_nonce=f.project_import_nonce JOIN {schema.OP} o USING(operation_id) WHERE f.project_import_nonce IS NOT NULL AND (f.id IS DISTINCT FROM m.file_birth_id OR ({schema.owner_sql('o','f')}) IS NOT TRUE OR f.created_by IS DISTINCT FROM o.requested_by OR f.model_id IS NOT NULL OR f.job_id IS NOT NULL OR f.stored_path NOT LIKE '%/'||o.relative_directory||'/'||m.expected_relative_path) LIMIT 1"
        else:
            sql=f"SELECT 1 FROM print_archives a JOIN {schema.MEMBER} m ON m.member_nonce=a.project_import_nonce JOIN {schema.OP} o USING(operation_id) LEFT JOIN print_files f ON f.id=m.file_birth_id AND f.project_import_nonce=m.member_nonce WHERE a.project_import_nonce IS NOT NULL AND (a.id IS DISTINCT FROM m.archive_birth_id OR ({schema.owner_sql('o','a')}) IS NOT TRUE OR a.ownership_state IS DISTINCT FROM 'known' OR a.project_id IS DISTINCT FROM o.project_birth_id OR a.job_id IS NOT NULL OR a.print_job_id IS NOT NULL OR a.printer_id IS NOT NULL OR a.user_id IS NOT NULL OR (m.expected_relative_path IS NULL AND (a.file_path IS NOT NULL OR a.print_file_id IS NOT NULL)) OR (m.expected_relative_path IS NOT NULL AND (a.print_file_id IS DISTINCT FROM m.file_birth_id OR (f.id IS NOT NULL AND a.file_path IS DISTINCT FROM f.stored_path)))) LIMIT 1"
        if connection.execute(text(sql)).first():_refuse()
    for row in _rows(connection,schema.CHECKPOINT):
        op=connection.execute(text('SELECT * FROM '+schema.OP+' WHERE operation_id=:op'),{'op':row['operation_id']}).mappings().one()
        key=row['object_key'];domain=row['checkpoint_domain']
        if row['root_generation']!=op['storage_root_generation']:_refuse()
        root=connection.execute(text('SELECT * FROM '+schema.ROOT+' WHERE generation=:g'),{'g':row['root_generation']}).mappings().one()
        if row['device']!=root['namespace_sealed_device'] or row['service_uid']!=root['namespace_sealed_service_uid']:_refuse()
        if key=='directory':nonce=op['directory_birth_nonce']
        elif key=='operation_birth':nonce=op['operation_marker_birth_nonce']
        else:
            member=connection.execute(text('SELECT * FROM '+schema.MEMBER+' WHERE operation_id=:op AND member_index=:i'),{'op':row['operation_id'],'i':row['member_index']}).mappings().one()
            if member['expected_relative_path'] is None:_refuse()
            nonce=member['member_nonce']
            if domain=='content' and (row['byte_count']!=member['expected_size'] or row['digest_sha256']!=member['expected_sha256']):_refuse()
        if row['object_birth_nonce']!=nonce:_refuse()
        if key=='operation_birth' and domain=='content' and (row['byte_count']!=70 or row['digest_sha256']!=op['operation_marker_sha256']):_refuse()
        if domain in ('birth','content','failure_identity') and any(row[k] is not None for k in ('related_object_key','related_domain','related_revision')):_refuse()
        if domain=='birth':
            if any(row[k] is not None for k in ('predecessor_object_key','predecessor_domain','predecessor_revision')):_refuse()
            if row['kind']=='regular' and (row['byte_count']!=0 or row['digest_sha256']!=schema.EMPTY_SHA):_refuse()
        else:
            birth=connection.execute(text('SELECT * FROM '+schema.CHECKPOINT+" WHERE operation_id=:op AND object_key=:key AND checkpoint_domain='birth' AND domain_revision=1"),{'op':row['operation_id'],'key':key}).mappings().one()
            if any(row[k]!=birth[k] for k in ('root_generation','object_birth_nonce','device','inode','kind','mode','service_uid','birth_ns')):_refuse()
        if domain=='content':
            if key!='directory' and (row['predecessor_object_key'],row['predecessor_domain'],row['predecessor_revision'])!=(key,'birth',1):_refuse()
            if key=='directory':
                parents=connection.execute(text('SELECT count(*) FROM '+schema.CHECKPOINT+" WHERE operation_id=:op AND checkpoint_domain='owned_metadata'"),{'op':row['operation_id']}).scalar_one()
                expected=('directory','owned_metadata',parents) if parents else ('directory','birth',1)
                if (row['predecessor_object_key'],row['predecessor_domain'],row['predecessor_revision'])!=expected:_refuse()
                if not connection.execute(text('SELECT 1 FROM '+schema.CHECKPOINT+" WHERE operation_id=:op AND object_key='operation_birth' AND checkpoint_domain='content'"),{'op':row['operation_id']}).first():_refuse()
                born=connection.execute(text('SELECT count(*) FROM '+schema.CHECKPOINT+" WHERE operation_id=:op AND checkpoint_domain='birth' AND object_key NOT IN ('directory','operation_birth')"),{'op':row['operation_id']}).scalar_one()
                if born!=parents:_refuse()
                if connection.execute(text('SELECT 1 FROM '+schema.MEMBER+" m WHERE m.operation_id=:op AND m.expected_relative_path IS NOT NULL AND NOT EXISTS(SELECT 1 FROM "+schema.CHECKPOINT+" c WHERE c.operation_id=m.operation_id AND c.object_key=CAST(m.member_index AS TEXT) AND c.checkpoint_domain='content') LIMIT 1"),{'op':row['operation_id']}).first():_refuse()
        if domain=='failure_identity':
            content=connection.execute(text('SELECT * FROM '+schema.CHECKPOINT+" WHERE operation_id=:op AND object_key=:key AND checkpoint_domain='content'"),{'op':row['operation_id'],'key':key}).mappings().one_or_none()
            expected=(key,'content' if content is not None else 'birth',1)
            if (row['predecessor_object_key'],row['predecessor_domain'],row['predecessor_revision'])!=expected:_refuse()
            if content is not None and any(content[k]!=row[k] for k in ('byte_count','digest_sha256')):_refuse()
        if domain=='owned_metadata':
            if row['related_domain']!='birth' or row['related_revision']!=1 or row['related_object_key'] in ('directory','operation_birth'):_refuse()
            if row['predecessor_object_key']!='directory' or (row['domain_revision']==1 and (row['predecessor_domain'],row['predecessor_revision'])!=('birth',1)) or (row['domain_revision']>1 and (row['predecessor_domain'],row['predecessor_revision'])!=('owned_metadata',row['domain_revision']-1)):_refuse()
    for op in _rows(connection,schema.OP):
        rows=connection.execute(text('SELECT checkpoint_domain,domain_revision,related_object_key FROM '+schema.CHECKPOINT+' WHERE operation_id=:op'),{'op':op['operation_id']}).all()
        if len(rows)>647:_refuse()
        for domain in ('owned_metadata','cleanup_metadata'):
            selected=[r for r in rows if r[0]==domain]
            if sorted(r[1] for r in selected)!=list(range(1,len(selected)+1)):_refuse()
        owned=[r[2] for r in rows if r[0]=='owned_metadata']
        if len(owned)!=len(set(owned)):_refuse()


def validate(connection):
    if connection.dialect.name not in ('sqlite','postgresql') or not connection.in_transaction():_refuse()
    if connection.dialect.name=='sqlite':
        driver=connection.connection.driver_connection
        if not driver.in_transaction or driver.execute('PRAGMA foreign_keys').fetchone()[0]!=1 or driver.execute('PRAGMA ignore_check_constraints').fetchone()[0]!=0:_refuse()
    _shape(connection)
    validate_data(connection)
