"""Receipt evidence is atomic with real dispatch admission; no device I/O."""
import importlib
import pytest
from sqlalchemy import text,inspect,create_engine
from core.schema.migrator import MigrationError
from modules.printers.dispatch_admission import admit
from tests.test_contracts.test_dispatch_final_admission_behavior import setup,state
from tests.test_contracts.test_education_review_workflow import review_db


def test_admission_receipt_persists_exact_attempt_and_no_raw_configuration(setup):
    engine,_,values=setup
    assert admit(engine,**values).admitted
    with engine.connect() as conn:
        row=conn.execute(text('SELECT * FROM dispatch_attempt_receipts')).mappings().one()
    assert row['claim_token']==values['token'] and row['printer_id']==59 and row['job_id']==53
    assert row['observed_print_job_id'] is None
    assert len(row['configuration_key'])==len(row['prepared_power_key'])==len(row['observed_filename_key'])==64
    assert row['prepared_power_key']!=row['observed_filename_key']
    assert 'printer.example.test' not in str(dict(row))
    engine.dispose()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT claim_token FROM dispatch_attempt_receipts')).scalar_one()==values['token']


def test_receipt_insert_failure_rolls_back_adapter_entry(setup,monkeypatch):
    from core import dispatch_receipts
    engine,_,values=setup
    def refuse(*args,**kwargs):raise RuntimeError('synthetic private marker')
    monkeypatch.setattr(dispatch_receipts,'store_admission_receipt',refuse)
    result=admit(engine,**values)
    assert not result.admitted and 'synthetic private marker' not in result.reason
    assert state(engine)=='preparing'
    with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts')).scalar_one()==0


@pytest.mark.parametrize('change',[
    {'power_accepted':False},{'power_accepted':None},{'power_accepted':1},
    {'transport_filename':'synthetic.gcode'},
    {'transport_filename':'odin-dispatch-'+'f'*32+'.gcode'},
    {'transport_filename':'/path/odin-dispatch-'+'f'*32+'.gcode'},
])
def test_unconfirmed_power_or_contradictory_filename_rolls_back_claim(setup,change):
    engine,_,values=setup
    assert not admit(engine,**{**values,**change}).admitted
    assert state(engine)=='preparing'
    with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts')).scalar_one()==0


def test_receipt_migration_repeated_and_rejects_malformed_adoption(tmp_path):
    migration=importlib.import_module('core.schema.migrations.012_dispatch_receipts')
    engine=create_engine('sqlite:///'+str(tmp_path/'receipts.db'))
    try:
        with engine.begin() as conn:
            migration.apply(conn);migration.validate(conn);migration.apply(conn)
        assert inspect(engine).get_foreign_keys('dispatch_attempt_receipts')==[]
        assert {'observed_print_job_id'}==set(inspect(engine).get_unique_constraints('dispatch_attempt_receipts')[0]['column_names'])
        with engine.begin() as conn:
            conn.execute(text('DROP TABLE dispatch_attempt_receipts'))
            conn.execute(text('CREATE TABLE dispatch_attempt_receipts(claim_token VARCHAR(32) PRIMARY KEY)'))
        with pytest.raises(MigrationError),engine.begin() as conn:migration.apply(conn)
        assert len(inspect(engine).get_columns('dispatch_attempt_receipts'))==1
    finally:engine.dispose()


def test_receipt_migration_rejects_partial_observation_uniqueness(tmp_path):
    import re
    from sqlalchemy.schema import CreateTable
    from core.dispatch_receipt_schema import DispatchAttemptReceipt
    migration=importlib.import_module('core.schema.migrations.012_dispatch_receipts')
    engine=create_engine('sqlite:///'+str(tmp_path/'partial.db'))
    try:
        ddl=str(CreateTable(DispatchAttemptReceipt.__table__).compile(engine))
        ddl=re.sub(r',\s*CONSTRAINT uq_dispatch_receipt_observation UNIQUE \(observed_print_job_id\)','',ddl)
        with engine.begin() as conn:
            conn.exec_driver_sql(ddl)
            conn.exec_driver_sql('CREATE UNIQUE INDEX partial_observation ON dispatch_attempt_receipts(observed_print_job_id) WHERE observed_print_job_id>1')
        with pytest.raises(MigrationError),engine.begin() as conn:migration.validate(conn)
    finally:engine.dispose()


def test_orchestrator_receipt_is_committed_before_adapter_entry(review_db,tmp_path,monkeypatch):
    from modules.printers import dispatch
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    seen=[]
    def adapter(job_id,path,filename,creds):
        with engine.connect() as independent:
            row=independent.execute(text('SELECT r.claim_token,c.state FROM dispatch_attempt_receipts r JOIN printer_dispatch_claims c ON c.token=r.claim_token WHERE r.job_id=53')).one()
        assert row[1]=='adapter_entered' and filename==f'odin-dispatch-{row[0]}.gcode'
        seen.append(True)
        return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    assert dispatch.dispatch_job(59,53)[0] and seen==[True]
