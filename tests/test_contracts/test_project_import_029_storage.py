"""Owned fresh SQLite SQL contract only: no gate/rebuild, FD permit or HTTP auth."""
from uuid import uuid4
import hashlib
import pytest
from sqlalchemy import create_engine,event,text
from sqlalchemy.exc import IntegrityError
from core import project_import_schema as s
from core.project_import_validation import validate,validate_data
from core.schema.migrator import MigrationError


def insert(db,table,values):
    names=tuple(values)
    return db.exec_driver_sql('INSERT INTO '+table+'('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',tuple(values.values()))

def update(db,table,values,where,parameters):
    return db.exec_driver_sql('UPDATE '+table+' SET '+','.join(n+'=?' for n in values)+' WHERE '+where,(*values.values(),*parameters))


def fresh_contract(db):
    # Source DDL is an independent minimal fixture, never a rebuild of app tables.
    db.exec_driver_sql('CREATE TABLE models(id INTEGER PRIMARY KEY)')
    db.exec_driver_sql("CREATE TABLE projects(id INTEGER PRIMARY KEY AUTOINCREMENT,org_id INTEGER,created_by INTEGER,name TEXT NOT NULL DEFAULT 'fixture',project_import_nonce TEXT,UNIQUE(id,project_import_nonce),FOREIGN KEY(id,project_import_nonce) REFERENCES project_import_operations(project_birth_id,project_birth_nonce) ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED)")
    db.exec_driver_sql("CREATE TABLE print_files(id INTEGER PRIMARY KEY AUTOINCREMENT,org_id INTEGER,created_by INTEGER,model_id INTEGER,job_id INTEGER,stored_path TEXT,filename TEXT DEFAULT 'fixture.3mf',project_import_nonce TEXT,UNIQUE(id,project_import_nonce),FOREIGN KEY(id,project_import_nonce) REFERENCES project_import_members(file_birth_id,member_nonce) ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED)")
    db.exec_driver_sql("CREATE TABLE print_archives(id INTEGER PRIMARY KEY AUTOINCREMENT,org_id INTEGER,ownership_state TEXT,job_id INTEGER,print_job_id INTEGER,printer_id INTEGER,user_id INTEGER,project_id INTEGER,print_file_id INTEGER,file_path TEXT,plate_count INTEGER DEFAULT 1,project_import_nonce TEXT,UNIQUE(id,project_import_nonce),FOREIGN KEY(id,project_import_nonce) REFERENCES project_import_members(archive_birth_id,member_nonce) ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED)")
    for table in s.TABLES:db.exec_driver_sql(s.ddl(table,False))
    for statement in s.sqlite_triggers().values():db.exec_driver_sql(statement)


@pytest.fixture
def db(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'contract.db'))
    @event.listens_for(engine,'connect')
    def enforcement(driver,record):driver.execute('PRAGMA foreign_keys=ON')
    with engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE');fresh_contract(connection)
        yield connection
        connection.connection.driver_connection.rollback()
    engine.dispose()


def root_reserved(db):
    generation=uuid4().hex
    row=dict(generation=generation,enrollment_nonce=uuid4().hex,command_id=str(uuid4()),configured_root_key=hashlib.sha256(b'/owned/root').hexdigest(),requested_by=7,request_digest='a'*64,expected_sha256=s.marker_sha(generation),namespace_birth_nonce=uuid4().hex,marker_birth_nonce=uuid4().hex,state='enrolling',lifecycle_revision=1,cleanup_capability='live_fd_only',expected_size=37)
    insert(db,s.ROOT,row);return row


def root_family(phase,root,birth=True):
    row={}
    for kind in ('namespace','marker'):
        fields=dict(device=1,inode=11 if kind=='namespace' else 12,kind='directory' if kind=='namespace' else 'regular',mode=448 if kind=='namespace' else 384,service_uid=501,link_count=2 if kind=='namespace' else 1,ctime_ns=10 if phase=='created' else 20,mtime_ns=10 if phase=='created' else 20,birth_ns=(8 if kind=='namespace' else 9) if birth else None)
        if kind=='marker':fields.update(byte_count=0 if phase=='created' else 37,digest_sha256=s.EMPTY_SHA if phase=='created' else root['expected_sha256'])
        row.update({kind+'_'+phase+'_'+k:v for k,v in fields.items()})
    row[phase+'_checkpoint_at']='2026-10-13T12:00:00.000000Z'
    return row


