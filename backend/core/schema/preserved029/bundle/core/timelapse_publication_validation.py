"""Read-only retained journal proof. Never infer current permission or file bytes."""
import stat
from sqlalchemy import inspect,func,select
from core.schema.migrator import MigrationError
from core.timelapse_ownership_schema import anchors
from core.timelapse_retirement_schema import intent,objects
from core.timelapse_publication_schema import (TABLES,lifetimes,versions,namespaces,bindings,publications,facts,retained)
from core.timelapse_manifest import _parts,MAX_OBJECTS
from core.timelapse_rename import _component

LIMIT=2**63-1
STABLE=('device','inode','uid','gid','mode')
FILE=STABLE+('link_count','byte_count','mtime_ns','ctime_ns')
PROVENANCE=('printer_id','scheduled_job_id','owner_state','owner_org_id','ownership_source','print_file_id','education_submission_id','capture_printer_org_id','capture_printer_shared')
EMPTY='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'


def refuse():
    raise MigrationError('Publication retained authority differs') from None


def number(value,minimum=0):
    if type(value) is not int or not minimum<=value<=LIMIT:refuse()


def digest(value):
    if type(value) is not str or len(value)!=64 or any(ch not in '0123456789abcdef' for ch in value):refuse()


def one(db,table,key,value):
    row=db.execute(table.select().where(table.c[key]==value)).mappings().first()
    if row is None:refuse()
    return dict(row)


def owner(db,nonce):
    _component(nonce);row=one(db,anchors,'operation_id',nonce)
    if row['owner_state'] not in ('known','shared'):refuse()
    _parts(row['filename'])
    for key in ('timelapse_id','printer_id','scheduled_job_id','print_file_id'):number(row[key],1)
    if row['owner_state']=='known':number(row['owner_org_id'],1)
    for key in ('education_submission_id','capture_printer_org_id'):
        if row[key] is not None:number(row[key],1)
    return row


def lifetime(db,nonce):
    value=one(db,lifetimes,'lifetime_nonce',nonce);root=owner(db,nonce)
    initial=one(db,versions,'version_nonce',nonce)
    if initial['lifetime_nonce']!=nonce:refuse()
    if (value['original_timelapse_id'],value['original_printer_id'])!=(root['timelapse_id'],root['printer_id']):refuse()
    for key in STABLE:number(value['root_'+key],1 if key=='inode' else 0)
    if value['root_mode']&61440!=stat.S_IFDIR or value['root_mode']&18:refuse()
    return value,root


def version(db,nonce):
    mapping=one(db,versions,'version_nonce',nonce);current=owner(db,nonce)
    original,base=lifetime(db,mapping['lifetime_nonce'])
    if any(current[key]!=base[key] for key in PROVENANCE) or current['timelapse_id']!=base['timelapse_id']:refuse()
    if nonce!=mapping['lifetime_nonce']:
        count=db.execute(select(func.count()).select_from(publications).where(publications.c.result_nonce==nonce,publications.c.kind!='frame')).scalar_one()
        if count!=1:refuse()
    return mapping,current,original


def directory(row,root,*,private=False):
    if row['kind']!='directory':refuse()
    for key in STABLE:number(row[key],1 if key=='inode' else 0)
    if row['mode']&61440!=stat.S_IFDIR or row['mode']&18 or (private and row['mode']!=16832):refuse()
    if any(row[key]!=root['root_'+key] for key in ('device','uid','gid')):refuse()
    if any(row[key] is not None for key in ('link_count','byte_count','mtime_ns','ctime_ns','digest_sha256')):refuse()


def file_fact(row,root):
    if row['kind']!='file':refuse()
    for key in FILE:number(row[key],1 if key=='inode' else 0)
    if row['mode']&61440!=stat.S_IFREG or row['mode']&18 or row['link_count']!=1:refuse()
    if any(row[key]!=root['root_'+key] for key in ('device','uid','gid')):refuse()
    digest(row['digest_sha256'])


