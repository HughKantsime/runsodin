"""Actual internal unlink admissions/progress/final commits on owned DB/media."""
import os
import pytest
from sqlalchemy import text,event
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from tests.test_contracts.test_timelapse_retirement_prepare import owned_plan,prepare
from tests.test_contracts.test_timelapse_quarantine_binding import bind
from tests.test_contracts.test_timelapse_staging import stage


@pytest.fixture
def staged_plan(owned_plan):
    prepare(owned_plan);bind(owned_plan);stage(owned_plan)
    return owned_plan


def drain(plan):
    from modules.archives.timelapse_drain import drain_retirement
    return drain_retirement(*plan[:4])


def saved(plan):
    with Session(plan[0]) as db:
        return dict(db.execute(text('SELECT * FROM timelapse_retirements')).mappings().one())


def media_paths(plan):
    with Session(plan[0]) as db:
        names=db.execute(text('SELECT quarantine_name FROM timelapse_retirement_objects WHERE quarantine_name IS NOT NULL')).scalars().all()
    return [plan[1]/'.retirement'/name for name in names]


def test_full_drain_commits_retired_evidence_metadata_removal_and_single_audit(staged_plan):
    from modules.archives.timelapse_drain import retirement_receipt
    plan=staged_plan;op=saved(plan)['operation_id'];paths=media_paths(plan)
    result=drain(plan);assert result.operation_id==op and result.state=='complete'
    assert all(not path.exists() for path in paths)
    with Session(plan[0]) as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapses')).scalar_one()==0
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_ownership_anchors')).scalar_one()==1
        assert set(db.execute(text('SELECT state FROM timelapse_retirement_objects')).scalars())=={'retired'}
        assert set(db.execute(text('SELECT phase FROM timelapse_retirement_unlinks')).scalars())=={'retired'}
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_drain'")).scalar_one()==1
    assert saved(plan)['completed_at'] is not None
    assert retirement_receipt(plan[0],op,plan[3]).state=='complete'


@pytest.mark.parametrize('target,ack',[(1,False),(1,True),(2,False),(2,True),(3,False),(3,True),(4,False),(4,True),(21,False),(21,True)])
def test_commit_and_ack_faults_recover_exact_durable_progress(staged_plan,monkeypatch,target,ack):
    from modules.archives import timelapse_drain_transactions as transactions
    from modules.archives.timelapse_drain import DrainUnavailable,retirement_receipt
    plan=staged_plan;op=saved(plan)['operation_id'];paths=media_paths(plan);commits=0
    class FaultSession(Session):
        def commit(self):
            nonlocal commits
            commits+=1
            if commits==target and not ack:raise RuntimeError('owned refused drain commit')
            super().commit()
            if commits==target and ack:raise RuntimeError('owned unconfirmed drain acknowledgment')
    monkeypatch.setattr(transactions,'Session',FaultSession)
    with pytest.raises(DrainUnavailable):drain(plan)
    assert commits==target
    if target==1:assert all(path.exists() for path in paths)
    if target==21 and ack:
        assert retirement_receipt(plan[0],op,plan[3]).state=='complete'
    else:
        assert saved(plan)['state']=='in_flight'
        monkeypatch.setattr(transactions,'Session',Session)
        assert drain(plan).state=='complete'
    with Session(plan[0]) as db:
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_drain'")).scalar_one()==1


@pytest.mark.parametrize('fault',['drain_audit','final_audit','final_delete','fsync'])
def test_real_faults_preserve_retry_evidence_and_never_claim_complete(staged_plan,monkeypatch,fault):
    from core import audit
    from modules.archives.timelapse_drain import DrainUnavailable
    plan=staged_plan;paths=media_paths(plan);actual_audit=audit.log_audit;actual_sync=os.fsync
    def audited(db,action,*args,**kwargs):
        if action==('retirement_drain' if fault=='drain_audit' else 'retirement_complete') and fault in ('drain_audit','final_audit'):raise RuntimeError('owned refused audit')
        return actual_audit(db,action,*args,**kwargs)
    monkeypatch.setattr(audit,'log_audit',audited)
    def refuse(_db,_cursor,statement,_params,_context,_many):
        if fault=='final_delete' and statement.startswith('DELETE FROM timelapses'):raise RuntimeError('owned refused final deletion')
    event.listen(plan[0],'before_cursor_execute',refuse)
    if fault=='fsync':monkeypatch.setattr(os,'fsync',lambda fd:(_ for _ in ()).throw(OSError('owned sync failure')))
    try:
        with pytest.raises(DrainUnavailable):drain(plan)
    finally:event.remove(plan[0],'before_cursor_execute',refuse)
    assert saved(plan)['state']=='in_flight' and saved(plan)['completed_at'] is None
    with Session(plan[0]) as db:assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='deleting'
    if fault=='drain_audit':assert all(path.exists() for path in paths)
    monkeypatch.setattr(audit,'log_audit',actual_audit);monkeypatch.setattr(os,'fsync',actual_sync)
    assert drain(plan).state=='complete'


