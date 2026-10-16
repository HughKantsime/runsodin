"""Actual owned physical staging and restart reconciliation, never unlink."""
import os
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from tests.test_contracts.test_timelapse_retirement_prepare import owned_plan,prepare
from tests.test_contracts.test_timelapse_quarantine_binding import bind


@pytest.fixture
def prepared_stage(owned_plan):
    prepare(owned_plan);bind(owned_plan)
    with Session(owned_plan[0]) as db:
        original=[dict(row) for row in db.execute(text('SELECT * FROM timelapse_retirement_objects ORDER BY object_index')).mappings()]
    bytes_by_index={row['object_index']:(owned_plan[1]/row['relative_path']).read_bytes() for row in original if row['kind']=='file'}
    return owned_plan,original,bytes_by_index


def stage(plan):
    from modules.archives.timelapse_staging import stage_retirement
    return stage_retirement(*plan[:4])


def retained(fixture):
    plan,original,expected=fixture;root=plan[1]
    for row in original:
        if row['kind']=='file':
            source=root/row['relative_path'];destination=root/'.retirement'/row['quarantine_name']
            assert (source.is_file() and source.read_bytes()==expected[row['object_index']]) or (destination.is_file() and destination.read_bytes()==expected[row['object_index']])


def test_full_staging_and_retry_resolve_already_moved_parent_directories(prepared_stage):
    plan,original,expected=prepared_stage
    result=stage(plan);assert result.state=='staged'
    retained(prepared_stage)
    for row in original:
        assert not (plan[1]/row['relative_path']).exists()
        destination=plan[1]/'.retirement'/row['quarantine_name']
        assert destination.stat().st_ino==row['inode']
        if row['kind']=='directory':assert list(destination.iterdir())==[]
    with Session(plan[0]) as db:
        assert {row[0] for row in db.execute(text('SELECT state FROM timelapse_retirement_objects'))}=={'staged'}
        assert {row[0] for row in db.execute(text('SELECT phase FROM timelapse_retirement_moves'))}=={'staged'}
        assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='deleting'
        assert db.execute(text('SELECT state,completed_at FROM timelapse_retirements')).one()==('in_flight',None)
    assert stage(plan).operation_id==result.operation_id
    with Session(plan[0]) as db:assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_stage'")).scalar_one()==1
    retained(prepared_stage)


@pytest.mark.parametrize('target',['file','nested_directory','root_directory'])
def test_move_then_exception_recovers_original_identity_and_nested_parents(prepared_stage,monkeypatch,target):
    from core import timelapse_staging_fs as fs
    from modules.archives.timelapse_staging import StagingUnavailable
    plan,original,_=prepared_stage;actual=fs.rename_noreplace;failed=False
    def rename(source,name,destination,planned):
        nonlocal failed
        actual(source,name,destination,planned)
        chosen=(target=='file' and name.endswith('.jpg')) or (target=='nested_directory' and name=='frames') or (target=='root_directory' and name==str(plan[2]))
        if chosen and not failed:failed=True;raise RuntimeError('owned interrupted move')
    monkeypatch.setattr(fs,'rename_noreplace',rename)
    with pytest.raises(StagingUnavailable):stage(plan)
    assert failed;retained(prepared_stage)
    with Session(plan[0]) as db:assert db.execute(text("SELECT COUNT(*) FROM timelapse_retirement_moves WHERE phase='rename_pending'")).scalar_one()==1
    monkeypatch.setattr(fs,'rename_noreplace',actual)
    assert stage(plan).state=='staged';retained(prepared_stage)


