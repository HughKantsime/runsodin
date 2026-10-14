"""Owned multi-pending retirement admission boundaries, actual database and FS."""
import pytest
from sqlalchemy import text,event
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_lifetime_action import credential
from tests.test_contracts.test_timelapse_lifetime_membership import prepared_source,prepare,encoder,frame_writer,review_db,owned_runtime
from modules.archives import timelapse_lifetime_action as service


@pytest.fixture(autouse=True)
def reconnects_keep_foreign_keys(prepared_source):
    engine=prepared_source[0]
    def enable(connection,record):connection.execute('PRAGMA foreign_keys=ON')
    event.listen(engine,'connect',enable)
    try:
        with engine.connect() as db:assert db.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==1
        yield
    finally:event.remove(engine,'connect',enable)


@pytest.mark.parametrize('kind',['files','unlinks'])
@pytest.mark.parametrize('position',['first','middle','last'])
def test_any_batch_row_failure_rolls_back_every_row_and_revision(prepared_source,monkeypatch,kind,position):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);batch=service._admit_batch;delta=service._journal_delta;active=[];baseline=[];fired=[]
    def wrapped(db,plan,actual_kind):
        if actual_kind==kind:
            rows=[r for r in plan['objects'] if r['kind']=='file' and r['state']=='planned'] if kind=='files' else [r for r in plan['objects'] if r['state']=='staged' and r['object_index'] not in {u['object_index'] for u in plan['unlinks']}]
            indices=sorted(r['object_index'] for r in rows);assert len(indices)>=3
            active.append(indices[{'first':0,'middle':len(indices)//2,'last':len(indices)-1}[position]])
            baseline.append(plan['intent']['revision'])
        return batch(db,plan,actual_kind)
    def failed(db,plan,key,values,**kwargs):
        result=delta(db,plan,key,values,**kwargs)
        if active and kwargs.get('index')==active[0] and key==('moves' if kind=='files' else 'unlinks'):
            fired.append(True);raise RuntimeError('owned batch row fault')
        return result
    monkeypatch.setattr(service,'_admit_batch',wrapped);monkeypatch.setattr(service,'_journal_delta',failed)
    with pytest.raises(RuntimeError,match='batch row fault'):service.retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce)
    assert fired
    with Session(engine) as db:
        assert db.execute(text('SELECT revision FROM timelapse_retirements')).scalar_one()==baseline[0]
        if kind=='files':assert db.execute(text("SELECT count(*) FROM timelapse_retirement_moves WHERE phase<>'planned'")).scalar_one()==0
        else:assert db.execute(text('SELECT count(*) FROM timelapse_retirement_unlinks')).scalar_one()==0


@pytest.mark.parametrize('kind',['files','unlinks'])
def test_durable_multiple_pending_restart_keeps_directory_dependencies(prepared_source,monkeypatch,kind):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);batch=service._admit_batch;take=service._take_handoff;stop=[]
    def wrapped(db,plan,actual_kind):
        result=batch(db,plan,actual_kind)
        if actual_kind=='files':
            for row in plan['objects']:
                if row['kind']=='directory':assert next(m for m in plan['moves'] if m['object_index']==row['object_index'])['phase']=='planned'
        if actual_kind==kind:stop.append(True)
        return result
    def crash(*args,**kwargs):
        result=take(*args,**kwargs)
        if stop:raise RuntimeError('owned after batch commit crash')
        return result
    monkeypatch.setattr(service,'_admit_batch',wrapped);monkeypatch.setattr(service,'_take_handoff',crash)
    evidence=credential(engine)
    with pytest.raises(RuntimeError,match='batch commit crash'):service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
    with Session(engine) as db:
        table='timelapse_retirement_moves' if kind=='files' else 'timelapse_retirement_unlinks';phase='rename_pending' if kind=='files' else 'unlink_pending'
        assert db.execute(text('SELECT count(*) FROM '+table+' WHERE phase=:phase'),{'phase':phase}).scalar_one()>=3
    monkeypatch.setattr(service,'_admit_batch',batch);monkeypatch.setattr(service,'_take_handoff',take)
    assert service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce).state=='complete'


