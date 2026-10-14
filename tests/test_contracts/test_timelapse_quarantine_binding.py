"""Actual owned binding and failure paths. No production media or physical moves."""
import os
import pytest
from sqlalchemy import text,event
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from tests.test_contracts.test_timelapse_retirement_prepare import owned_plan,prepare,preserved


def bind(plan):
    from modules.archives.timelapse_quarantine_binding import bind_quarantine
    return bind_quarantine(*plan[:4])


def test_binding_requires_existing_complete_plan_before_mkdir(owned_plan):
    from modules.archives.timelapse_quarantine_binding import QuarantineBindingUnavailable
    with pytest.raises(QuarantineBindingUnavailable):bind(owned_plan)
    assert not (owned_plan[1]/'.retirement').exists()
    prepare(owned_plan)
    with Session(owned_plan[0]) as db:db.execute(text('DELETE FROM timelapse_retirement_parents'));db.commit()
    with pytest.raises(QuarantineBindingUnavailable):bind(owned_plan)
    assert not (owned_plan[1]/'.retirement').exists()
    preserved(owned_plan)


def test_binding_commits_actual_identity_without_touching_media_and_retries_once(owned_plan):
    prepare(owned_plan);result=bind(owned_plan);folder=owned_plan[1]/'.retirement'
    assert result.created and folder.stat().st_mode&0o777==0o700
    with Session(owned_plan[0]) as db:
        assert db.execute(text('SELECT device,inode,uid,gid,mode FROM timelapse_retirement_quarantine')).one()==(folder.stat().st_dev,folder.stat().st_ino,folder.stat().st_uid,folder.stat().st_gid,folder.stat().st_mode)
    (folder/'owned-existing').write_bytes(b'owned contents')
    assert not bind(owned_plan).created
    assert (folder/'owned-existing').read_bytes()==b'owned contents'
    with Session(owned_plan[0]) as db:assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_quarantine'")).scalar_one()==1
    preserved(owned_plan)


@pytest.mark.parametrize('fault',['insert','audit','commit','acknowledgment','fsync_directory','fsync_root'])
def test_binding_fault_retains_media_and_never_reports_unconfirmed_success(owned_plan,monkeypatch,fault):
    from modules.archives import timelapse_quarantine_binding as module
    from core import audit as dependencies
    prepare(owned_plan);engine=owned_plan[0]
    class FaultSession(Session):
        def commit(self):
            if fault=='commit':raise RuntimeError('owned commit refusal')
            super().commit()
            if fault=='acknowledgment':raise RuntimeError('owned commit acknowledgment uncertain')
    monkeypatch.setattr(module,'Session',FaultSession)
    if fault=='audit':monkeypatch.setattr(dependencies,'log_audit',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('owned audit refusal')))
    if fault.startswith('fsync_'):
        actual_sync=os.fsync;count=0
        def sync(fd):
            nonlocal count
            count+=1
            if count==(1 if fault=='fsync_directory' else 2):raise OSError('owned sync refusal')
            return actual_sync(fd)
        monkeypatch.setattr(os,'fsync',sync)
    def refuse(_db,_cursor,statement,_params,_context,_many):
        if fault=='insert' and statement.lstrip().startswith('INSERT INTO timelapse_retirement_quarantine'):raise RuntimeError('owned binding insert refusal')
    event.listen(engine,'before_cursor_execute',refuse)
    try:
        with pytest.raises(module.QuarantineBindingUnavailable):bind(owned_plan)
    finally:event.remove(engine,'before_cursor_execute',refuse)
    with Session(engine) as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_quarantine')).scalar_one()==(1 if fault=='acknowledgment' else 0)
    assert (owned_plan[1]/'.retirement').is_dir()
    preserved(owned_plan)
    if fault=='acknowledgment':
        monkeypatch.setattr(module,'Session',Session)
        assert not bind(owned_plan).created
        with Session(engine) as db:assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_quarantine'")).scalar_one()==1


@pytest.mark.parametrize('damage',['missing','replacement','mode','symlink'])
def test_existing_binding_never_creates_or_adopts_replacement(owned_plan,monkeypatch,damage):
    from modules.archives.timelapse_quarantine_binding import QuarantineBindingUnavailable
    prepare(owned_plan);bind(owned_plan);folder=owned_plan[1]/'.retirement';original_inode=folder.stat().st_ino
    if damage in ('missing','replacement','symlink'):
        folder.rename(owned_plan[1]/'held-quarantine')
        if damage=='replacement':folder.mkdir(mode=0o700)
        elif damage=='symlink':folder.symlink_to(owned_plan[1]/'held-quarantine',target_is_directory=True)
    else:folder.chmod(0o755)
    actual_mkdir=os.mkdir
    def mkdir(name,*args,**kwargs):
        if name=='.retirement':pytest.fail('Existing binding attempted creation')
        return actual_mkdir(name,*args,**kwargs)
    monkeypatch.setattr(os,'mkdir',mkdir)
    with pytest.raises(QuarantineBindingUnavailable):bind(owned_plan)
    with Session(owned_plan[0]) as db:assert db.execute(text('SELECT inode FROM timelapse_retirement_quarantine')).scalar_one()==original_inode
    if damage=='missing':assert not folder.exists()
    preserved(owned_plan)


def test_caller_session_is_rejected_without_rolling_back_unrelated_write(owned_plan):
    from modules.archives.timelapse_quarantine_binding import bind_quarantine,QuarantineBindingUnavailable
    with Session(owned_plan[0]) as caller:
        caller.execute(text("UPDATE users SET username='owned_pending' WHERE id=3"))
        with pytest.raises(QuarantineBindingUnavailable):bind_quarantine(caller,*owned_plan[1:4])
        assert caller.in_transaction() and caller.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()=='owned_pending'
        assert not (owned_plan[1]/'.retirement').exists()


def test_actor_revoked_after_directory_capture_cannot_commit_binding(owned_plan,monkeypatch):
    from contextlib import contextmanager
    from modules.archives import timelapse_quarantine_binding as module
    prepare(owned_plan);actual=module.quarantine_directory
    @contextmanager
    def capture(*args,**kwargs):
        with actual(*args,**kwargs) as result:
            if kwargs.get('create'):
                with Session(owned_plan[0]) as db:db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));db.commit()
            yield result
    monkeypatch.setattr(module,'quarantine_directory',capture)
    with pytest.raises(module.QuarantineBindingUnavailable):bind(owned_plan)
    with Session(owned_plan[0]) as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_quarantine')).scalar_one()==0
    preserved(owned_plan)


def test_unsafe_unbound_directory_is_not_chmodded_or_cleaned(owned_plan):
    from modules.archives.timelapse_quarantine_binding import QuarantineBindingUnavailable
    prepare(owned_plan);folder=owned_plan[1]/'.retirement';folder.mkdir(mode=0o755);(folder/'owned-existing').write_bytes(b'preserve')
    with pytest.raises(QuarantineBindingUnavailable):bind(owned_plan)
    assert folder.stat().st_mode&0o777==0o755 and (folder/'owned-existing').read_bytes()==b'preserve'
    preserved(owned_plan)
