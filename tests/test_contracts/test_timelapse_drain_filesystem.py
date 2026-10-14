"""Actual descriptor unlink/sync with owned staged media and real receipts.

Admission/progress SQL below is a fixture, not certification of a drain service.
"""
from contextlib import contextmanager
import os
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from tests.test_contracts.test_timelapse_retirement_prepare import owned_plan,prepare
from tests.test_contracts.test_timelapse_quarantine_binding import bind
from tests.test_contracts.test_timelapse_staging import stage


@pytest.fixture
def staged_drain(owned_plan):
    prepare(owned_plan);bind(owned_plan);stage(owned_plan)
    return owned_plan


@contextmanager
def filesystem(plan):
    from core.timelapse_manifest import _root
    from core.timelapse_media_lock import acquire_media_guard
    from core.timelapse_quarantine import quarantine_directory
    from core.timelapse_drain_fs import DrainFilesystem
    from modules.archives.timelapse_stage_transactions import load_stage_plan,directory_identity
    with acquire_media_guard(plan[1],plan[2]) as guard:
        with _root(plan[1]) as (root_fd,root_identity):
            guard.assert_root_identity(root_identity)
            snapshot=load_stage_plan(plan[0],plan[2],plan[3],root_identity)
            with Session(plan[0]) as db:
                receipts={row['object_index']:dict(row) for row in db.execute(text('SELECT * FROM timelapse_retirement_unlinks')).mappings()}
            protected=directory_identity('.retirement',snapshot.quarantine_row)
            parents={path:directory_identity(path,row) for path,row in snapshot.parent_rows.items()}
            with quarantine_directory(plan[1],root_identity,protected) as (fd,_):
                yield DrainFilesystem(root_fd,fd,root_identity,protected,snapshot.objects,parents,snapshot.move_rows,receipts,guard)


def pending(plan,fs,row):
    from core.timelapse_unlink_schema import unlinks
    value={'operation_id':row['operation_id'],'object_index':row['object_index'],'phase':'unlink_pending'}
    with Session(plan[0]) as db:db.execute(unlinks.insert(),value);db.commit()
    fs.unlinks[row['object_index']]=value


def destination(plan,row):
    return plan[1]/'.retirement'/row['quarantine_name']


def first_file(fs):
    return next(row for row in fs.plan if row['kind']=='file')


def test_actual_unlink_sync_only_after_pending_receipt_and_leaves_metadata(staged_drain,monkeypatch):
    from core.timelapse_drain_fs import DrainFilesystemUnavailable
    plan=staged_drain;calls=[];actual=os.fsync
    with filesystem(plan) as fs:
        fs.preflight();row=first_file(fs);file=destination(plan,row)
        with pytest.raises(DrainFilesystemUnavailable):fs.unlink_and_sync(row)
        assert file.exists()
        pending(plan,fs,row)
        monkeypatch.setattr(os,'fsync',lambda fd:(calls.append(not file.exists()),actual(fd))[-1])
        assert fs.unlink_and_sync(row)=='absence_synced'
        assert calls==[True] and not file.exists()
        assert fs.unlink_and_sync(row)=='absence_synced'
    with Session(plan[0]) as db:
        assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='deleting'
        assert db.execute(text('SELECT phase FROM timelapse_retirement_unlinks')).scalar_one()=='unlink_pending'
        assert db.execute(text('SELECT completed_at FROM timelapse_retirements')).scalar_one() is None


@pytest.mark.parametrize('kind',['file','directory'])
def test_unlink_then_exception_retries_exact_pending_absence(staged_drain,monkeypatch,kind):
    plan=staged_drain;function='unlink' if kind=='file' else 'rmdir';actual=getattr(os,function)
    with filesystem(plan) as fs:
        row=next(value for value in fs.plan if value['kind']==kind);pending(plan,fs,row)
        def interrupted(*args,**kwargs):
            actual(*args,**kwargs);raise OSError('owned interrupted deletion')
        monkeypatch.setattr(os,function,interrupted)
        with pytest.raises(OSError):fs.unlink_and_sync(row)
        assert not destination(plan,row).exists()
    monkeypatch.setattr(os,function,actual)
    with filesystem(plan) as fs:
        row=next(value for value in fs.plan if value['kind']==kind)
        fs.preflight();assert fs.unlink_and_sync(row)=='absence_synced'


