"""Owned unregistered029 PostgreSQL SQL journals; no root/FD/auth authority."""
import os,stat,tempfile,secrets,json,traceback,hashlib,importlib
from pathlib import Path
from uuid import uuid4
PHASE='bootstrap'


def main():
    global PHASE
    private=Path(os.environ['PROJECT_IMPORT_NATIVE_PASSWORD_FILE'])
    assert os.getuid()==10001 and private.stat().st_uid==10001 and stat.S_IMODE(private.stat().st_mode)==0o600 and not private.is_symlink()
    with tempfile.TemporaryDirectory(prefix='owned-project-import029-') as folder:
        os.environ.update(DATABASE_URL='postgresql+psycopg://postgres@pg/postgres',DATABASE_PASSWORD_FILE=str(private),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
        from sqlalchemy import text,inspect
        from sqlalchemy.orm import Session
        from sqlalchemy.exc import DBAPIError
        from core.db import engine
        from core.db_compat import IS_POSTGRES
        from core.db_utils import _ConnectionAdapter,_postgres_placeholders
        from core.schema import bootstrap_database
        from core import project_import_schema as s
        migration=importlib.import_module('core.schema.migrations.029_project_imports')
        from core.project_import_validation import validate,validate_data
        from core.project_import_transaction import project_import_transaction,execute
        from core.schema.migrator import MigrationError
        assert IS_POSTGRES and engine.dialect.name=='postgresql'
        bootstrap_database(engine,Path('/source/backend'))
        def q(db,sql,params=()):return db.exec_driver_sql(_postgres_placeholders(sql),tuple(params))
        def insert(db,table,values):return q(db,'INSERT INTO '+table+'('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',values.values())
        def update(db,table,values,key,value):return q(db,'UPDATE '+table+' SET '+','.join(n+'=?' for n in values)+' WHERE '+key+'=?',(*values.values(),value))
        def root(db):
            generation=uuid4().hex;row=dict(generation=generation,enrollment_nonce=uuid4().hex,command_id=str(uuid4()),configured_root_key=hashlib.sha256(b'/owned/root').hexdigest(),requested_by=7,request_digest='a'*64,expected_sha256=s.marker_sha(generation),namespace_birth_nonce=uuid4().hex,marker_birth_nonce=uuid4().hex,state='enrolling',lifecycle_revision=1,cleanup_capability='live_fd_only',expected_size=37)
            insert(db,s.ROOT,row)
            for phase in ('created','sealed'):
                values={}
                for kind in ('namespace','marker'):
                    facts=dict(device=1,inode=11 if kind=='namespace' else 12,kind='directory' if kind=='namespace' else 'regular',mode=448 if kind=='namespace' else 384,service_uid=10001,link_count=2 if kind=='namespace' else 1,ctime_ns=10 if phase=='created' else 20,mtime_ns=10 if phase=='created' else 20,birth_ns=8 if kind=='namespace' else 9)
                    if kind=='marker':facts.update(byte_count=0 if phase=='created' else 37,digest_sha256=s.EMPTY_SHA if phase=='created' else row['expected_sha256'])
                    values.update({kind+'_'+phase+'_'+k:v for k,v in facts.items()})
                values[phase+'_checkpoint_at']='2026-10-13T12:00:00.000000Z'
                if phase=='sealed':values.update(state='active',lifecycle_revision=2,terminal_at='2026-10-13T12:00:01.000000Z',cleanup_capability='birth_verified')
                update(db,s.ROOT,values,'generation',generation)
            return row
        def operation(db,r,count=0):
            op=str(uuid4());directory=uuid4().hex
            row=dict(operation_id=op,owner_scope='org',owner_key=1,requested_by=7,manifest_sha256='b'*64,relative_directory='.project-imports/'+op.replace('-',''),declared_members=count,storage_root_generation=r['generation'],state='staging',lifecycle_revision=1,project_birth_nonce=uuid4().hex,directory_birth_nonce=directory,operation_marker_birth_nonce=uuid4().hex,operation_marker_size=70,operation_marker_sha256=s.marker_sha(r['generation'],op,directory))
            insert(db,s.OP,row);return row
        def checkpoint(db,o,key,domain,revision=1,**extra):
            values=dict(operation_id=o['operation_id'],object_key=key,checkpoint_domain=domain,domain_revision=revision,root_generation=o['storage_root_generation'],object_birth_nonce=o['directory_birth_nonce'] if key=='directory' else o['operation_marker_birth_nonce'],device=1,inode=20 if key=='directory' else 21,kind='directory' if key=='directory' else 'regular',mode=448 if key=='directory' else 384,service_uid=10001,link_count=2 if key=='directory' else 1,ctime_ns=30,mtime_ns=30,birth_ns=25)
            if key!='directory':values.update(byte_count=0 if domain=='birth' else 70,digest_sha256=s.EMPTY_SHA if domain=='birth' else o['operation_marker_sha256'])
            if domain!='birth':values.update(predecessor_object_key=key,predecessor_domain='birth',predecessor_revision=1)
            values.update(extra);insert(db,s.CHECKPOINT,values)
        def metadata_ready(db,o):
            checkpoint(db,o,'directory','birth');checkpoint(db,o,'operation_birth','birth');checkpoint(db,o,'operation_birth','content');checkpoint(db,o,'directory','content')
            update(db,s.OP,dict(directory_device=1,directory_inode=20,directory_kind='directory',directory_receipt_object_key='directory',directory_receipt_domain='content',directory_receipt_revision=1),'operation_id',o['operation_id'])
        def expect_state(function,state):
            try:function()
            except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)==state
            else:raise AssertionError('Expected exact actual PostgreSQL refusal')
        def source_columns(db):return {t:[(c['name'],str(c['type']),c['nullable'],c.get('default'),c.get('identity'),c.get('computed')) for c in inspect(db).get_columns(t,schema='public')] for t in s.SOURCE_TABLES}
        with engine.begin() as db:
            before=source_columns(db)
            name_column=next(c for c in before['projects'] if c[0]=='name')
            assert name_column[2] is False and name_column[3] is None
            db.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned SQL journal fixture',true)")
            db.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(7,'sql-journal-fixture','fixture','operator',true,1)")
            db.exec_driver_sql("INSERT INTO projects(id,created_by,name) VALUES(9000,7,'Owned legacy')")
            migration.apply(db);migration.validate(db)
            after=source_columns(db)
            assert all([x for x in after[t] if x[0]!=s.SOURCE]==before[t] for t in s.SOURCE_TABLES)
        print('project-import029-actualRegistered027-unregistered029-sourceColumnsDefaultsPreserved-pass',flush=True)
        PHASE='caller_executors'
        for kind in ('connection','session','adapter'):
            if kind=='connection':db=engine.connect();db.begin();db.exec_driver_sql('SELECT 1')
            elif kind=='session':db=Session(engine);db.begin();db.execute(text('SELECT 1'))
            else:
                raw=engine.raw_connection();db=_ConnectionAdapter(raw,True);db.execute('BEGIN')
            try:
                with project_import_transaction(db) as scope:
                    scope.fence(db);scope.check(db);execute(db,"INSERT INTO projects(id,name) VALUES(?,?)",(9001,'Owned rollback'))
                db.rollback()
                with engine.connect() as observer:assert observer.exec_driver_sql('SELECT count(*) FROM projects WHERE id=9001').scalar_one()==0
                if kind=='adapter':db.execute('BEGIN')
                elif kind=='session':db.begin();db.execute(text('SELECT 1'))
                else:db.begin();db.exec_driver_sql('SELECT 1')
                try:
                    with project_import_transaction(db) as scope:
                        scope.fence(db);db.commit()
                        if kind=='adapter':db.execute('BEGIN')
                        elif kind=='session':db.begin();db.execute(text('SELECT 1'))
                        else:db.begin();db.exec_driver_sql('SELECT 1')
                        scope.check(db)
                except MigrationError:pass
                else:raise AssertionError('Expired physical scope revived')
            finally:
                db.rollback()
                if kind=='adapter':raw.close()
                else:db.close()
            print('project-import029-actual-'+kind+'-callerRollback-commitExpiry-pass',flush=True)
        PHASE='journals'
        with engine.begin() as db:r=root(db);o=operation(db,r);metadata_ready(db,o)
        with engine.connect() as db:
            db.begin();db.exec_driver_sql('SELECT 1')
            with project_import_transaction(db) as scope:
                scope.fence(db);insert(db,'projects',dict(id=9010,name='Owned imported project',org_id=1,created_by=7,project_import_nonce=o['project_birth_nonce']))
                update(db,s.OP,dict(project_birth_id=9010),'operation_id',o['operation_id'])
            expect_state(db.commit,'23503');db.connection.driver_connection.rollback();db.rollback()
        with engine.connect() as observer:assert observer.exec_driver_sql('SELECT count(*) FROM projects WHERE id=9010').scalar_one()==0
        print('project-import029-realDeferredCOMMIT-stagedProject-fullRollback-pass',flush=True)
        with engine.begin() as db:
            insert(db,'projects',dict(id=9010,name='Owned imported project',org_id=1,created_by=7,project_import_nonce=o['project_birth_nonce']))
            update(db,s.OP,dict(project_birth_id=9010,state='committed',lifecycle_revision=2,terminal_at='2026-10-13T12:00:03.000000Z'),'operation_id',o['operation_id']);validate(db)
        print('project-import029-sameTX-completeZeroMemberProject-pass',flush=True)
        PHASE='partial_staging'
        with engine.begin() as db:
            empty=operation(db,r,3);partial=operation(db,r,3)
            insert(db,s.MEMBER,dict(operation_id=partial['operation_id'],member_index=2,member_nonce=uuid4().hex,archive_key='2',protection='generic'))
            validate(db)
        with engine.connect() as observer:
            assert q(observer,'SELECT count(*) FROM '+s.MEMBER+' WHERE operation_id=?',(empty['operation_id'],)).scalar_one()==0
            assert q(observer,'SELECT member_index FROM '+s.MEMBER+' WHERE operation_id=?',(partial['operation_id'],)).scalar_one()==2
        print('project-import029-declared3-stagingZero-andInRangePartial-durable-pass',flush=True)
        PHASE='failure_before_content'
        with engine.begin() as db:
            failed=operation(db,r);checkpoint(db,failed,'directory','birth');checkpoint(db,failed,'operation_birth','birth');checkpoint(db,failed,'operation_birth','failure_identity')
            before=db.exec_driver_sql('SELECT count(*) FROM '+s.CHECKPOINT).scalar_one()
            try:
                with db.begin_nested():checkpoint(db,failed,'operation_birth','content')
            except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='23514'
            else:raise AssertionError('Content admitted after failure receipt')
            assert db.exec_driver_sql('SELECT count(*) FROM '+s.CHECKPOINT).scalar_one()==before
            validate(db)
        print('project-import029-failureThenContent23514-unchangedReceipts-pass',flush=True)

        with engine.begin() as db:
            db.exec_driver_sql('DELETE FROM projects WHERE id=9010')
            try:
                with db.begin_nested():insert(db,'projects',dict(id=9010,name='Owned imported project',org_id=1,created_by=7,project_import_nonce=o['project_birth_nonce']))
            except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='23514'
            else:raise AssertionError('Retained deleted source nonce reattached')
        PHASE='cleanup_deferred_cycle'
        with engine.begin() as db:
            abandoned=operation(db,r);metadata_ready(db,abandoned)
            update(db,s.OP,dict(state='abandoned',lifecycle_revision=2,terminal_at='2026-10-13T12:00:02.000000Z',reason='definite_failure'),'operation_id',abandoned['operation_id'])
            insert(db,s.CLEANUP,dict(operation_id=abandoned['operation_id'],object_key='operation_birth',checkpoint_domain='content',checkpoint_revision=1,root_generation=r['generation'],object_birth_nonce=abandoned['operation_marker_birth_nonce'],purpose='abandoned_cleanup',state='intent',revision=1))
        def unlinked(db):update(db,s.CLEANUP,dict(state='unlinked',revision=2,parent_receipt_object_key='directory',parent_receipt_domain='cleanup_metadata',parent_receipt_revision=1),'operation_id',abandoned['operation_id'])
        with engine.connect() as db:
            db.begin();unlinked(db);expect_state(db.commit,'23503');db.connection.driver_connection.rollback();db.rollback()
        with engine.begin() as db:
            assert db.exec_driver_sql('SELECT state,revision FROM '+s.CLEANUP).one()==('intent',1)
            unlinked(db);checkpoint(db,abandoned,'directory','cleanup_metadata',predecessor_domain='content',cleanup_object_key='operation_birth',cleanup_transition_revision=2)
            update(db,s.CLEANUP,dict(state='complete',revision=3,completion_at='2026-10-13T12:00:03.000000Z'),'operation_id',abandoned['operation_id']);validate(db)
        print('project-import029-realCleanupMissingMetadata-COMMIT23503-thenStableRevision3-pass',flush=True)
        PHASE='exact_schema_drift'
        with engine.begin() as db:
            for bad in ('disabled','foreign_schema'):
                try:
                    with db.begin_nested():
                        if bad=='disabled':db.exec_driver_sql('ALTER TABLE public.'+s.ROOT+' DISABLE TRIGGER zip029_root_row')
                        else:
                            db.exec_driver_sql('CREATE SCHEMA owned_zip_shadow');db.exec_driver_sql('CREATE TABLE owned_zip_shadow.'+s.OP+' (LIKE public.'+s.OP+' INCLUDING ALL)')
                            db.exec_driver_sql('ALTER TABLE public.projects DROP CONSTRAINT zip029_projects_source_binding')
                            db.exec_driver_sql('ALTER TABLE public.projects ADD CONSTRAINT zip029_projects_source_binding FOREIGN KEY(id,project_import_nonce) REFERENCES owned_zip_shadow.'+s.OP+'(project_birth_id,project_birth_nonce) ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED')
                        validate(db)
                except MigrationError:pass
                else:raise AssertionError('Schema enforcement drift accepted')
            validate(db)
        print('project-import029-disabledGuard-foreignSameNameOID-refused-pass',flush=True)
        PHASE='contention'
        for first in ('reader','writer'):
            with engine.connect() as owner,engine.connect() as contender:
                owner.begin();owner.exec_driver_sql('SELECT 1');contender.begin();contender.exec_driver_sql('SELECT 1')
                if first=='reader':
                    with project_import_transaction(owner) as scope:
                        scope.fence(owner)
                        expect_state(lambda:contender.exec_driver_sql('LOCK TABLE public.'+s.OP+' IN ROW EXCLUSIVE MODE NOWAIT'),'55P03')
                else:
                    owner.exec_driver_sql('LOCK TABLE public.'+s.OP+' IN ROW EXCLUSIVE MODE NOWAIT')
                    def attempt():
                        with project_import_transaction(contender) as scope:scope.fence(contender)
                    expect_state(attempt,'55P03')
                contender.rollback();owner.rollback()
            with engine.connect() as released:
                released.begin();released.exec_driver_sql('SELECT 1')
                with project_import_transaction(released) as scope:scope.fence(released)
                released.rollback()
        with engine.connect() as db:
            db.begin();db.exec_driver_sql("SET LOCAL lock_timeout='50ms'")
            with project_import_transaction(db) as scope:
                scope.fence(db);assert db.exec_driver_sql("SELECT setting FROM pg_settings WHERE name='lock_timeout'").scalar_one()=='50'
            db.rollback()
        print('project-import029-bothOrder-NOWAIT55P03-release-stricterTimeoutPreserved-pass',flush=True)
        PHASE='lost_ack'
        with engine.connect() as db:
            db.begin();lost=operation(db,r)
            db.commit()
            try:raise RuntimeError('Owned acknowledgement loss after actual commit')
            except RuntimeError:pass
            with engine.connect() as observer:
                row=q(observer,'SELECT operation_id,manifest_sha256,storage_root_generation,state,lifecycle_revision FROM '+s.OP+' WHERE operation_id=?',(lost['operation_id'],)).one()
                assert tuple(row)==(lost['operation_id'],lost['manifest_sha256'],lost['storage_root_generation'],'staging',1)
        with engine.begin() as db:migration.validate(db);migration.apply(db)
        print('project-import029-realCommittedJournal-lostACK-exactFreshObserver-noFilesystemCleanup-pass',flush=True)
        print('project-import029-native-pass',flush=True)
        engine.dispose()

if __name__=='__main__':
    try:main()
    except BaseException as error:
        original=getattr(error,'orig',error);state=getattr(original,'sqlstate',None)
        print('project-import029-native-failure '+json.dumps({'phase':PHASE,'class':type(error).__name__,'sqlstate':state if isinstance(state,str) and len(state)==5 else 'unknown','frames':[{'file':Path(frame.filename).name,'function':frame.name,'line':frame.lineno} for frame in traceback.extract_tb(error.__traceback__)[-5:]]}),flush=True)
        raise RuntimeError('Owned029 probe failed; sanitized classification above') from None
