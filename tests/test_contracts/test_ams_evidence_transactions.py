"""Actual SQLite transitions, supersession and caller rollback ownership."""
from types import SimpleNamespace
from datetime import timedelta
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core.ams_evidence_schema import EVIDENCE_TABLES, generations, current_reads
from modules.printers.telemetry.bambu import ams_evidence_transactions as module

RUNTIME=SimpleNamespace(incarnation='a'*32,clock_domain='synthetic-domain')

@pytest.fixture
def db(monkeypatch,tmp_path):
    key=Fernet.generate_key()
    monkeypatch.setenv('ENCRYPTION_KEY',key.decode())
    monkeypatch.setattr(module,'revalidate_runtime_identity',lambda runtime:None)
    engine=create_engine('sqlite:///'+str(tmp_path/'transitions.sqlite'),connect_args={'timeout':0.05})
    with engine.begin() as connection:
        connection.execute(text('PRAGMA foreign_keys=ON'))
        connection.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY,org_id INTEGER,api_type TEXT,api_host TEXT,api_key TEXT,is_active,shared)'))
        connection.execute(text("INSERT INTO printers VALUES(1,2,'bambu','printer.local',:secret,1,0)"),
            {'secret':Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
        for table in EVIDENCE_TABLES:table.create(connection)
    with Session(engine) as session:yield session
    engine.dispose()


def state(db,generation):
    return db.execute(generations.select().where(generations.c.generation_id==generation)).mappings().one()


def test_new_read_supersedes_old_and_late_failure_cannot_replace_pointer(db):
    first=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.commit()
    second=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.commit()
    assert state(db,first.generation_id)['state']=='failed'
    assert state(db,second.generation_id)['state']=='acquiring'
    assert not module._fail_owned_read(db,started=first,runtime=RUNTIME)
    assert db.execute(current_reads.select()).mappings().one()['generation_id']==second.generation_id
    assert module._fail_owned_read(db,started=second,runtime=RUNTIME)
    db.commit()
    assert state(db,second.generation_id)['state']=='failed'
    assert db.execute(current_reads.select()).first() is None
    import importlib
    migration=importlib.import_module('core.schema.migrations.020_ams_evidence')
    migration.validate(db.connection())


def test_begin_does_not_commit_for_caller(db):
    started=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.rollback()
    assert db.execute(generations.select()).first() is None
    assert db.execute(current_reads.select()).first() is None


def test_new_read_invalidates_ready_history_without_deleting_it(db):
    first=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    row=state(db,first.generation_id)
    db.execute(generations.update().where(generations.c.generation_id==first.generation_id)
        .values(state='ready',completed_at=row['started_at']))
    db.commit()
    second=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.commit()
    old=state(db,first.generation_id)
    assert old['state']=='invalidated' and old['invalidated_at']>=old['completed_at']
    assert state(db,second.generation_id)['state']=='acquiring'


def test_changed_runtime_after_mutation_requires_caller_rollback(db,monkeypatch):
    calls=[]
    def validate(runtime):
        calls.append(runtime)
        if len(calls)==2:raise ValueError('synthetic private failure')
    monkeypatch.setattr(module,'revalidate_runtime_identity',validate)
    with pytest.raises(module.AMSTransitionUnavailable):module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.rollback()
    assert db.execute(generations.select()).first() is None


def test_backwards_database_clock_refuses_without_rewriting_history(db,monkeypatch):
    first=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.commit()
    started=module._instant(state(db,first.generation_id)['started_at'])
    monkeypatch.setattr(module,'sample_database_clock',lambda db:SimpleNamespace(database_now=started-timedelta(seconds=1)))
    with pytest.raises(module.AMSTransitionUnavailable):module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.rollback()
    assert state(db,first.generation_id)['state']=='acquiring'
    assert db.execute(current_reads.select()).mappings().one()['generation_id']==first.generation_id


@pytest.mark.parametrize('printer_id',[0,True,2,'1'])
def test_unavailable_target_creates_no_generation(db,printer_id):
    with pytest.raises(module.AMSTransitionUnavailable):module._begin_read(db,printer_id=printer_id,runtime=RUNTIME)
    db.rollback()
    assert db.execute(generations.select()).first() is None


def test_competing_sqlite_writer_refuses_then_can_supersede_after_commit(db):
    first=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    with Session(db.get_bind()) as second:
        with pytest.raises(module.AMSTransitionUnavailable):
            module._begin_read(second,printer_id=1,runtime=RUNTIME)
        second.rollback()
        db.commit()
        newer=module._begin_read(second,printer_id=1,runtime=RUNTIME)
        second.commit()
    assert state(db,first.generation_id)['state']=='failed'
    assert db.execute(current_reads.select()).mappings().one()['generation_id']==newer.generation_id


def test_missed_supersession_requires_rollback_and_keeps_old_pointer(db):
    first=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.commit()
    # Independent connection installs a deterministic DB-side missed UPDATE.
    with db.get_bind().begin() as other:
        other.execute(text("CREATE TRIGGER refuse_supersession BEFORE UPDATE ON ams_read_generations BEGIN SELECT RAISE(IGNORE); END"))
    with pytest.raises(module.AMSTransitionUnavailable):
        module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.rollback()
    assert state(db,first.generation_id)['state']=='acquiring'
    assert db.execute(current_reads.select()).mappings().one()['generation_id']==first.generation_id


@pytest.fixture
def pending(db,monkeypatch):
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    started=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.commit()
    receipt=module._instant(state(db,started.generation_id)['started_at'])+timedelta(seconds=1)
    db.rollback()
    monkeypatch.setattr(module,'sample_database_clock',lambda db:SimpleNamespace(database_now=receipt+timedelta(seconds=1)))
    return module._PendingAMSReport(started,'b'*32,receipt,RUNTIME.clock_domain,'db-clock-estimate-v1',
        (RawTrayFact(1,0,0,'PETG','reported'),))


def test_persist_and_identical_retry_preserve_packet_and_receipt(db,pending):
    from core.ams_evidence_schema import packets
    first=module._persist_report(db,pending=pending,runtime=RUNTIME)
    db.commit()
    assert module._persist_report(db,pending=pending,runtime=RUNTIME)==first
    assert db.execute(packets.select()).mappings().one()['packet_id']==pending.packet_id
    import importlib
    importlib.import_module('core.schema.migrations.020_ams_evidence').validate(db.connection())


def test_rollback_retry_preserves_original_pending(db,pending):
    from core.ams_evidence_schema import packets
    module._persist_report(db,pending=pending,runtime=RUNTIME)
    db.rollback()
    assert db.execute(packets.select()).first() is None
    module._persist_report(db,pending=pending,runtime=RUNTIME)
    db.commit()
    assert module._instant(db.execute(packets.select()).mappings().one()['received_at'])==pending.received_at


@pytest.mark.parametrize('change', ['configuration','owner','superseded','expired','future','conflicting_packet'])
def test_changed_authority_or_receipt_refuses(db,pending,monkeypatch,change):
    from dataclasses import replace
    if change=='configuration':db.execute(text("UPDATE printers SET api_host='other.local'"));db.commit()
    elif change=='owner':db.execute(text('UPDATE printers SET org_id=3'));db.commit()
    elif change=='superseded':module._begin_read(db,printer_id=1,runtime=RUNTIME);db.commit()
    elif change=='expired':monkeypatch.setattr(module,'sample_database_clock',lambda db:SimpleNamespace(database_now=pending.received_at+timedelta(seconds=120)))
    elif change=='future':monkeypatch.setattr(module,'sample_database_clock',lambda db:SimpleNamespace(database_now=pending.received_at-timedelta(microseconds=1)))
    elif change=='conflicting_packet':
        module._persist_report(db,pending=pending,runtime=RUNTIME);db.commit()
        pending=replace(pending,received_at=pending.received_at+timedelta(microseconds=1))
    with pytest.raises(module.AMSTransitionUnavailable):module._persist_report(db,pending=pending,runtime=RUNTIME)
    db.rollback()


def test_freeze_receipt_once_and_persistence_retry_does_not_reanchor(db,pending,monkeypatch):
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    from modules.printers.telemetry.bambu.ams_receipt_time import ReceiptEstimate
    from modules.printers.telemetry.bambu import ams_database_clock
    anchors=[]
    def anchor(*args,**kwargs):
        anchors.append(kwargs['received_monotonic'])
        return ReceiptEstimate(pending.received_at,RUNTIME.clock_domain)
    monkeypatch.setattr(ams_database_clock,'anchor_original_receipt',anchor)
    snapshot=RawAMSSnapshot(pending.started.generation_id,pending.packet_id,1,10.0,pending.started.configuration_key,
        pending.started.owner_org_id,pending.facts)
    result=RuntimeBoundAMSResult(True,snapshot,RUNTIME.incarnation,RUNTIME.clock_domain,True,None)
    frozen=module._freeze_read_result(db,started=pending.started,result=result,runtime=RUNTIME,selected_slots=[1])
    module._persist_report(db,pending=frozen,runtime=RUNTIME)
    db.rollback()
    module._persist_report(db,pending=frozen,runtime=RUNTIME)
    db.commit()
    assert anchors==[10.0]
    assert frozen.received_at==pending.received_at


def test_changed_tray_fact_under_same_packet_is_refused(db,pending):
    from dataclasses import replace
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    module._persist_report(db,pending=pending,runtime=RUNTIME)
    db.commit()
    altered=replace(pending,facts=(RawTrayFact(1,0,0,'PLA','reported'),))
    with pytest.raises(module.AMSTransitionUnavailable):
        module._persist_report(db,pending=altered,runtime=RUNTIME)
    db.rollback()


def test_result_from_other_overlapping_operation_cannot_freeze(db,pending,monkeypatch):
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    from modules.printers.telemetry.bambu import ams_database_clock
    newer=module._begin_read(db,printer_id=1,runtime=RUNTIME)
    db.commit()
    def forbidden(*args,**kwargs):raise AssertionError('wrong operation must refuse before clock anchor')
    monkeypatch.setattr(ams_database_clock,'anchor_original_receipt',forbidden)
    snapshot=RawAMSSnapshot(pending.started.generation_id,pending.packet_id,1,10.0,
        newer.configuration_key,newer.owner_org_id,pending.facts)
    result=RuntimeBoundAMSResult(True,snapshot,RUNTIME.incarnation,RUNTIME.clock_domain,True,None)
    with pytest.raises(module.AMSTransitionUnavailable,match='^AMS report freeze unavailable$'):
        module._freeze_read_result(db,started=newer,result=result,runtime=RUNTIME,selected_slots=[1])