@pytest.mark.parametrize('fault',['pending_commit','pending_ack','progress_commit','progress_ack','source_fsync','quarantine_fsync'])
def test_phase_and_sync_faults_preserve_media_and_exact_retry(prepared_stage,monkeypatch,fault):
    from modules.archives import timelapse_stage_transactions as transactions
    from modules.archives.timelapse_staging import StagingUnavailable
    plan,_,_=prepared_stage;commits=0
    class FaultSession(Session):
        def commit(self):
            nonlocal commits
            commits+=1
            target=2 if fault.startswith('pending_') else 3
            if commits==target and fault.endswith('_commit'):raise RuntimeError('owned refused phase commit')
            super().commit()
            if commits==target and fault.endswith('_ack'):raise RuntimeError('owned uncertain phase acknowledgment')
    monkeypatch.setattr(transactions,'Session',FaultSession)
    actual_sync=os.fsync;syncs=0
    def sync(fd):
        nonlocal syncs
        syncs+=1
        if (fault=='source_fsync' and syncs==1) or (fault=='quarantine_fsync' and syncs==2):raise OSError('owned sync interruption')
        return actual_sync(fd)
    monkeypatch.setattr(os,'fsync',sync)
    with pytest.raises(StagingUnavailable):stage(plan)
    retained(prepared_stage)
    if fault in ('pending_commit','pending_ack'):
        assert all((plan[1]/row['relative_path']).exists() for row in prepared_stage[1])
    monkeypatch.setattr(transactions,'Session',Session);monkeypatch.setattr(os,'fsync',actual_sync)
    assert stage(plan).state=='staged';retained(prepared_stage)


@pytest.mark.parametrize('damage',['destination','source_contents','hardlink','symlink','extra_child','parent_replacement','quarantine_replacement'])
def test_foreign_or_changed_objects_are_never_adopted_or_removed(prepared_stage,damage):
    from modules.archives.timelapse_staging import StagingUnavailable
    plan,original,_=prepared_stage;root=plan[1]
    first=next(row for row in original if row['relative_path'].endswith('one.jpg'))
    source=root/first['relative_path'];foreign=None
    if damage=='destination':foreign=root/'.retirement'/first['quarantine_name'];foreign.write_bytes(b'owned foreign destination')
    elif damage=='source_contents':foreign=source;foreign.write_bytes(b'owned changed bytes')
    elif damage=='hardlink':foreign=root/'alias.jpg';os.link(source,foreign)
    elif damage=='symlink':source.rename(root/'held.jpg');source.symlink_to(root/'held.jpg');foreign=source
    elif damage=='extra_child':foreign=plan[5]/'new.jpg';foreign.write_bytes(b'owned extra child')
    elif damage=='parent_replacement':
        plan[5].rename(plan[5].with_name('held-frames'));plan[5].mkdir();foreign=plan[5]/'new.jpg';foreign.write_bytes(b'owned new parent child')
    else:
        (root/'.retirement').rename(root/'held-quarantine');(root/'.retirement').mkdir(mode=0o700);foreign=root/'.retirement'/'new';foreign.write_bytes(b'owned new quarantine')
    before=foreign.lstat()
    with pytest.raises(StagingUnavailable):stage(plan)
    after=foreign.lstat();assert before.st_ino==after.st_ino
    with Session(plan[0]) as db:assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='deleting'


@pytest.mark.parametrize('target',['file','frames','root'])
def test_actual_process_death_after_move_and_fresh_process_recovery(prepared_stage,target):
    import subprocess,sys,json
    from pathlib import Path
    plan,_,_=prepared_stage;repo=Path(__file__).resolve().parents[2]
    command=[sys.executable,str(repo/'tests/fixtures/certification/run_owned_timelapse_stage.py'),str(plan[0].url.database),str(plan[1]),str(plan[2])]
    environment=dict(os.environ,PYTHONPATH=str(repo/'backend')+':'+str(repo),ODIN_OWNED_TEST_ROOT=str(_pytest_owned_root()),BASE_URL='http://127.0.0.1:1')
    crashed=subprocess.run(command+[target],cwd=repo,env=environment,capture_output=True,text=True,timeout=30)
    assert crashed.returncode==73,{'returncode':crashed.returncode,'output':crashed.stdout}
    retained(prepared_stage)
    with Session(plan[0]) as db:assert db.execute(text("SELECT COUNT(*) FROM timelapse_retirement_moves WHERE phase='rename_pending'")).scalar_one()==1
    recovered=subprocess.run(command+['none'],cwd=repo,env=environment,capture_output=True,text=True,timeout=30)
    assert recovered.returncode==0,{'returncode':recovered.returncode,'output':recovered.stdout}
    assert json.loads(recovered.stdout)['status']=='staged'
    retained(prepared_stage)
    with Session(plan[0]) as db:
        assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='deleting'
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_stage'")).scalar_one()==1


