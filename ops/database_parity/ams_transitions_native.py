"""Native DB transitions with explicit synthetic runtime, not runtime authority."""
import os
from types import SimpleNamespace
from cryptography.fernet import Fernet
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from core.ams_evidence_schema import EVIDENCE_TABLES,current_reads,generations
from modules.printers.telemetry.bambu import ams_evidence_transactions as module

key=Fernet.generate_key()
os.environ['ENCRYPTION_KEY']=key.decode()
runtime=SimpleNamespace(incarnation='a'*32)
def validate(value):
    assert value is runtime
module.revalidate_runtime_identity=validate
engine=create_engine(os.environ['AMS_TEST_DB_URL'])
with engine.begin() as db:
    db.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY,org_id INTEGER,api_type TEXT,api_host TEXT,api_key TEXT,is_active BOOLEAN,shared BOOLEAN)'))
    db.execute(text("INSERT INTO printers VALUES(1,2,'bambu','printer.local',:key,true,false)"),
        {'key':Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
    for table in EVIDENCE_TABLES:table.create(db)
with Session(engine) as first,Session(engine) as second:
    old=module._begin_read(first,printer_id=1,runtime=runtime)
    try:module._begin_read(second,printer_id=1,runtime=runtime)
    except module.AMSTransitionUnavailable:second.rollback()
    else:raise AssertionError('NOWAIT authority lock bypassed')
    first.commit()
    newer=module._begin_read(second,printer_id=1,runtime=runtime)
    second.commit()
    assert not module._fail_owned_read(first,started=old,runtime=runtime)
    first.rollback()
    assert first.execute(current_reads.select()).mappings().one()['generation_id']==newer.generation_id
    first.rollback()
    assert module._fail_owned_read(second,started=newer,runtime=runtime)
    second.commit()
    assert first.execute(current_reads.select()).first() is None
    assert set(first.execute(generations.select()).mappings().all()[i]['state'] for i in range(2))=={'failed'}
engine.dispose()
print('native-ams-transitions-pass')