@pytest.mark.parametrize('position',['first','middle','last'])
def test_fresh_authority_before_first_middle_last_file_apply(prepared_source,monkeypatch,position):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source)
    with Session(engine) as db:n=db.execute(text("SELECT count(*) FROM timelapse_retirement_objects WHERE kind='file' AND state='planned'")).scalar_one()
    target={'first':1,'middle':n//2+1,'last':n}[position];verify=service.verify_action;seen=[];fired=[]
    def changed(fs,row,**kwargs):
        token=verify(fs,row,**kwargs)
        if kwargs['action']=='rename' and row['kind']=='file':
            seen.append(row['object_index'])
            if len(seen)==target:
                with engine.begin() as c:c.execute(text('UPDATE timelapse_retirements SET revision=revision+1'))
                fired.append(row['object_index'])
        return token
    monkeypatch.setattr(service,'verify_action',changed)
    with pytest.raises(service.RetirementUnavailable):service.retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce)
    assert len(seen)==target and fired
    with Session(engine) as db:assert db.execute(text('SELECT phase FROM timelapse_retirement_moves WHERE object_index=:i'),{'i':fired[0]}).scalar_one()=='rename_pending'


@pytest.mark.parametrize('kind',['files','unlinks'])
@pytest.mark.parametrize('fault',['commit','ack'])
def test_batch_commit_uncertainty_emits_no_handoff_and_restarts(prepared_source,monkeypatch,kind,fault):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);batch=service._admit_batch;take=service._take_handoff;commit=Session.commit;armed=[];consumed=[];before=[]
    def wrapped(db,plan,actual_kind):
        result=batch(db,plan,actual_kind)
        if actual_kind==kind:armed.append(True);before.append(len(consumed))
        return result
    def counted(*args,**kwargs):consumed.append(True);return take(*args,**kwargs)
    def failed(db):
        if armed:
            if fault=='ack':commit(db)
            raise RuntimeError('owned batch '+fault+' uncertainty')
        return commit(db)
    monkeypatch.setattr(service,'_admit_batch',wrapped);monkeypatch.setattr(service,'_take_handoff',counted);monkeypatch.setattr(Session,'commit',failed)
    evidence=credential(engine)
    with pytest.raises(RuntimeError,match='uncertainty'):service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
    assert len(consumed)==before[0]
    with Session(engine) as db:
        table='timelapse_retirement_moves' if kind=='files' else 'timelapse_retirement_unlinks';phase='rename_pending' if kind=='files' else 'unlink_pending'
        count=db.execute(text('SELECT count(*) FROM '+table+' WHERE phase=:phase'),{'phase':phase}).scalar_one()
        assert count>=3 if fault=='ack' else count==0
    monkeypatch.setattr(service,'_admit_batch',batch);monkeypatch.setattr(service,'_take_handoff',take);monkeypatch.setattr(Session,'commit',commit)
    assert service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce).state=='complete'


def test_admission_reduction_matches_actual_sealed_object_counts(prepared_source,monkeypatch):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source)
    with Session(engine) as db:
        physical=db.execute(text("SELECT count(*) FROM timelapse_retirement_objects WHERE state<>'originally_absent'")).scalar_one()
        directories=db.execute(text("SELECT count(*) FROM timelapse_retirement_objects WHERE state<>'originally_absent' AND kind='directory'")).scalar_one()
    load=service._load;calls=[]
    def counted(*args,**kwargs):calls.append(bool(kwargs.get('lock')));return load(*args,**kwargs)
    monkeypatch.setattr(service,'_load',counted)
    assert service.retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    assert len(calls)==4*physical+directories+5
    assert calls.count(False)==1
    assert (6*physical+3)-len(calls)==(physical-directories-1)+(physical-1)>0