@pytest.mark.parametrize('damage',['phase_mismatch','parent_advanced','absence_advanced'])
def test_impossible_recorded_progress_refuses_before_media_move(prepared_stage,damage):
    from modules.archives.timelapse_staging import StagingUnavailable
    plan,original,_=prepared_stage
    with Session(plan[0]) as db:
        if damage=='phase_mismatch':db.execute(text("UPDATE timelapse_retirement_objects SET state='staged' WHERE kind='file'"))
        elif damage=='parent_advanced':db.execute(text("UPDATE timelapse_retirement_moves SET phase='rename_pending' WHERE kind='directory'"))
        else:
            row=next(value for value in original if value['relative_path']==str(plan[2]))
            db.execute(text('DELETE FROM timelapse_retirement_moves WHERE object_index=:index'),{'index':row['object_index']})
            db.execute(text("UPDATE timelapse_retirement_objects SET state='originally_absent',quarantine_name=NULL,device=NULL,inode=NULL,uid=NULL,gid=NULL,mode=NULL,link_count=NULL,byte_count=NULL,mtime_ns=NULL,ctime_ns=NULL WHERE object_index=:index"),{'index':row['object_index']})
        db.commit()
    with pytest.raises(StagingUnavailable):stage(plan)
    assert all((plan[1]/row['relative_path']).exists() for row in original)
    retained(prepared_stage)


def test_changed_pending_destination_digest_cannot_be_adopted(prepared_stage,monkeypatch):
    from core import timelapse_staging_fs as fs
    from modules.archives.timelapse_staging import StagingUnavailable
    plan,original,_=prepared_stage;actual=fs.rename_noreplace
    def rename(*args):actual(*args);raise RuntimeError('owned interrupted rename')
    monkeypatch.setattr(fs,'rename_noreplace',rename)
    with pytest.raises(StagingUnavailable):stage(plan)
    row=next(value for value in original if value['relative_path'].endswith('one.jpg'))
    destination=plan[1]/'.retirement'/row['quarantine_name'];foreign=b'x'*row['byte_count'];destination.write_bytes(foreign)
    os.utime(destination,ns=(row['mtime_ns'],row['mtime_ns']))
    monkeypatch.setattr(fs,'rename_noreplace',actual)
    with pytest.raises(StagingUnavailable):stage(plan)
    assert destination.read_bytes()==foreign
    with Session(plan[0]) as db:assert db.execute(text('SELECT phase FROM timelapse_retirement_moves WHERE object_index=:index'),{'index':row['object_index']}).scalar_one()=='rename_pending'


def test_deadline_during_hash_preserves_pending_and_starts_no_rename(prepared_stage,monkeypatch):
    from core import timelapse_staging_fs as fs
    from modules.archives.timelapse_staging import StagingUnavailable
    plan,original,_=prepared_stage;expired=False;actual=os.read
    monkeypatch.setattr(fs.time,'monotonic',lambda:200 if expired else 100)
    def read(*args):
        nonlocal expired
        value=actual(*args);expired=True;return value
    monkeypatch.setattr(os,'read',read)
    with pytest.raises(StagingUnavailable) as error:stage(plan)
    assert error.value.reason=='manifest_limit'
    assert all((plan[1]/row['relative_path']).exists() for row in original)
    with Session(plan[0]) as db:assert db.execute(text("SELECT COUNT(*) FROM timelapse_retirement_moves WHERE phase='rename_pending'")).scalar_one()==1
    retained(prepared_stage)


