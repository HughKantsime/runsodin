"""Actual journal/commit handoffs retain fresh next-transaction authority."""
import pytest
from sqlalchemy.orm import Session
from sqlalchemy import text
from tests.test_contracts.test_timelapse_lifetime_action import credential
from tests.test_contracts.test_timelapse_lifetime_membership import prepared_source,prepare,encoder,frame_writer,review_db,owned_runtime
from modules.archives import timelapse_lifetime_action as service


def test_every_committed_delta_equals_fresh_database_snapshot(prepared_source,monkeypatch):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);evidence=credential(engine)
    take=service._take_handoff;seen=[]
    def checked(handoff,guard,actual_tid,actual_nonce,identity):
        result=take(handoff,guard,actual_tid,actual_nonce,identity)
        fresh=service._refresh(engine,evidence,actual_tid,actual_nonce,identity,guard=guard)
        assert result==fresh
        assert not handoff
        seen.append(result['intent']['revision'])
        return result
    monkeypatch.setattr(service,'_take_handoff',checked)
    assert service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce).state=='complete'
    assert len(seen)>5 and seen==sorted(set(seen))


def test_only_initial_refresh_remaining(prepared_source,monkeypatch):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);refresh=service._refresh;calls=[]
    def counted(*args,**kwargs):calls.append(True);return refresh(*args,**kwargs)
    monkeypatch.setattr(service,'_refresh',counted)
    assert service.retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    assert len(calls)==1


def test_lost_commit_ack_never_emits_handoff(prepared_source,monkeypatch):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);evidence=credential(engine)
    commit=Session.commit;emitted=[];constructor=service._CommittedPlan
    def lost(db):commit(db);raise RuntimeError('owned commit ACK uncertainty')
    def captured(*args,**kwargs):emitted.append(True);return constructor(*args,**kwargs)
    monkeypatch.setattr(Session,'commit',lost);monkeypatch.setattr(service,'_CommittedPlan',captured)
    with pytest.raises(RuntimeError,match='ACK uncertainty'):service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
    assert emitted==[] and list(root.glob('frames_*'))


@pytest.mark.parametrize('change',['revision','inactive','publication'])
def test_intercommit_drift_is_not_authorized_by_handoff(prepared_source,monkeypatch,change):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);evidence=credential(engine)
    take=service._take_handoff;fired=[]
    def changed(*args,**kwargs):
        result=take(*args,**kwargs)
        if not fired:
            fired.append(True)
            with engine.begin() as c:
                if change=='revision':c.execute(text('UPDATE timelapse_retirements SET revision=revision+1'))
                elif change=='inactive':c.execute(text('UPDATE users SET is_active=FALSE WHERE id=3'))
                else:c.execute(text("UPDATE timelapse_publications SET state='blocked',reason_code='media_changed' WHERE kind='encode'"))
        return result
    monkeypatch.setattr(service,'_take_handoff',changed)
    with pytest.raises(Exception):service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
    assert fired and list(root.glob('frames_*'))


def test_unrecognized_delta_rolls_back_entire_transaction(prepared_source,monkeypatch):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);delta=service._journal_delta
    def invalid(db,plan,key,values,**kwargs):return delta(db,plan,key,{**values,'unknown_column':1},**kwargs)
    monkeypatch.setattr(service,'_journal_delta',invalid)
    with pytest.raises(service.RetirementUnavailable):service.retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce)
    with Session(engine) as db:
        assert db.execute(text('SELECT count(*) FROM timelapse_retirement_quarantine')).scalar_one()==0
        assert db.execute(text('SELECT state FROM timelapse_retirements')).scalar_one()=='pending'
    assert list(root.glob('frames_*'))


def test_exact_prior_row_cas_zero_refuses_before_rename(prepared_source,monkeypatch):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);delta=service._journal_delta;fired=[]
    def mismatched(db,plan,key,values,**kwargs):
        if key=='moves' and not fired:
            fired.append(True)
            next(r for r in plan[key] if r['object_index']==kwargs['index'])['digest_sha256']='0'*64
        return delta(db,plan,key,values,**kwargs)
    monkeypatch.setattr(service,'_journal_delta',mismatched)
    with pytest.raises(service.RetirementUnavailable):service.retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce)
    assert fired and list(root.glob('frames_*'))
    with Session(engine) as db:
        assert db.execute(text("SELECT count(*) FROM timelapse_retirement_moves WHERE phase<>'planned'")).scalar_one()==0


def test_handoff_is_single_use_and_guard_bound():
    class Guard:
        def assert_root_identity(self,value):assert value=='identity'
    guard=Guard();other=Guard();item=service._CommittedPlan({'value':1},guard,1,'nonce','identity',service._HANDOFF)
    with pytest.raises(service.RetirementUnavailable):service._take_handoff([item],other,1,'nonce','identity')
    holder=[item]
    assert service._take_handoff(holder,guard,1,'nonce','identity')=={'value':1}
    with pytest.raises(service.RetirementUnavailable):service._take_handoff([item],guard,1,'nonce','identity')


@pytest.mark.parametrize('fault',['none','extra','missing','foreign','boolean','wrong'])
def test_insert_returning_requires_one_exact_typed_complete_row(fault):
    row={'operation_id':'owned','object_index':0,'phase':'unlink_pending'}
    returned=dict(row)
    if fault=='missing':returned.pop('phase')
    elif fault=='foreign':returned['foreign']=1
    elif fault=='boolean':returned['object_index']=False
    elif fault=='wrong':returned['phase']='retired'
    rows=[] if fault=='none' else [returned,dict(row)] if fault=='extra' else [returned]
    class Result:
        def mappings(self):return self
        def fetchmany(self,count):assert count==2;return rows
    class DB:
        def execute(self,statement,values):
            assert list(statement._returning)==list(service.unlinks.c) and values==row
            return Result()
    plan={'intent':{'operation_id':'owned'},'unlinks':[]}
    with pytest.raises(service.RetirementUnavailable):service._journal_delta(DB(),plan,'unlinks',{'phase':'unlink_pending'},index=0,insert=True)
    assert plan['unlinks']==[]
