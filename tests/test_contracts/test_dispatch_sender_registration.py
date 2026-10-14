"""Owned SQLite sender registry tests; no printer or recovery authorization."""
import importlib
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import create_engine,inspect,text
from core import dispatch_claims
from core.schema.migrator import MigrationError
from core import dispatch_senders as registry
from core.dispatch_sender_schema import DispatchSenderRegistration
migration=importlib.import_module('core.schema.migrations.014_dispatch_senders')
claims_migration=importlib.import_module('core.schema.migrations.011_dispatch_claims')

@pytest.fixture
def engine(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'sender.db'),connect_args={'timeout':.1},hide_parameters=True)
    with engine.begin() as conn:
        claims_migration.apply(conn);migration.apply(conn)
        conn.exec_driver_sql('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,printer_id INTEGER)')
        conn.exec_driver_sql("INSERT INTO jobs VALUES(1,'scheduled',1)")
    yield engine
    engine.dispose()


def identity(engine,owner=1):
    claim=dispatch_claims.acquire(engine,1,1,'a'*64,'b'*64,'c'*64,owner)
    assert claim.status=='acquired'
    return dict(claim_token=claim.token,printer_id=1,job_id=1,physical_key='a'*64,
                configuration_key='b'*64,payload_sha256='c'*64,owner_org_id=owner,
                origin_key='d'*32,runtime_incarnation='e'*32)


def snapshot(engine):
    with engine.connect() as conn:
        return conn.execute(text('SELECT * FROM dispatch_sender_registrations')).mappings().all()


def test_register_exact_preparing_attempt_committed_and_retained(engine):
    values=identity(engine);receipt=registry.register(engine,**values)
    assert receipt.status=='registered'
    assert receipt.registration_key and receipt.registration_key not in repr(receipt)
    engine.dispose()
    row=snapshot(engine)[0]
    assert row['claim_token']==values['claim_token'] and row['state']=='active'
    assert row['finished_at'] is None and row['cleanup_evidence_key'] is None
    assert registry.register(engine,**values).status=='conflict'
    assert inspect(engine).get_foreign_keys('dispatch_sender_registrations')==[]
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM printer_dispatch_claims'))
        conn.execute(text("UPDATE dispatch_sender_registrations SET created_at='2000-01-01'"))
        migration.apply(conn)
    assert len(snapshot(engine))==1


@pytest.mark.parametrize('field,value',[
 ('claim_token','f'*32),('printer_id',2),('job_id',2),('physical_key','f'*64),
 ('configuration_key','f'*64),('payload_sha256','f'*64),('owner_org_id',2)])
def test_identity_drift_refuses_registration(engine,field,value):
    values=identity(engine);values[field]=value
    assert registry.register(engine,**values).status=='refused'
    assert snapshot(engine)==[]


@pytest.mark.parametrize('state',['adapter_entered','accepted','uncertain'])
def test_post_admission_claim_cannot_register(engine,state):
    values=identity(engine)
    assert dispatch_claims.enter_adapter(engine,values['claim_token'])
    if state!='adapter_entered':assert dispatch_claims.record_outcome(engine,values['claim_token'],state)
    assert registry.register(engine,**values).status=='refused'


@pytest.mark.parametrize('owner',[None,1])
def test_exact_owner_null_semantics(engine,owner):
    values=identity(engine,owner)
    assert registry.register(engine,**values).status=='registered'


def test_uncertain_commit_never_grants_launch_permission(engine):
    values=identity(engine)
    class Uncertain:
        @contextmanager
        def begin(self):
            with engine.begin() as conn:yield conn
            raise RuntimeError('private-marker')
    receipt=registry.register(Uncertain(),**values)
    assert receipt.status=='uncertain' and receipt.registration_key is None
    assert 'private-marker' not in repr(receipt)
    assert snapshot(engine)[0]['state']=='active'
    assert registry.register(engine,**values).status=='conflict'


def test_registration_conflict_across_actual_connections(engine):
    values=identity(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:registry.register(engine,**values),range(2)))
    assert sum(r.status=='registered' for r in results)==1
    assert len(snapshot(engine))==1


def test_writer_contention_refuses_without_permission(engine):
    values=identity(engine)
    conn=engine.connect();conn.exec_driver_sql('BEGIN IMMEDIATE')
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(registry.register,engine,**values).result(timeout=2)
        assert result.status=='uncertain' and result.registration_key is None
    finally:conn.rollback();conn.close()
    assert snapshot(engine)==[]