def namespace(db,record):
    _component(record['namespace_id']);root,_=lifetime(db,record['lifetime_nonce'])
    if record['relative_path']!='frames_'+record['namespace_id']:refuse()
    _component(record['relative_path'])
    present=[record[key] is not None for key in STABLE]
    if any(present) and not all(present):refuse()
    if all(present):
        check=dict(record,kind='directory',link_count=None,byte_count=None,mtime_ns=None,ctime_ns=None,digest_sha256=None)
        directory(check,root,private=True)
        if record['inode']==root['root_inode']:refuse()
    elif record['phase'] not in ('planned','blocked'):refuse()
    if record['phase']=='planned' and any(present):refuse()
    return root


def bind(db,record):
    number(record['next_number'],1)
    target=one(db,namespaces,'namespace_id',record['namespace_id']);namespace(db,target)
    if target['lifetime_nonce']!=record['lifetime_nonce'] or target['phase'] not in ('bound','retired'):refuse()
    return target


def parent_paths(path):
    parts=_parts(path)
    return {'/'.join(parts[:index]) for index in range(1,len(parts))}


def operation(db,op):
    from core.timelapse_recipes import recipe_from_row
    recipe_from_row(op)
    _component(op['operation_id']);_parts(op['source_filename']);_parts(op['destination_path'])
    number(op['source_timelapse_id'],1);number(op['source_printer_id'],1);number(op['result_tid'],1);number(op['revision'],1)
    if op['actor_id'] is not None:number(op['actor_id'],1)
    source_map,source,root=version(db,op['source_nonce']);result_map,result,result_root=version(db,op['result_nonce'])
    if source_map['lifetime_nonce']!=op['source_lifetime'] or result_map['lifetime_nonce']!=op['result_lifetime']:refuse()
    if (source['timelapse_id'],source['printer_id'],source['filename'])!=(op['source_timelapse_id'],op['source_printer_id'],op['source_filename']):refuse()
    if result['timelapse_id']!=op['result_tid'] or any(source[key]!=result[key] for key in PROVENANCE):refuse()
    if any(result_root['root_'+key]!=root['root_'+key] for key in STABLE):refuse()
    if op['private_directory']!='publish_'+op['operation_id']:refuse()
    _component(op['private_directory']);_component(op['private_name'])
    if op['kind']=='frame':
        if (op['result_nonce'],op['result_lifetime'],op['result_tid'])!=(op['source_nonce'],op['source_lifetime'],op['source_timelapse_id']):refuse()
        binding=one(db,bindings,'lifetime_nonce',op['source_lifetime']);space=bind(db,binding)
        number(op['frame_number'],1);number(op['payload_byte_count'],1);digest(op['payload_digest_sha256'])
        if op['payload_byte_count']>16777216 or op['frame_number']>=binding['next_number'] or space['namespace_id']!=op['frame_namespace_id']:refuse()
        expected=space['relative_path']+'/frame_'+str(op['frame_number']).zfill(6)+'.jpg'
        if op['destination_path']!=expected:refuse()
    else:
        if result['filename']!=op['destination_path'] or op['result_nonce']==op['source_nonce']:refuse()
        # Every non-frame result version belongs to exactly one operation.
        count=db.execute(select(func.count()).select_from(publications).where(publications.c.result_nonce==op['result_nonce'],publications.c.kind!='frame')).scalar_one()
        if count!=1:refuse()
        if op['kind'] in ('encode','trim'):
            if op['result_lifetime']!=op['source_lifetime'] or op['result_tid']!=op['source_timelapse_id']:refuse()
        elif op['kind']=='speed':
            if op['result_lifetime']!=op['result_nonce'] or op['result_lifetime']==op['source_lifetime'] or op['result_tid']==op['source_timelapse_id']:refuse()
        else:refuse()
    rows=[dict(row) for row in db.execute(facts.select().where(facts.c.operation_id==op['operation_id']).limit(MAX_OBJECTS+1)).mappings()]
    if not 1<=len(rows)<=MAX_OBJECTS:refuse()
    grouped={role:[] for role in ('root','source','source_parent','private_root','private_creation','output','output_parent')}
    for row in rows:
        if row['role'] not in grouped:refuse()
        if row['relative_path']:_parts(row['relative_path'])
        if row['kind']=='directory':directory(row,root,private=row['role']=='private_root')
        else:file_fact(row,root)
        grouped[row['role']].append(row)
    directories={}
    for row in rows:
        if row['kind']=='directory':
            identity=tuple(row[key] for key in STABLE)
            if row['relative_path'] in directories and directories[row['relative_path']]!=identity:refuse()
            directories[row['relative_path']]=identity
    roots=grouped['root']
    if len(roots)!=1 or roots[0]['relative_path']!='' or any(roots[0][key]!=root['root_'+key] for key in STABLE):refuse()
    sources=grouped['source'];parents=grouped['source_parent'];output_parents=grouped['output_parent']
    if op['kind']=='frame':
        if sources or len(parents)!=1 or parents[0]['relative_path']!=space['relative_path'] or any(parents[0][key]!=space[key] for key in STABLE):refuse()
    else:
        if not sources:refuse()
        paths={row['relative_path']:row for row in sources}
        if len(paths)!=len(sources):refuse()
        if op['kind'] in ('trim','speed'):
            if len(sources)!=1 or sources[0]['kind']!='file' or sources[0]['relative_path']!=op['source_filename']:refuse()
        else:
            binding=one(db,bindings,'lifetime_nonce',op['source_lifetime']);capture=bind(db,binding)
            if any(row['kind']!='file' or not row['relative_path'].startswith(capture['relative_path']+'/frame_') or not row['relative_path'].endswith('.jpg') for row in sources):refuse()
            # Numeric, uniquely ordered admitted frames; no unbound legacy files.
            for row in sources:
                stem=row['relative_path'].removeprefix(capture['relative_path']+'/frame_').removesuffix('.jpg')
                if not stem.isascii() or not stem.isdigit() or not 1<=int(stem)<binding['next_number'] or stem!=str(int(stem)).zfill(6):refuse()
                prior=db.execute(publications.select().where(publications.c.kind=='frame',publications.c.frame_namespace_id==capture['namespace_id'],publications.c.frame_number==int(stem))).mappings().first()
                if prior is None or prior['phase']!='complete' or prior['payload_byte_count']!=row['byte_count'] or prior['payload_digest_sha256']!=row['digest_sha256']:refuse()
                original=db.execute(facts.select().where(facts.c.operation_id==prior['operation_id'],facts.c.role=='output')).mappings().first()
                if original is None or any(row[key]!=original[key] for key in STABLE+('link_count','byte_count','mtime_ns','digest_sha256')) or row['ctime_ns']!=prior['published_ctime_ns']:refuse()
        required=set().union(*(parent_paths(row['relative_path']) for row in sources))
        if {row['relative_path'] for row in parents}!=required or len(parents)!=len(required):refuse()
        total=sum(row['byte_count'] for row in sources if row['kind']=='file')
        if total>32*1024**3:refuse()
    destination_parents=parent_paths(op['destination_path']) or {''}
    if {row['relative_path'] for row in output_parents}!=destination_parents or len(output_parents)!=len(destination_parents):refuse()
    for parent in output_parents:
        if not parent['relative_path'] and any(parent[key]!=roots[0][key] for key in STABLE):refuse()
        if op['kind']=='frame' and any(parent[key]!=space[key] for key in STABLE):refuse()
    private=grouped['private_root'];creation=grouped['private_creation'];output=grouped['output']
    if op['phase']=='admitted':
        if private or creation or output:refuse()
    else:
        if len(private)!=1 or len(creation)!=1 or private[0]['relative_path']!=op['private_directory'] or creation[0]['relative_path']!=op['private_name']:refuse()
        if private[0]['inode']==root['root_inode'] or creation[0]['mode']!=33152 or creation[0]['byte_count']!=0 or creation[0]['digest_sha256']!=EMPTY:refuse()
        if any(creation[0]['inode']==row['inode'] and creation[0]['device']==row['device'] for row in sources):refuse()
        if op['phase'] in ('private_created','worker_admitted'):
            if output:refuse()
        else:
            if len(output)!=1 or output[0]['relative_path']!=op['private_name'] or any(output[0][key]!=creation[0][key] for key in STABLE):refuse()
            if not 1<=output[0]['byte_count']<=32*1024**3:refuse()
            if op['kind']=='frame' and (output[0]['byte_count'],output[0]['digest_sha256'])!=(op['payload_byte_count'],op['payload_digest_sha256']):refuse()
    if op['published_ctime_ns'] is not None:number(op['published_ctime_ns'])
    if op['kind']=='trim' and op['phase']=='complete':
        reference=one(db,retained,'version_nonce',op['source_nonce'])
        if (reference['lifetime_nonce'],reference['retained_by_operation'],reference['filename'])!=(op['source_lifetime'],op['operation_id'],op['source_filename']):refuse()