def test_fsync_failure_keeps_pending_and_retry_syncs_absence(staged_drain,monkeypatch):
    plan=staged_drain;actual=os.fsync
    with filesystem(plan) as fs:
        row=first_file(fs);pending(plan,fs,row)
        monkeypatch.setattr(os,'fsync',lambda fd:(_ for _ in ()).throw(OSError('owned sync refusal')))
        with pytest.raises(OSError):fs.unlink_and_sync(row)
        assert not destination(plan,row).exists()
    monkeypatch.setattr(os,'fsync',actual)
    with filesystem(plan) as fs:
        fs.preflight();assert fs.unlink_and_sync(first_file(fs))=='absence_synced'


@pytest.mark.parametrize('damage',['replacement','contents','hardlink','symlink','nonempty_directory','original_root','external_parent','quarantine'])
def test_foreign_or_changed_entries_never_removed(staged_drain,damage):
    from core.timelapse_staging_fs import StagingFilesystemUnavailable
    from core.timelapse_manifest import ManifestUnavailable
    from core.timelapse_quarantine import QuarantineDirectoryUnavailable
    from contextlib import nullcontext
    plan=staged_drain
    with (pytest.raises(QuarantineDirectoryUnavailable) if damage=='quarantine' else nullcontext()):
      with filesystem(plan) as fs:
        row=next(value for value in fs.plan if value['kind']=='directory') if damage=='nonempty_directory' else first_file(fs)
        pending(plan,fs,row);file=destination(plan,row)
        if damage=='replacement':file.rename(plan[1]/'held-original');file.write_bytes(b'owned replacement');foreign=file
        elif damage=='contents':file.write_bytes(b'owned changed');foreign=file
        elif damage=='hardlink':foreign=plan[1]/'owned-alias';os.link(file,foreign)
        elif damage=='symlink':file.rename(plan[1]/'held-original');file.symlink_to(plan[1]/'held-original');foreign=file
        elif damage=='nonempty_directory':foreign=file/'owned-child';foreign.write_bytes(b'owned child')
        elif damage=='original_root':foreign=plan[4];foreign.write_bytes(b'owned new original')
        elif damage=='external_parent':
            plan[4].parent.rename(plan[4].parent.with_name('held-parent'));plan[4].parent.mkdir();foreign=plan[4];foreign.write_bytes(b'owned new parent')
        else:
            (plan[1]/'.retirement').rename(plan[1]/'held-quarantine');(plan[1]/'.retirement').mkdir(mode=0o700);foreign=plan[1]/'.retirement'/'new';foreign.write_bytes(b'owned foreign quarantine')
        before=foreign.lstat()
        with pytest.raises((StagingFilesystemUnavailable,ManifestUnavailable)):fs.unlink_and_sync(row)
        assert foreign.lstat().st_ino==before.st_ino


def test_absence_without_pending_is_not_adopted(staged_drain):
    from core.timelapse_drain_fs import DrainFilesystemUnavailable
    plan=staged_drain
    with filesystem(plan) as fs:
        row=first_file(fs);destination(plan,row).unlink()
        with pytest.raises(DrainFilesystemUnavailable):fs.preflight()


@pytest.mark.parametrize('phase,state',[('unlink_confirmed','unlink_confirmed'),('fsync_confirmed','fsync_confirmed'),('retired','retired')])
def test_confirmed_receipt_never_deletes_reappeared_destination(staged_drain,phase,state):
    from core.timelapse_staging_fs import StagingFilesystemUnavailable
    plan=staged_drain
    with filesystem(plan) as fs:
        row=first_file(fs);pending(plan,fs,row);fs.unlink_and_sync(row)
        with Session(plan[0]) as db:
            db.execute(text('UPDATE timelapse_retirement_objects SET state=:state WHERE object_index=:index'),{'state':state,'index':row['object_index']})
            db.execute(text('UPDATE timelapse_retirement_unlinks SET phase=:phase'),{'phase':phase});db.commit()
        file=destination(plan,row);file.write_bytes(b'owned replacement after confirmation')
    with filesystem(plan) as fs:
        with pytest.raises(StagingFilesystemUnavailable):fs.preflight()
        assert file.read_bytes()==b'owned replacement after confirmation'