def active_root(db,birth=True):
    row=root_reserved(db)
    update(db,s.ROOT,root_family('created',row,birth),'generation=?',(row['generation'],))
    fields=root_family('sealed',row,birth);fields.update(state='active',lifecycle_revision=2,terminal_at='2026-10-13T12:00:01.000000Z',cleanup_capability='birth_verified' if birth else 'live_fd_only')
    update(db,s.ROOT,fields,'generation=?',(row['generation'],));return row


def operation(db,root,count=0):
    op=str(uuid4());directory=uuid4().hex
    row=dict(operation_id=op,owner_scope='org',owner_key=1,requested_by=7,manifest_sha256='b'*64,relative_directory='.project-imports/'+op.replace('-',''),declared_members=count,storage_root_generation=root['generation'],state='staging',lifecycle_revision=1,project_birth_nonce=uuid4().hex,directory_birth_nonce=directory,operation_marker_birth_nonce=uuid4().hex,operation_marker_size=70,operation_marker_sha256=s.marker_sha(root['generation'],op,directory))
    insert(db,s.OP,row);return row


def member(db,op,index=0,file=False):
    row=dict(operation_id=op['operation_id'],member_index=index,member_nonce=uuid4().hex,archive_key=str(index),protection='generic')
    if file:row.update(expected_relative_path=f'{index:03}.3mf',expected_size=3,expected_sha256=hashlib.sha256(b'abc').hexdigest(),original_name='owned.3mf')
    insert(db,s.MEMBER,row);return row


def checkpoint(db,op,key,domain,revision=1,**extra):
    nonce=op['directory_birth_nonce'] if key=='directory' else op['operation_marker_birth_nonce']
    row=dict(operation_id=op['operation_id'],object_key=key,checkpoint_domain=domain,domain_revision=revision,root_generation=op['storage_root_generation'],object_birth_nonce=nonce,device=1,inode=20 if key=='directory' else 21,kind='directory' if key=='directory' else 'regular',mode=448 if key=='directory' else 384,service_uid=501,link_count=2 if key=='directory' else 1,ctime_ns=30,mtime_ns=30,birth_ns=25)
    if key!='directory':row.update(byte_count=0 if domain=='birth' else 70,digest_sha256=s.EMPTY_SHA if domain=='birth' else op['operation_marker_sha256'])
    if domain!='birth':row.update(predecessor_object_key=key,predecessor_domain='birth',predecessor_revision=1)
    if domain=='failure_identity' and db.exec_driver_sql('SELECT count(*) FROM '+s.CHECKPOINT+" WHERE operation_id=? AND object_key=? AND checkpoint_domain='content'",(op['operation_id'],key)).scalar_one():row['predecessor_domain']='content'
    row.update(extra);insert(db,s.CHECKPOINT,row);return row


def directory_ready(db,op):
    checkpoint(db,op,'directory','birth');checkpoint(db,op,'operation_birth','birth');checkpoint(db,op,'operation_birth','content');checkpoint(db,op,'directory','content')
    update(db,s.OP,dict(directory_device=1,directory_inode=20,directory_kind='directory',directory_receipt_object_key='directory',directory_receipt_domain='content',directory_receipt_revision=1),'operation_id=?',(op['operation_id'],))


def abandoned_marker(db):
    root=active_root(db);op=operation(db,root);directory_ready(db,op)
    update(db,s.OP,dict(state='abandoned',lifecycle_revision=2,terminal_at='2026-10-13T12:00:02.000000Z',reason='definite_failure'),'operation_id=?',(op['operation_id'],))
    insert(db,s.CLEANUP,dict(operation_id=op['operation_id'],object_key='operation_birth',checkpoint_domain='content',checkpoint_revision=1,root_generation=root['generation'],object_birth_nonce=op['operation_marker_birth_nonce'],purpose='abandoned_cleanup',state='intent',revision=1))
    return op


def unlink_marker(db,op):
    update(db,s.CLEANUP,dict(state='unlinked',revision=2,parent_receipt_object_key='directory',parent_receipt_domain='cleanup_metadata',parent_receipt_revision=1),'operation_id=? AND object_key=?',(op['operation_id'],'operation_birth'))