def test_revocation_after_admitted_unlink_blocks_progress_and_further_objects(staged_plan,monkeypatch):
    from modules.archives.timelapse_drain import DrainUnavailable
    plan=staged_plan;paths=media_paths(plan);actual=os.unlink;done=False
    def revoked(*args,**kwargs):
        nonlocal done
        actual(*args,**kwargs)
        if not done:
            done=True
            with Session(plan[0]) as db:db.execute(text('UPDATE users SET is_active=0 WHERE id=3'));db.commit()
    monkeypatch.setattr(os,'unlink',revoked)
    with pytest.raises(DrainUnavailable) as error:drain(plan)
    assert error.value.reason=='unknown_authority'
    assert sum(not path.exists() for path in paths)==1
    with Session(plan[0]) as db:
        assert db.execute(text('SELECT phase FROM timelapse_retirement_unlinks')).scalar_one()=='unlink_pending'
        db.execute(text('UPDATE users SET is_active=1 WHERE id=3'));db.commit()
    monkeypatch.setattr(os,'unlink',actual)
    assert drain(plan).state=='complete'


def test_completed_operation_receipt_survives_id_reuse_without_adopting_new_row(staged_plan):
    from modules.archives.timelapse_drain import retirement_receipt,DrainUnavailable
    plan=staged_plan;result=drain(plan)
    with Session(plan[0]) as db:
        db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status) VALUES(:id,77,'new-unrelated.mp4','capturing')"),{'id':plan[2]});db.commit()
    assert retirement_receipt(plan[0],result.operation_id,plan[3]).operation_id==result.operation_id
    with pytest.raises(DrainUnavailable):retirement_receipt(plan[0],'unrelated',plan[3])
    with pytest.raises(DrainUnavailable):retirement_receipt(plan[0],result.operation_id,{**plan[3],'group_id':2})
    with Session(plan[0]) as db:assert db.execute(text('SELECT filename,status FROM timelapses')).one()==('new-unrelated.mp4','capturing')


def test_drain_requires_fully_staged_plan_before_any_unlink(owned_plan):
    from modules.archives.timelapse_drain import DrainUnavailable
    prepare(owned_plan);bind(owned_plan)
    with pytest.raises(DrainUnavailable):drain(owned_plan)
    assert owned_plan[4].exists() and (owned_plan[5]/'one.jpg').exists()


@pytest.mark.parametrize('damage',['missing_receipt','unretired_object','missing_move','wrong_root'])
def test_completed_flag_without_exact_retired_evidence_cannot_certify(staged_plan,damage):
    from modules.archives.timelapse_drain import retirement_receipt,DrainUnavailable
    plan=staged_plan;result=drain(plan)
    with Session(plan[0]) as db:
        if damage=='missing_receipt':db.execute(text('DELETE FROM timelapse_retirement_unlinks'))
        elif damage=='unretired_object':db.execute(text("UPDATE timelapse_retirement_objects SET state='staged'"))
        elif damage=='missing_move':db.execute(text('DELETE FROM timelapse_retirement_moves'))
        else:db.execute(text('UPDATE timelapse_retirements SET root_inode=root_inode+1'))
        db.commit()
    with pytest.raises(DrainUnavailable):retirement_receipt(plan[0],result.operation_id,plan[3])