def test_deadline_and_hash_budget_refuse_before_unlink(staged_drain):
    from core.timelapse_staging_fs import StagingFilesystemUnavailable
    plan=staged_drain
    with filesystem(plan) as fs:
        row=first_file(fs);pending(plan,fs,row);fs.remaining=0
        with pytest.raises(StagingFilesystemUnavailable) as error:fs.unlink_and_sync(row)
        assert error.value.reason=='manifest_limit' and destination(plan,row).exists()
        fs.deadline=0
        with pytest.raises(StagingFilesystemUnavailable) as error:fs.unlink_and_sync(row)
        assert error.value.reason=='manifest_limit' and destination(plan,row).exists()


def test_full_owned_file_and_empty_directory_removal_preserves_foreign_sibling(staged_drain):
    plan=staged_drain
    foreign=plan[1]/'.retirement'/'another-operation';foreign.write_bytes(b'owned unrelated sibling')
    with filesystem(plan) as fs:
        fs.preflight()
        ordered=sorted(fs.plan,key=lambda row:(row['kind']=='directory',-row['relative_path'].count('/'),row['object_index']))
        for row in ordered:
            pending(plan,fs,row)
            assert fs.unlink_and_sync(row)=='absence_synced'
        fs.preflight()
        assert all(not destination(plan,row).exists() for row in fs.plan)
        assert foreign.read_bytes()==b'owned unrelated sibling'
    with Session(plan[0]) as db:
        assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='deleting'
        assert db.execute(text("SELECT COUNT(*) FROM timelapse_retirement_unlinks WHERE phase='unlink_pending'")).scalar_one()==5


@pytest.mark.parametrize('kind,crash',[('file','unlink'),('directory','unlink'),('file','sync')])
def test_actual_process_death_after_unlink_or_sync_and_fresh_retry(staged_drain,kind,crash):
    import subprocess,sys,json
    from pathlib import Path
    plan=staged_drain;repo=Path(__file__).resolve().parents[2]
    with filesystem(plan) as fs:
        row=next(value for value in fs.plan if value['kind']==kind);pending(plan,fs,row)
        index=row['object_index'];file=destination(plan,row)
    command=[sys.executable,str(repo/'artifacts/application-certification/run_owned_timelapse_unlink.py'),str(plan[0].url.database),str(plan[1]),str(plan[2]),str(index)]
    env=dict(os.environ,PYTHONPATH=str(repo/'backend')+':'+str(repo),BASE_URL='http://127.0.0.1:1')
    ended=subprocess.run(command+[crash],cwd=repo,env=env,capture_output=True,text=True,timeout=30)
    assert ended.returncode==(74 if crash=='unlink' else 75),{'returncode':ended.returncode,'output':ended.stdout}
    assert not file.exists()
    with Session(plan[0]) as db:
        assert db.execute(text('SELECT phase FROM timelapse_retirement_unlinks')).scalar_one()=='unlink_pending'
        assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='deleting'
    resumed=subprocess.run(command+['none'],cwd=repo,env=env,capture_output=True,text=True,timeout=30)
    assert resumed.returncode==0,{'returncode':resumed.returncode,'output':resumed.stdout}
    assert json.loads(resumed.stdout)['status']=='absence_synced'


@pytest.mark.parametrize('change',['unsafe_name','wrong_operation','wrong_receipt','wrong_kind','phase_mismatch'])
def test_malformed_admission_refuses_before_any_unlink(staged_drain,change):
    from core.timelapse_drain_fs import DrainFilesystem
    from core.timelapse_staging_fs import StagingFilesystemUnavailable
    from core.timelapse_rename import RenameUnavailable
    plan=staged_drain
    with filesystem(plan) as fs:
        row=first_file(fs);pending(plan,fs,row);file=destination(plan,row)
        if change=='unsafe_name':row['quarantine_name']='../outside'
        elif change=='wrong_operation':row['operation_id']='different'
        elif change=='wrong_receipt':fs.unlinks[row['object_index']]['operation_id']='different'
        elif change=='wrong_kind':fs.moves[row['object_index']]['kind']='directory'
        else:row['state']='retired'
        with pytest.raises((StagingFilesystemUnavailable,RenameUnavailable)):
            DrainFilesystem(fs.root_fd,fs.quarantine_fd,fs.root,fs.quarantine,fs.plan,fs.parents,fs.moves,fs.unlinks,fs.guard)
        assert file.exists()