def cleanup_metadata(db,op):
    checkpoint(db,op,'directory','cleanup_metadata',predecessor_domain='content',cleanup_object_key='operation_birth',cleanup_transition_revision=2,ctime_ns=40,mtime_ns=40)


def test_sqlite_apply_refuses_before_any_mutation(db):
    before=db.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').all()
    with pytest.raises(s.ProjectImportMaintenanceRequired):s.apply(db)
    assert db.connection.driver_connection.execute('PRAGMA foreign_keys').fetchone()[0]==1
    assert db.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').all()==before


def test_fresh_contract_and_exact_inventory(db):
    assert len(s.sqlite_triggers())==21 and len(s.pg_functions())==9 and len(s.pg_triggers())==13
    validate(db)


@pytest.mark.parametrize('birth',[True,False])
def test_root_created_then_atomic_active_capability(db,birth):
    active_root(db,birth);validate(db)


@pytest.mark.parametrize('variation',['premature','terminal','downgrade','unknown_birth','sealed_without_created'])
def test_root_capability_invalid_transitions(db,variation):
    root=root_reserved(db)
    if variation=='premature':fields=dict(cleanup_capability='birth_verified')
    elif variation=='sealed_without_created':fields=root_family('sealed',root);fields.update(state='active',lifecycle_revision=2,terminal_at='2026-10-13T12:00:01.000000Z',cleanup_capability='birth_verified')
    else:
        update(db,s.ROOT,root_family('created',root,variation!='unknown_birth'),'generation=?',(root['generation'],))
        fields=root_family('sealed',root,variation!='unknown_birth');fields.update(state='active',lifecycle_revision=2,terminal_at='2026-10-13T12:00:01.000000Z',cleanup_capability='birth_verified')
        if variation in ('terminal','downgrade'):
            update(db,s.ROOT,fields,'generation=?',(root['generation'],));fields=dict(cleanup_capability='live_fd_only' if variation=='downgrade' else 'birth_verified')
    with pytest.raises(IntegrityError):update(db,s.ROOT,fields,'generation=?',(root['generation'],))