@pytest.mark.parametrize('target',['file','directory','sync','final_ack'])
def test_actual_service_process_death_and_fresh_progress_or_receipt_recovery(staged_plan,target):
    import subprocess,sys,json
    from pathlib import Path
    plan=staged_plan;op=saved(plan)['operation_id'];repo=Path(__file__).resolve().parents[2]
    command=[sys.executable,str(repo/'tests/fixtures/certification/run_owned_timelapse_drain.py'),str(plan[0].url.database),str(plan[1]),str(plan[2])]
    env=dict(os.environ,PYTHONPATH=str(repo/'backend')+':'+str(repo),ODIN_OWNED_TEST_ROOT=str(_pytest_owned_root()),BASE_URL='http://127.0.0.1:1')
    ended=subprocess.run(command+[target],cwd=repo,env=env,capture_output=True,text=True,timeout=30)
    assert ended.returncode==({'sync':75,'final_ack':76}.get(target,74)),{'returncode':ended.returncode,'output':ended.stdout}
    if target=='final_ack':
        with Session(plan[0]) as db:
            db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status) VALUES(:id,77,'new-unrelated.mp4','capturing')"),{'id':plan[2]});db.commit()
    resumed=subprocess.run(command+(['receipt',op] if target=='final_ack' else ['none']),cwd=repo,env=env,capture_output=True,text=True,timeout=30)
    assert resumed.returncode==0,{'returncode':resumed.returncode,'output':resumed.stdout}
    assert json.loads(resumed.stdout)['status']=='complete'
    with Session(plan[0]) as db:
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1
        if target=='final_ack':assert db.execute(text('SELECT filename,status FROM timelapses')).one()==('new-unrelated.mp4','capturing')


@pytest.mark.parametrize('provider_present',[True,False])
def test_education_completed_receipt_preserves_current_policy_or_refuses_without_provider(review_db,tmp_path,monkeypatch,provider_present):
    from types import SimpleNamespace
    from core.registry import registry
    from modules.organizations.education_review_service import archive_audience_sql
    from tests.test_contracts.test_timelapse_content_audience import capture_education
    from modules.archives.timelapse_drain import retirement_receipt,DrainUnavailable
    original=registry.get_provider
    provider=SimpleNamespace(archive_audience_sql=archive_audience_sql)
    monkeypatch.setattr(registry,'get_provider',lambda name:provider if name=='EducationPolicyProvider' else original(name))
    tid=capture_education(review_db)
    filename=review_db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one();review_db.rollback()
    root=tmp_path/'owned-media';root.mkdir(mode=0o700);video=root/filename;video.parent.mkdir();video.write_bytes(b'OWNED_EDUCATION')
    plan=(review_db.get_bind(),root,tid,{'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'},video,root/str(tid))
    prepare(plan);bind(plan);stage(plan);result=drain(plan)
    assert retirement_receipt(plan[0],result.operation_id,plan[3]).state=='complete'
    if not provider_present:
        monkeypatch.setattr(registry,'get_provider',lambda name:None if name=='EducationPolicyProvider' else original(name))
        with pytest.raises(DrainUnavailable):retirement_receipt(plan[0],result.operation_id,plan[3])
    else:
        with Session(plan[0]) as db:db.execute(text('UPDATE users SET is_active=0 WHERE id=3'));db.commit()
        with pytest.raises(DrainUnavailable):retirement_receipt(plan[0],result.operation_id,plan[3])
    assert saved(plan)['state']=='complete'


@pytest.mark.parametrize('damage',['missing','device','uid','gid','mode','inode'])
def test_completed_receipt_requires_original_quarantine_binding(staged_plan,damage):
    from modules.archives.timelapse_drain import retirement_receipt,DrainUnavailable
    plan=staged_plan;result=drain(plan)
    with Session(plan[0]) as db:
        if damage=='missing':db.execute(text('DELETE FROM timelapse_retirement_quarantine'))
        elif damage in ('mode','inode'):
            db.execute(text('PRAGMA ignore_check_constraints=ON'))
            db.execute(text('UPDATE timelapse_retirement_quarantine SET '+damage+'=0'))
            db.execute(text('PRAGMA ignore_check_constraints=OFF'))
        else:
            assert damage in ('device','uid','gid')
            db.execute(text('UPDATE timelapse_retirement_quarantine SET '+damage+'='+damage+'+1'))
        db.commit()
    with pytest.raises(DrainUnavailable):retirement_receipt(plan[0],result.operation_id,plan[3])



# The child's safety guard accepts only DB/media paths inside the pytest-owned temp
# tree; that is the session basetemp, which --basetemp may place outside $TMPDIR.
_BASETEMP = []


def _pytest_owned_root():
    return _BASETEMP[0]


@pytest.fixture(autouse=True)
def _record_pytest_owned_root(tmp_path_factory):
    _BASETEMP[:] = [tmp_path_factory.getbasetemp().resolve()]
