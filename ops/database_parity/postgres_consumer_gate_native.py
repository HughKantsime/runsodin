"""Owned PG restore consumer fence: real marker, process death and recovery."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from sqlalchemy import create_engine,text
from core.database_config import create_database_engine,RESTORE_ADVISORY_LOCK,APPLICATION_NAMES
from core.postgres_restore_gate import assert_postgres_restore_ready,RestoreConsumerBlocked,MARKER
from core.schema import bootstrap_database,validate_schema
from modules.system import postgres_backup_service as backup


def main():
    source='postgresql://postgres@pg/consumer_gate'
    raw=os.environ['CONSUMER_GATE_NATIVE_URL']
    admin=create_engine(raw,isolation_level='AUTOCOMMIT',hide_parameters=True)
    with admin.connect() as db:db.exec_driver_sql('CREATE DATABASE consumer_gate')
    try:
        with tempfile.TemporaryDirectory(prefix='consumer-gate-') as folder:
            root=Path(folder);os.environ.update(ODIN_DATA_DIR=str(root),DATABASE_URL=source,ODIN_DB_ROLE='api',ODIN_SCHEMA_WAIT_SECONDS='2')
            app=root/'app-secret';maintenance=root/'maintenance-secret'
            for file in (app,maintenance):file.write_text(uuid.uuid4().hex);file.chmod(0o600)
            os.environ.update(DATABASE_PASSWORD_FILE=str(app),DATABASE_MAINTENANCE_PASSWORD_FILE=str(maintenance),DATABASE_MAINTENANCE_URL='postgresql://postgres@pg/postgres')
            privileged=create_database_engine(source,role='restore',password_file=str(app));bootstrap_database(privileged)
            candidate,metadata=backup.create_postgres_backup(source,maintenance_url=os.environ['DATABASE_MAINTENANCE_URL'],
                password_file=str(app),maintenance_password_file=str(maintenance),base_dir=root,prefix='rollback_')
            backup.stage_postgres_restore(candidate,source,os.environ['DATABASE_MAINTENANCE_URL'],password_file=str(app),maintenance_password_file=str(maintenance),base_dir=root)
            staged=json.loads((root/MARKER).read_bytes());assert set(staged)=={'schema_version','dialect','state','sha256','size_bytes','actor_id','staged_at'}
            assert_postgres_restore_ready()
            print('consumer-gate-native-real-generated-staged-pass')
            # Consumer wins: shared physical-session ownership prevents restore.
            consumer=create_database_engine(source,role='api',password_file=str(app))
            with consumer.connect() as live,privileged.connect() as observer:
                assert live.execute(text('SELECT 1')).scalar_one()==1
                assert observer.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':RESTORE_ADVISORY_LOCK}).scalar_one() is False
            consumer.dispose()
            print('consumer-gate-native-shared-first-pass')
            # Already pooled connection must not bypass a new durable refusal.
            consumer=create_database_engine(source,role='api',password_file=str(app))
            with consumer.connect():pass
            pending=dict(staged,state='restore_in_progress',rollback_backup=candidate.name,rollback_sha256=metadata['sha256'],rollback_size_bytes=metadata['size_bytes'],apply_started_at=staged['staged_at'])
            backup._write_pending_manifest(backup.postgres_paths(root),pending)
            try:consumer.connect()
            except RestoreConsumerBlocked:pass
            else:raise AssertionError('Pooled connection escaped durable gate')
            assert consumer.pool.checkedout()==0
            consumer.dispose();backup._write_pending_manifest(backup.postgres_paths(root),staged)
            print('consumer-gate-native-pool-refusal-pass')
            # Eager passes staged; restore then owns exclusive lock before marker.
            waiting=create_database_engine(source,role='api',password_file=str(app))
            privileged.dispose()
            payload_path=root/'expected-progress.json';payload_path.write_text(json.dumps(pending));payload_path.chmod(0o600)
            control=root/'write-marker'
            child_code='''import json,os,time\nfrom pathlib import Path\nfrom sqlalchemy import text\nfrom core.database_config import create_database_engine,RESTORE_ADVISORY_LOCK\nfrom modules.system import postgres_backup_service as b\ne=create_database_engine(os.environ['DATABASE_URL'],role='restore')\nc=e.connect();c.execute(text('SELECT pg_advisory_lock(:key)'),{'key':RESTORE_ADVISORY_LOCK});c.commit()\nprint('EXCLUSIVE',flush=True)\np=Path(os.environ['ODIN_DATA_DIR'])\nwhile not (p/'write-marker').exists():time.sleep(.02)\nb._write_pending_manifest(b.postgres_paths(p),json.loads((p/'expected-progress.json').read_text()))\nprint('DURABLE',flush=True)\nwhile True:time.sleep(1)\n'''
            owner=subprocess.Popen([sys.executable,'-c',child_code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
            try:
                assert owner.stdout.readline().strip()=='EXCLUSIVE'
                errors=[]
                def connect():
                    try:
                        with waiting.connect():raise AssertionError('Gate escaped unfinished restore')
                    except BaseException as exc:errors.append(exc)
                thread=threading.Thread(target=connect);thread.start()
                # Observe ungranted shared lock request on actual PostgreSQL.
                with privileged.connect() as observer:
                    deadline=time.monotonic()+3
                    while not observer.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted")).scalar_one():
                        if time.monotonic()>deadline:raise AssertionError('Consumer did not wait for exclusive owner')
                        time.sleep(.02)
                control.touch();assert owner.stdout.readline().strip()=='DURABLE'
                owner.kill();owner.wait(timeout=5);thread.join(timeout=5)
                if len(errors)==1 and not isinstance(errors[0],RestoreConsumerBlocked):
                    import traceback
                    print('consumer-gate-diagnostic-stack=', ''.join(traceback.format_exception(errors[0])),flush=True)
                assert len(errors)==1 and isinstance(errors[0],RestoreConsumerBlocked), ([type(error).__name__ for error in errors], thread.is_alive())
            finally:
                if owner.poll() is None:owner.kill();owner.wait(timeout=5)
                owner.stdout.close();owner.stderr.close();waiting.dispose()
            # Schema itself is valid; durable gate survives actual process death.
            with privileged.connect() as db:
                validate_schema(db)
                assert db.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':RESTORE_ADVISORY_LOCK}).scalar_one()
                db.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':RESTORE_ADVISORY_LOCK});db.commit()
            for role in APPLICATION_NAMES:
                if role in ('backup','restore'):continue
                try:create_database_engine(source,role=role,password_file=str(app))
                except RestoreConsumerBlocked:pass
                else:raise AssertionError('Ordinary role escaped eager gate')
            follower=subprocess.run([sys.executable,'-m','scripts.wait_for_database_schema'],env=os.environ.copy(),capture_output=True,text=True,timeout=10)
            assert follower.returncode!=0 and 'RestoreConsumerBlocked' in follower.stderr
            pending['recovery_incomplete']=True;pending['recovery_error']='RuntimeError'
            backup._write_pending_manifest(backup.postgres_paths(root),pending)
            follower=subprocess.run([sys.executable,'-m','scripts.wait_for_database_schema'],env=os.environ.copy(),capture_output=True,text=True,timeout=10)
            assert follower.returncode!=0
            print('consumer-gate-native-actual-death-unfinished-follower-roles-pass')
            privileged.dispose()
            coordinator=subprocess.run([sys.executable,'-m','modules.system.restore_coordinator'],env=os.environ.copy(),capture_output=True,text=True,timeout=120)
            assert coordinator.returncode==0,coordinator.stderr[-2000:]
            assert 'rolled_back_after_interruption' in coordinator.stdout and not (root/MARKER).exists()
            follower=subprocess.run([sys.executable,'-m','scripts.wait_for_database_schema'],env=os.environ.copy(),capture_output=True,text=True,timeout=30)
            assert follower.returncode==0,follower.stderr[-2000:]
            print('consumer-gate-native-actual-owner-recovery-follower-pass')
            privileged.dispose()
    finally:
        with admin.connect() as db:db.exec_driver_sql('DROP DATABASE consumer_gate WITH (FORCE)')
        admin.dispose()
    print('postgres-consumer-gate-native-pass')

if __name__=='__main__':main()