@pytest.mark.parametrize('recursive',[0,1])
@pytest.mark.parametrize('table',['root','operation','member','checkpoint','cleanup'])
def test_retained_replace_refused_recursive_modes(db,recursive,table):
    db.exec_driver_sql('PRAGMA recursive_triggers='+str(recursive))
    op=abandoned_marker(db) if table=='cleanup' else operation(db,active_root(db),1 if table=='member' else 0)
    if table=='member':member(db,op)
    if table=='checkpoint':checkpoint(db,op,'directory','birth')
    name={'root':s.ROOT,'operation':s.OP,'member':s.MEMBER,'checkpoint':s.CHECKPOINT,'cleanup':s.CLEANUP}[table]
    row=dict(db.exec_driver_sql('SELECT * FROM '+name).mappings().first());row.pop('required_operation_state',None)
    with pytest.raises(IntegrityError):
        names=tuple(row);db.exec_driver_sql('INSERT OR REPLACE INTO '+name+'('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',tuple(row.values()))
    assert db.exec_driver_sql('SELECT count(*) FROM '+name).scalar_one()==1


def test_cleanup_revision3_preserves_stable_metadata_link(db):
    op=abandoned_marker(db);unlink_marker(db,op);cleanup_metadata(db,op)
    update(db,s.CLEANUP,dict(state='complete',revision=3,completion_at='2026-10-13T12:00:03.000000Z'),'operation_id=? AND object_key=?',(op['operation_id'],'operation_birth'))
    validate(db);db.commit()
    assert db.exec_driver_sql('SELECT state,revision,parent_receipt_revision FROM '+s.CLEANUP).one()==('complete',3,1)
    assert db.exec_driver_sql('PRAGMA foreign_key_check').all()==[]


def test_cleanup_missing_parent_receipt_fails_actual_commit(db):
    op=abandoned_marker(db);db.commit();db.exec_driver_sql('BEGIN IMMEDIATE');unlink_marker(db,op)
    with pytest.raises(IntegrityError):db.commit()
    db.connection.driver_connection.rollback();db.rollback();db.exec_driver_sql('BEGIN IMMEDIATE')
    assert db.exec_driver_sql('SELECT state,revision FROM '+s.CLEANUP).one()==('intent',1)
    assert db.exec_driver_sql('SELECT count(*) FROM '+s.CHECKPOINT+" WHERE checkpoint_domain='cleanup_metadata'").scalar_one()==0


def test_cleanup_duplicate_metadata_and_terminal_update_refuse(db):
    op=abandoned_marker(db);unlink_marker(db,op);cleanup_metadata(db,op)
    with pytest.raises(IntegrityError):cleanup_metadata(db,op)
    update(db,s.CLEANUP,dict(state='complete',revision=3,completion_at='2026-10-13T12:00:03.000000Z'),'operation_id=? AND object_key=?',(op['operation_id'],'operation_birth'))
    with pytest.raises(IntegrityError):update(db,s.CLEANUP,dict(state='complete',revision=3),'operation_id=?',(op['operation_id'],))


@pytest.mark.parametrize('value',['2026-02-30T12:00:00.000000Z','2026-10-13T24:00:00.000000Z','infinity','2026-10-13 12:00:00'])
def test_invalid_finite_calendar_refuses(db,value):
    root=root_reserved(db)
    with pytest.raises(IntegrityError):update(db,s.ROOT,dict(created_at=value),'generation=?',(root['generation'],))


def test_actual_partial_project_commit_rolls_back_whole_graph(db):
    root=active_root(db);op=operation(db,root);db.commit();db.exec_driver_sql('BEGIN IMMEDIATE')
    insert(db,'projects',dict(id=50,org_id=1,created_by=7,project_import_nonce=op['project_birth_nonce']))
    update(db,s.OP,dict(project_birth_id=50),'operation_id=?',(op['operation_id'],))
    with pytest.raises(IntegrityError):db.commit()
    db.connection.driver_connection.rollback();db.rollback();db.exec_driver_sql('BEGIN IMMEDIATE')
    assert db.exec_driver_sql('SELECT count(*) FROM projects').scalar_one()==0
    assert db.exec_driver_sql('SELECT project_birth_id FROM '+s.OP).scalar_one() is None


def test_complete_metadata_only_project_same_transaction(db):
    root=active_root(db);op=operation(db,root,1);m=member(db,op);directory_ready(db,op)
    insert(db,'projects',dict(id=50,org_id=1,created_by=7,project_import_nonce=op['project_birth_nonce']))
    update(db,s.OP,dict(project_birth_id=50),'operation_id=?',(op['operation_id'],))
    insert(db,'print_archives',dict(id=60,org_id=1,ownership_state='known',project_id=50,project_import_nonce=m['member_nonce']))
    update(db,s.MEMBER,dict(archive_birth_id=60),'operation_id=? AND member_index=?',(op['operation_id'],0))
    update(db,s.OP,dict(state='committed',lifecycle_revision=2,terminal_at='2026-10-13T12:00:03.000000Z'),'operation_id=?',(op['operation_id'],))
    validate(db);db.commit()
    assert db.exec_driver_sql('SELECT required_operation_state FROM '+s.MEMBER).scalar_one()=='committed'

@pytest.mark.parametrize('kind',['archive','file','mixed'])
def test_partial_member_bindings_fail_actual_commit(db,kind):
    root=active_root(db);op=operation(db,root,1);m=member(db,op,file=kind!='archive');db.commit();db.exec_driver_sql('BEGIN IMMEDIATE')
    if kind in ('archive','mixed'):
        insert(db,'projects',dict(id=50,org_id=1,created_by=7,project_import_nonce=op['project_birth_nonce']))
        update(db,s.OP,dict(project_birth_id=50),'operation_id=?',(op['operation_id'],))
    path='/owned/'+op['relative_directory']+'/000.3mf'
    if kind in ('file','mixed'):
        insert(db,'print_files',dict(id=70,org_id=1,created_by=7,stored_path=path,project_import_nonce=m['member_nonce']))
        update(db,s.MEMBER,dict(file_birth_id=70),'operation_id=? AND member_index=?',(op['operation_id'],0))
    if kind in ('archive','mixed'):
        values=dict(id=60,org_id=1,ownership_state='known',project_id=50,project_import_nonce=m['member_nonce'])
        if kind=='mixed':values.update(print_file_id=70,file_path=path)
        insert(db,'print_archives',values);update(db,s.MEMBER,dict(archive_birth_id=60),'operation_id=? AND member_index=?',(op['operation_id'],0))
    with pytest.raises(IntegrityError):db.commit()
    db.connection.driver_connection.rollback();db.rollback();db.exec_driver_sql('BEGIN IMMEDIATE')
    for table in ('projects','print_files','print_archives'):assert db.exec_driver_sql('SELECT count(*) FROM '+table).scalar_one()==0
    assert db.exec_driver_sql('SELECT archive_birth_id,file_birth_id FROM '+s.MEMBER).one()==(None,None)


def member_checkpoint(db,op,m,domain,**extra):
    key=str(m['member_index'])
    return checkpoint(db,op,key,domain,object_birth_nonce=m['member_nonce'],member_index=m['member_index'],inode=100+m['member_index'],byte_count=0 if domain=='birth' else m['expected_size'],digest_sha256=s.EMPTY_SHA if domain=='birth' else m['expected_sha256'],**extra)


def member_owned_metadata(db,op,m):
    i=m['member_index']
    return checkpoint(db,op,'directory','owned_metadata',i+1,related_object_key=str(i),related_domain='birth',related_revision=1,predecessor_domain='birth' if i==0 else 'owned_metadata',predecessor_revision=1 if i==0 else i)


def test_complete_file_project_and_retained_deleted_id_exclusion(db):
    root=active_root(db);op=operation(db,root,1);m=member(db,op,file=True)
    checkpoint(db,op,'directory','birth');checkpoint(db,op,'operation_birth','birth');checkpoint(db,op,'operation_birth','content')
    member_checkpoint(db,op,m,'birth');member_owned_metadata(db,op,m);member_checkpoint(db,op,m,'content')
    checkpoint(db,op,'directory','content',predecessor_domain='owned_metadata',predecessor_revision=1)
    update(db,s.OP,dict(directory_device=1,directory_inode=20,directory_kind='directory',directory_receipt_object_key='directory',directory_receipt_domain='content',directory_receipt_revision=1),'operation_id=?',(op['operation_id'],))
    insert(db,'projects',dict(id=50,org_id=1,created_by=7,project_import_nonce=op['project_birth_nonce']))
    update(db,s.OP,dict(project_birth_id=50),'operation_id=?',(op['operation_id'],))
    path='/owned/'+op['relative_directory']+'/000.3mf'
    insert(db,'print_files',dict(id=70,org_id=1,created_by=7,stored_path=path,project_import_nonce=m['member_nonce']))
    update(db,s.MEMBER,dict(file_birth_id=70,file_device=1,file_inode=100,file_kind='regular',verified_size=3,verified_sha256=m['expected_sha256'],sealed_object_key='0',sealed_domain='content',sealed_revision=1),'operation_id=? AND member_index=?',(op['operation_id'],0))
    insert(db,'print_archives',dict(id=60,org_id=1,ownership_state='known',project_id=50,print_file_id=70,file_path=path,project_import_nonce=m['member_nonce']))
    update(db,s.MEMBER,dict(archive_birth_id=60),'operation_id=? AND member_index=?',(op['operation_id'],0))
    update(db,s.OP,dict(state='committed',lifecycle_revision=2,terminal_at='2026-10-13T12:00:03.000000Z'),'operation_id=?',(op['operation_id'],))
    validate(db);db.commit();db.exec_driver_sql('BEGIN IMMEDIATE')
    for table in ('print_archives','print_files','projects'):db.exec_driver_sql('DELETE FROM '+table)
    validate(db)
    with pytest.raises(IntegrityError):insert(db,'projects',dict(id=50,org_id=1,created_by=7,project_import_nonce=op['project_birth_nonce']))
    insert(db,'projects',dict(id=50,org_id=2,created_by=8))
    validate(db);db.commit()
    assert db.exec_driver_sql('SELECT project_import_nonce FROM projects WHERE id=50').scalar_one() is None


@pytest.mark.parametrize('mutation',['trigger_missing','extra_index','root_digest'])
def test_independent_schema_and_data_drift_refuses(db,mutation):
    root=active_root(db)
    if mutation=='trigger_missing':db.exec_driver_sql('DROP TRIGGER zip029_root_update')
    elif mutation=='extra_index':db.exec_driver_sql('CREATE INDEX unsupported029 ON '+s.ROOT+'(state)')
    else:
        db.exec_driver_sql('DROP TRIGGER zip029_root_update');update(db,s.ROOT,dict(expected_sha256='c'*64,marker_sealed_digest_sha256='c'*64),'generation=?',(root['generation'],))
        # Data validator alone must reject structurally valid drift even without guard.
        with pytest.raises(MigrationError):validate_data(db)
    with pytest.raises(MigrationError):validate(db)


def test_full128_member647_receipt130_cleanup_bounds(db):
    root=active_root(db);op=operation(db,root,128)
    checkpoint(db,op,'directory','birth');checkpoint(db,op,'operation_birth','birth');checkpoint(db,op,'operation_birth','content');checkpoint(db,op,'operation_birth','failure_identity')
    values=[]
    for i in range(128):
        m=member(db,op,i,file=True);values.append(m)
        member_checkpoint(db,op,m,'birth');member_owned_metadata(db,op,m);member_checkpoint(db,op,m,'content');member_checkpoint(db,op,m,'failure_identity')
    checkpoint(db,op,'directory','content',predecessor_domain='owned_metadata',predecessor_revision=128)
    checkpoint(db,op,'directory','failure_identity')
    update(db,s.OP,dict(state='abandoned',lifecycle_revision=2,terminal_at='2026-10-13T12:00:02.000000Z',reason='definite_failure'),'operation_id=?',(op['operation_id'],))
    for n,(key,nonce) in enumerate([(str(m['member_index']),m['member_nonce']) for m in values]+[('operation_birth',op['operation_marker_birth_nonce'])],1):
        insert(db,s.CLEANUP,dict(operation_id=op['operation_id'],object_key=key,member_index=int(key) if key.isdecimal() else None,checkpoint_domain='failure_identity',checkpoint_revision=1,root_generation=root['generation'],object_birth_nonce=nonce,purpose='abandoned_cleanup',state='intent',revision=1))
        update(db,s.CLEANUP,dict(state='unlinked',revision=2,parent_receipt_object_key='directory',parent_receipt_domain='cleanup_metadata',parent_receipt_revision=n),'operation_id=? AND object_key=?',(op['operation_id'],key))
        checkpoint(db,op,'directory','cleanup_metadata',n,predecessor_domain='failure_identity' if n==1 else 'cleanup_metadata',predecessor_revision=1 if n==1 else n-1,cleanup_object_key=key,cleanup_transition_revision=2)
        update(db,s.CLEANUP,dict(state='complete',revision=3,completion_at='2026-10-13T12:00:03.000000Z'),'operation_id=? AND object_key=?',(op['operation_id'],key))
    insert(db,s.CLEANUP,dict(operation_id=op['operation_id'],object_key='directory',checkpoint_domain='failure_identity',checkpoint_revision=1,root_generation=root['generation'],object_birth_nonce=op['directory_birth_nonce'],purpose='abandoned_cleanup',state='intent',revision=1))
    update(db,s.CLEANUP,dict(state='unlinked',revision=2),'operation_id=? AND object_key=?',(op['operation_id'],'directory'))
    update(db,s.CLEANUP,dict(state='complete',revision=3,completion_at='2026-10-13T12:00:03.000000Z'),'operation_id=? AND object_key=?',(op['operation_id'],'directory'))
    validate(db);db.commit()
    assert db.exec_driver_sql('SELECT count(*) FROM '+s.CHECKPOINT).scalar_one()==647
    assert db.exec_driver_sql('SELECT count(*) FROM '+s.CLEANUP).scalar_one()==130
    assert db.exec_driver_sql('PRAGMA foreign_key_check').all()==[]


def test_unregistered_wrapper_pins_and_sqlite_refusal(db,monkeypatch):
    import importlib
    migration=importlib.import_module('core.schema.migrations.029_project_imports')
    before=db.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').all()
    with pytest.raises(s.ProjectImportMaintenanceRequired):migration.apply(db)
    assert db.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').all()==before
    altered=dict(migration.SOURCE_PINS);altered['project_import_schema.py']='0'*64
    monkeypatch.setattr(migration,'SOURCE_PINS',altered)
    with pytest.raises(MigrationError):migration.validate(db)


def test_failure_identity_then_content_refuses_and_preserves_receipts(db):
    op=operation(db,active_root(db));checkpoint(db,op,'directory','birth');checkpoint(db,op,'operation_birth','birth');checkpoint(db,op,'operation_birth','failure_identity')
    before=db.exec_driver_sql('SELECT * FROM '+s.CHECKPOINT+' ORDER BY object_key,checkpoint_domain').all()
    with pytest.raises(IntegrityError):checkpoint(db,op,'operation_birth','content')
    assert db.exec_driver_sql('SELECT * FROM '+s.CHECKPOINT+' ORDER BY object_key,checkpoint_domain').all()==before


@pytest.mark.parametrize('reserved',[0,1])
def test_bounded_partial_staging_is_retained_valid(db,reserved):
    op=operation(db,active_root(db),3)
    if reserved:member(db,op,2)
    validate(db);db.commit()
    assert db.exec_driver_sql('SELECT count(*) FROM '+s.MEMBER).scalar_one()==reserved


def test_guardless_restore_ambiguous_failure_content_order_refuses(db):
    op=operation(db,active_root(db));checkpoint(db,op,'directory','birth');checkpoint(db,op,'operation_birth','birth');checkpoint(db,op,'operation_birth','content');checkpoint(db,op,'operation_birth','failure_identity')
    db.exec_driver_sql('DROP TRIGGER zip029_checkpoint_update')
    update(db,s.CHECKPOINT,dict(predecessor_domain='birth',predecessor_revision=1),'operation_id=? AND object_key=? AND checkpoint_domain=?',(op['operation_id'],'operation_birth','failure_identity'))
    db.exec_driver_sql(s.sqlite_triggers()['zip029_checkpoint_update'])
    with pytest.raises(MigrationError):validate(db)


@pytest.mark.parametrize('kind',['marker','member'])
def test_guardless_restore_wrong_sealed_expected_bytes_refuses(db,kind):
    op=operation(db,active_root(db),1 if kind=='member' else 0)
    checkpoint(db,op,'directory','birth');checkpoint(db,op,'operation_birth','birth');checkpoint(db,op,'operation_birth','content')
    key='operation_birth'
    if kind=='member':
        m=member(db,op,file=True);member_checkpoint(db,op,m,'birth');member_owned_metadata(db,op,m);member_checkpoint(db,op,m,'content');key='0'
    db.exec_driver_sql('DROP TRIGGER zip029_checkpoint_update')
    update(db,s.CHECKPOINT,dict(digest_sha256='e'*64),'operation_id=? AND object_key=? AND checkpoint_domain=?',(op['operation_id'],key,'content'))
    db.exec_driver_sql(s.sqlite_triggers()['zip029_checkpoint_update'])
    with pytest.raises(MigrationError):validate(db)


def test_guardless_restore_null_org_owner_refuses_independently(db):
    test_complete_metadata_only_project_same_transaction(db)
    db.exec_driver_sql('DROP TRIGGER zip029_archives_update')
    update(db,'print_archives',dict(org_id=None),'id=?',(60,))
    db.exec_driver_sql(s.sqlite_triggers()['zip029_archives_update'])
    with pytest.raises(MigrationError):validate(db)


def test_closed_ddl_psycopg_transport_preserves_exact_server_sql():
    from sqlalchemy.dialects.postgresql.psycopg import dialect
    from psycopg._queries import _query2pg
    statements=[s.ddl(table,True,foreign=False) for table in s.TABLES]
    statements += [s.ddl(table,True,foreign=False).replace('CREATE TABLE '+table,'CREATE TEMPORARY TABLE zip029_reference_'+table,1) for table in s.TABLES]
    statements += ['CREATE FUNCTION public.'+name+'() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog, public AS $$'+body+'$$' for name,body in s.pg_functions().items()]
    for statement in statements:
        compiled=text(statement).compile(dialect=dialect())
        assert compiled.params == {} and compiled.binds == {}
        server_sql,formats,order,_ = _query2pg(str(compiled).encode(), 'utf8')
        assert server_sql == statement.encode()
        assert formats == [] and order is None
    original=s.ddl(s.MEMBER,True,foreign=False)
    from psycopg import ProgrammingError
    with pytest.raises(ProgrammingError):
        _query2pg(original.encode(),'utf8')


def test_closed_ddl_refuses_colon_binds_before_execution():
    from sqlalchemy.dialects.postgresql.psycopg import dialect
    class ClosedConnection:
        dialect=None
        def execute(self,clause):
            pytest.fail('bind-bearing closed SQL reached execution')
    for statement in ("SELECT :unexpected", "SELECT ':unexpected'"):
        with pytest.raises(MigrationError):
            connection=ClosedConnection()
            connection.dialect=dialect()
            s._execute_closed_ddl(connection,statement)