def validate_retained(db):
    present={table.name for table in TABLES if inspect(db).has_table(table.name)}
    if not present:return
    if len(present)!=len(TABLES):
        if any(db.execute(table.select().limit(1)).first() is not None for table in TABLES if table.name in present):refuse()
        return
    try:
        for row in db.execute(lifetimes.select()).mappings():lifetime(db,row['lifetime_nonce'])
        for row in db.execute(versions.select()).mappings():version(db,row['version_nonce'])
        for row in db.execute(namespaces.select()).mappings():namespace(db,dict(row))
        for row in db.execute(bindings.select()).mappings():bind(db,dict(row))
        for row in db.execute(publications.select()).mappings():operation(db,dict(row))
        for row in db.execute(retained.select()).mappings():
            op=one(db,publications,'operation_id',row['retained_by_operation']);mapping,anchor,_=version(db,row['version_nonce'])
            if op['kind']!='trim' or op['phase']!='complete' or (mapping['lifetime_nonce'],anchor['filename'],op['source_nonce'],op['source_lifetime'])!=(row['lifetime_nonce'],row['filename'],row['version_nonce'],row['lifetime_nonce']):refuse()
            if row['state']=='retired':
                completed=one(db,intent,'operation_id',row['retire_operation_id'])
                retirement_map,retirement_owner,retirement_root=version(db,row['retire_operation_id'])
                planned=db.execute(objects.select().where(objects.c.operation_id==row['retire_operation_id'],objects.c.relative_path==row['filename'])).mappings().first()
                original=db.execute(facts.select().where(facts.c.operation_id==op['operation_id'],facts.c.role=='source',facts.c.relative_path==row['filename'])).mappings().first()
                if planned is not None and planned['state']=='retired':
                    from core.timelapse_recovery_schema import moves
                    from core.timelapse_unlink_schema import unlinks
                    movement=db.execute(moves.select().where(moves.c.operation_id==row['retire_operation_id'],moves.c.object_index==planned['object_index'])).mappings().first()
                    removal=db.execute(unlinks.select().where(unlinks.c.operation_id==row['retire_operation_id'],unlinks.c.object_index==planned['object_index'])).mappings().first()
                    if original is None or movement is None or removal is None or movement['phase']!='staged' or movement['verified_ctime_ns'] is None or removal['phase']!='retired' or movement['digest_sha256']!=original['digest_sha256'] or any(planned[key]!=original[key] for key in FILE):refuse()
                if completed['state']!='complete' or retirement_map['lifetime_nonce']!=row['lifetime_nonce'] or (completed['timelapse_id'],completed['printer_id'],completed['filename'],completed['owner_state'],completed['owner_org_id'])!=(retirement_owner['timelapse_id'],retirement_owner['printer_id'],retirement_owner['filename'],retirement_owner['owner_state'],retirement_owner['owner_org_id']) or (completed['root_device'],completed['root_inode'])!=(retirement_root['root_device'],retirement_root['root_inode']) or planned is None or planned['kind']!='file' or planned['state'] not in ('retired','originally_absent'):refuse()
    except MigrationError:raise
    except Exception:refuse()
