"""PG persistence with real protected identity and owned spawned fixture reports.

No MQTT/device contact or initiating authorization. Synthetic material facts.
"""
import os
import time
import uuid
from dataclasses import replace
from cryptography.fernet import Fernet
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from core.runtime_identity import read_runtime_identity,revalidate_runtime_identity
from core.ams_evidence_schema import EVIDENCE_TABLES,packets,trays,current_reads
from modules.printers.telemetry.bambu import ams_evidence_transactions as service
from modules.printers.telemetry.bambu.ams_configuration import load_ams_configuration
from modules.printers.telemetry.bambu.ams_acquisition_session import _run_owned_worker
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult


def fixture_worker(channel,config,key,owner,slots,incarnation,deadline):
    runtime=read_runtime_identity()
    snapshot=RawAMSSnapshot(incarnation,uuid.uuid4().hex,1,time.monotonic(),key,owner,
        (RawTrayFact(1,0,0,'PETG','reported'),))
    revalidate_runtime_identity(runtime)
    channel.put({'snapshot':snapshot,'error':None,'runtime_incarnation':runtime.incarnation,
                 'clock_domain':runtime.clock_domain})


def refuse(db,pending,runtime):
    try:service._persist_report(db,pending=pending,runtime=runtime)
    except service.AMSTransitionUnavailable:db.rollback()
    else:raise AssertionError('Invalid evidence accepted')


def main():
    assert os.getuid()==10001
    runtime=read_runtime_identity()
    key=Fernet.generate_key()
    os.environ['ENCRYPTION_KEY']=key.decode()
    engine=create_engine(os.environ['AMS_TEST_DB_URL'])
    with engine.begin() as db:
        db.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY,org_id INTEGER,api_type TEXT,api_host TEXT,api_key TEXT,is_active BOOLEAN,shared BOOLEAN)'))
        db.execute(text("INSERT INTO printers VALUES(1,2,'bambu','printer.local',:key,true,false)"),
            {'key':Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
        for table in EVIDENCE_TABLES:table.create(db)
    with Session(engine) as db:
        started=service._begin_read(db,printer_id=1,runtime=runtime)
        db.commit()
        configured=load_ams_configuration(db,1)
        db.rollback()
        acquired=_run_owned_worker(configured.transport,configured.configuration_key,configured.owner_org_id,
            [1],5,worker=fixture_worker,required_runtime=runtime,acquisition_id=started.generation_id)
        assert acquired.success and acquired.cleanup_confirmed
        assert acquired.snapshot.incarnation==started.generation_id
        bound=RuntimeBoundAMSResult(True,acquired.snapshot,runtime.incarnation,runtime.clock_domain,True,None)
        pending=service._freeze_read_result(db,started=started,result=bound,runtime=runtime,selected_slots=[1])
        db.rollback()
        sequence=service._persist_report(db,pending=pending,runtime=runtime)
        db.rollback()
        sequence=service._persist_report(db,pending=pending,runtime=runtime)
        db.commit()
        assert service._persist_report(db,pending=pending,runtime=runtime)==sequence
        db.rollback()
        refuse(db,replace(pending,facts=(RawTrayFact(1,0,0,'PLA','reported'),)),runtime)
        stored=db.execute(packets.select()).mappings().one()
        assert service._instant(stored['received_at'])==pending.received_at
        assert db.execute(trays.select()).mappings().one()['material']=='PETG'
        db.rollback()
        db.execute(text("UPDATE printers SET api_host='other.local'"));db.commit()
        refuse(db,pending,runtime)
        db.execute(text("UPDATE printers SET api_host='printer.local'"));db.commit()
        newer=service._begin_read(db,printer_id=1,runtime=runtime);db.commit()
        refuse(db,pending,runtime)
        assert db.execute(current_reads.select()).mappings().one()['generation_id']==newer.generation_id
        import importlib
        importlib.import_module('core.schema.migrations.020_ams_evidence').validate(db.connection())
    engine.dispose()
    print('native-ams-persistence-pass')


if __name__=='__main__':main()