def test_hash_budget_refuses_before_file_read(prepared_stage,monkeypatch):
    from core import timelapse_staging_fs as fs
    from modules.archives.timelapse_staging import StagingUnavailable
    monkeypatch.setattr(fs,'MAX_BYTES',1)
    monkeypatch.setattr(os,'read',lambda *a:pytest.fail('Read beyond aggregate file budget'))
    with pytest.raises(StagingUnavailable):stage(prepared_stage[0])
    retained(prepared_stage)


def test_audit_refusal_rolls_back_admission_without_any_media_move(prepared_stage,monkeypatch):
    from core import audit
    from modules.archives.timelapse_staging import StagingUnavailable
    plan,original,_=prepared_stage
    monkeypatch.setattr(audit,'log_audit',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('owned audit refusal')))
    with pytest.raises(StagingUnavailable):stage(plan)
    assert all((plan[1]/row['relative_path']).exists() for row in original)
    with Session(plan[0]) as db:
        assert db.execute(text('SELECT state FROM timelapse_retirements')).scalar_one()=='pending'
        assert db.execute(text("SELECT COUNT(*) FROM timelapse_retirement_moves WHERE phase<>'planned'")).scalar_one()==0
    retained(prepared_stage)


def test_revocation_after_admitted_move_prevents_progress_and_further_moves(prepared_stage,monkeypatch):
    from modules.archives import timelapse_staging as implementation
    plan,original,_=prepared_stage;actual=implementation.commit_stage_step;changed=False
    def commit(*args,**kwargs):
        nonlocal changed
        result=actual(*args,**kwargs)
        if kwargs.get('phase')=='rename_pending' and not changed:
            changed=True
            with Session(plan[0]) as db:db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));db.commit()
        return result
    monkeypatch.setattr(implementation,'commit_stage_step',commit)
    with pytest.raises(implementation.StagingUnavailable) as error:stage(plan)
    assert error.value.reason=='unknown_authority'
    assert changed
    retained(prepared_stage)
    moved=[row for row in original if not (plan[1]/row['relative_path']).exists()]
    assert len(moved)==1 and moved[0]['kind']=='file'
    with Session(plan[0]) as db:
        assert db.execute(text("SELECT COUNT(*) FROM timelapse_retirement_moves WHERE phase='staged'")).scalar_one()==0
        db.execute(text("UPDATE users SET role='admin' WHERE id=3"));db.commit()
    monkeypatch.setattr(implementation,'commit_stage_step',actual)
    assert stage(plan).state=='staged';retained(prepared_stage)


def test_original_absence_is_checked_before_staging_and_never_adopts_late_file(owned_plan):
    import shutil
    from modules.archives.timelapse_staging import StagingUnavailable
    owned_plan[4].unlink();shutil.rmtree(owned_plan[5].parent)
    prepare(owned_plan);bind(owned_plan)
    owned_plan[4].write_bytes(b'owned late file')
    with pytest.raises(StagingUnavailable):stage(owned_plan)
    assert owned_plan[4].read_bytes()==b'owned late file'
    with Session(owned_plan[0]) as db:
        assert db.execute(text('SELECT state FROM timelapse_retirements')).scalar_one()=='pending'
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_moves')).scalar_one()==0



# The child's safety guard accepts only DB/media paths inside the pytest-owned temp
# tree; that is the session basetemp, which --basetemp may place outside $TMPDIR.
_BASETEMP = []


def _pytest_owned_root():
    return _BASETEMP[0]


@pytest.fixture(autouse=True)
def _record_pytest_owned_root(tmp_path_factory):
    _BASETEMP[:] = [tmp_path_factory.getbasetemp().resolve()]