def test_cleanup_uncertainty_exact_runtime_only_and_terminal(engine):
    values=identity(engine);receipt=registry.register(engine,**values)
    owned=dict(values,registration_key=receipt.registration_key)
    for field in ('registration_key','origin_key','runtime_incarnation','claim_token'):
        wrong=dict(owned);wrong[field]='f'*32
        assert not registry.finish_uncertain(engine,**wrong)
        assert snapshot(engine)[0]['state']=='active'
    assert registry.finish_uncertain(engine,**owned)
    row=snapshot(engine)[0]
    assert row['state']=='cleanup_uncertain' and row['finished_at'] is not None
    assert row['cleanup_evidence_key'] is None
    assert not registry.finish_uncertain(engine,**owned)
    assert registry.register(engine,**values).status=='conflict'


@pytest.mark.parametrize('state,finish,evidence',[
 ('expired',None,None),('quiesced',None,None),('active','2026-10-11',None),
 ('quiesced','2026-10-11',None),('cleanup_uncertain','2026-10-11','f'*64)])
def test_database_rejects_false_finish_shapes(engine,state,finish,evidence):
    values=identity(engine);assert registry.register(engine,**values).status=='registered'
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError),engine.begin() as conn:
        conn.execute(text('UPDATE dispatch_sender_registrations SET state=:state,finished_at=:finish,cleanup_evidence_key=:evidence'),dict(state=state,finish=finish,evidence=evidence))


def test_malformed_adoption_refuses_without_repair(tmp_path):
    malformed=create_engine('sqlite:///'+str(tmp_path/'malformed.db'))
    try:
        with malformed.begin() as conn:conn.exec_driver_sql('CREATE TABLE dispatch_sender_registrations(claim_token TEXT PRIMARY KEY)')
        with pytest.raises(MigrationError),malformed.begin() as conn:migration.apply(conn)
        assert len(inspect(malformed).get_columns('dispatch_sender_registrations'))==1
    finally:malformed.dispose()


def test_schema_pin_and_invalid_retained_records_refuse(engine,monkeypatch):
    values=identity(engine);assert registry.register(engine,**values).status=='registered'
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
        conn.execute(text("UPDATE dispatch_sender_registrations SET state='quiesced'"))
        conn.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(MigrationError),engine.begin() as conn:migration.validate(conn)
    monkeypatch.setattr(migration,'SCHEMA_SOURCE_SHA256','0'*64)
    with pytest.raises(MigrationError),engine.begin() as conn:migration.validate(conn)


@pytest.mark.parametrize('uncertain',[False,True])
def test_historical_unresolved_sender_blocks_replacement_claim(engine,uncertain):
    values=identity(engine);receipt=registry.register(engine,**values)
    if uncertain:assert registry.finish_uncertain(engine,**values,registration_key=receipt.registration_key)
    assert dispatch_claims.release_preparing(engine,values['claim_token'])
    replacement=identity(engine)
    assert replacement['claim_token']!=values['claim_token']
    assert registry.register(engine,**replacement).status=='refused'
    assert len(snapshot(engine))==1


@pytest.mark.parametrize('field,value',[
 ('origin_key','G'*32),('runtime_incarnation','E'*32),('claim_token','x'*32),
 ('printer_id',True),('owner_org_id',False)])
def test_invalid_identity_refused_before_transaction(engine,field,value):
    values=identity(engine);values[field]=value
    class NoTransaction:
        def begin(self):pytest.fail('Invalid identity opened transaction')
    assert registry.register(NoTransaction(),**values).status=='refused'


def test_database_rejects_nonhex_identity(engine):
    values=identity(engine);assert registry.register(engine,**values).status=='registered'
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError),engine.begin() as conn:
        conn.execute(text("UPDATE dispatch_sender_registrations SET origin_key=:key"),{'key':'G'*32})


def test_weakened_finish_adoption_refuses(tmp_path):
    from sqlalchemy.schema import CreateTable
    database=create_engine('sqlite:///'+str(tmp_path/'weakened.db'))
    ddl=str(CreateTable(DispatchSenderRegistration.__table__).compile(dialect=database.dialect))
    ddl=ddl.replace("state='quiesced'","state='QUIESCED'")
    try:
        with database.begin() as conn:conn.exec_driver_sql(ddl)
        with pytest.raises(MigrationError),database.begin() as conn:migration.apply(conn)
    finally:database.dispose()


@pytest.mark.parametrize('failure',['source_pin','after_validation'])
def test_fresh_migration_failure_leaves_no_table(tmp_path,monkeypatch,failure):
    database=create_engine('sqlite:///'+str(tmp_path/(failure+'.db')))
    if failure=='source_pin':monkeypatch.setattr(migration,'SCHEMA_SOURCE_SHA256','0'*64)
    else:
        original=migration.validate
        def fail(conn):
            original(conn)
            raise MigrationError('Owned post-validation refusal')
        monkeypatch.setattr(migration,'validate',fail)
    try:
        with pytest.raises(MigrationError),database.begin() as conn:migration.apply(conn)
        assert not inspect(database).has_table('dispatch_sender_registrations')
    finally:database.dispose()
