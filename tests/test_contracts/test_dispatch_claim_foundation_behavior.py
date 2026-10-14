"""Committed schema/claim storage contract; not dispatch integration."""
import importlib
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import create_engine,text,insert,inspect
from sqlalchemy.exc import IntegrityError,OperationalError
from core import dispatch_claims as module
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.schema.migrator import MigrationError
migration=importlib.import_module('core.schema.migrations.011_dispatch_claims')


@pytest.fixture
def engine(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'claims.db'),connect_args={'timeout':.1})
    with engine.begin() as conn:
        migration.apply(conn)
        conn.exec_driver_sql('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,printer_id INTEGER)')
        conn.exec_driver_sql("INSERT INTO jobs VALUES(1,'scheduled',1),(2,'scheduled',2)")
    yield engine
    engine.dispose()


def claim(engine,printer=1,job=1,physical='a'*64):
    return module.acquire(engine,printer,job,physical,'b'*64,'c'*64)


def test_fresh_repeated_contract_and_retained_state_after_reopen(engine):
    first=claim(engine);assert first.status=='acquired'
    assert first.token not in repr(first)
    assert module.enter_adapter(engine,first.token)
    with engine.begin() as conn:migration.apply(conn)
    engine.dispose()
    assert claim(engine).status=='conflict'
    with engine.connect() as conn:assert conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one()=='adapter_entered'


@pytest.mark.parametrize('printer,job,physical',[(1,2,'d'*64),(2,1,'d'*64),(2,2,'a'*64)])
def test_independent_unique_printer_job_and_physical_identity(engine,printer,job,physical):
    assert claim(engine).status=='acquired'
    # Storage uniqueness is independent of runtime assignment validation.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(PrinterDispatchClaim.__table__).values(printer_id=printer,job_id=job,
            physical_key=physical,configuration_key='b'*64,payload_sha256='c'*64,
            token='e'*32,state='preparing'))


def test_only_owned_preparing_token_can_release_and_no_postentry_release(engine):
    first=claim(engine)
    assert not module.release_preparing(engine,'f'*32)
    assert module.release_preparing(engine,first.token)
    second=claim(engine)
    assert module.enter_adapter(engine,second.token)
    assert not module.release_preparing(engine,second.token)
    assert module.record_outcome(engine,second.token,'uncertain')
    assert not module.release_preparing(engine,second.token)
    assert claim(engine).status=='conflict'


def test_table_has_no_deletion_cascades_or_expiry(engine):
    assert inspect(engine).get_foreign_keys('printer_dispatch_claims')==[]
    first=claim(engine)
    assert module.enter_adapter(engine,first.token)
    assert module.record_outcome(engine,first.token,'accepted')
    with engine.begin() as conn:conn.execute(text("UPDATE printer_dispatch_claims SET created_at='2000-01-01'"))
    assert claim(engine).status=='conflict'


@pytest.mark.parametrize('state', ['printing', 'paused', 'completed', 'failed', 'cancelled', 'submitted', 'rejected', None])
def test_new_claim_refuses_nonqueued_current_job(engine, state):
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET status=:state WHERE id=1'), {'state':state})
    assert claim(engine).status == 'refused'
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one() == 0


@pytest.mark.parametrize('change', ['unassigned', 'reassigned', 'missing'])
def test_new_claim_refuses_stale_assignment_after_reschedule(engine, change):
    with engine.begin() as conn:
        if change == 'missing':
            conn.execute(text('DELETE FROM jobs WHERE id=1'))
        else:
            conn.execute(text('UPDATE jobs SET printer_id=:printer WHERE id=1'), {'printer':None if change == 'unassigned' else 2})
    assert claim(engine).status == 'refused'
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one() == 0


@pytest.mark.parametrize('state',['expired','finished',''])
def test_invalid_state_refused_by_database(engine,state):
    with pytest.raises(IntegrityError),engine.begin() as conn:
        conn.execute(insert(PrinterDispatchClaim.__table__).values(printer_id=1,job_id=1,physical_key='a'*64,configuration_key='b'*64,payload_sha256='c'*64,token='d'*32,state=state))


@pytest.mark.parametrize('printer,job,physical',[(1,2,'d'*64),(2,1,'d'*64)])
def test_two_connections_contend_without_double_admission(engine,printer,job,physical):
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET printer_id=:printer WHERE id=:job'), {'printer':printer,'job':job})
    first=engine.connect();transaction=first.begin()
    try:
        first.execute(insert(PrinterDispatchClaim.__table__).values(printer_id=1,job_id=1,physical_key='a'*64,configuration_key='b'*64,payload_sha256='c'*64,token='d'*32,state='preparing'))
        with ThreadPoolExecutor(max_workers=1) as executor:
            result=executor.submit(claim,engine,printer,job,physical).result(timeout=2)
        assert result.status=='uncertain'
        transaction.commit()
        assert claim(engine,printer,job,physical).status=='conflict'
    finally:
        if transaction.is_active:transaction.rollback()
        first.close()


def test_malformed_adoption_refused_without_repair(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'malformed.db'))
    try:
        with engine.begin() as conn:conn.execute(text('CREATE TABLE printer_dispatch_claims(printer_id INTEGER PRIMARY KEY)'))
        with pytest.raises(MigrationError),engine.begin() as conn:migration.apply(conn)
        assert [column['name'] for column in inspect(engine).get_columns('printer_dispatch_claims')]==['printer_id']
    finally:engine.dispose()


@pytest.mark.parametrize('change',['partial_unique','literal_case'])
def test_actual_weakened_adoption_refused(tmp_path,change):
    import re
    from sqlalchemy.schema import CreateTable
    engine=create_engine('sqlite:///'+str(tmp_path/(change+'.db')))
    ddl=str(CreateTable(PrinterDispatchClaim.__table__).compile(dialect=engine.dialect))
    if change=='partial_unique':
        ddl=re.sub(r',\s*CONSTRAINT uq_dispatch_claim_physical UNIQUE \(physical_key\)','',ddl)
    else:ddl=ddl.replace("state='preparing'","state='PREPARING'")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(ddl)
            if change=='partial_unique':conn.exec_driver_sql("CREATE UNIQUE INDEX partial_physical ON printer_dispatch_claims(physical_key) WHERE state!='preparing'")
        with pytest.raises(MigrationError),engine.begin() as conn:migration.apply(conn)
    finally:engine.dispose()


def test_check_normalization_preserves_literal_case_and_boolean_grouping():
    assert migration._normalized_check("state='preparing'")!=migration._normalized_check("state='PREPARING'")
    assert migration._normalized_check('a AND (b OR c)')!=migration._normalized_check('(a AND b) OR c')


def test_mutated_schema_source_pin_refused(engine,monkeypatch):
    monkeypatch.setattr(migration,'SCHEMA_SOURCE_SHA256','0'*64)
    with pytest.raises(MigrationError),engine.begin() as conn:migration.validate(conn)


def test_sqlite_retained_invalid_row_refuses_adoption(engine):
    first=claim(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
        conn.execute(text("UPDATE printer_dispatch_claims SET state='invalid_retained'"))
        conn.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(MigrationError),engine.begin() as conn:migration.validate(conn)


def test_uncertain_commit_never_authorizes_but_retains_committed_fence(engine):
    from contextlib import contextmanager
    class UncertainCommit:
        @contextmanager
        def begin(self):
            with engine.begin() as conn:yield conn
            raise RuntimeError('synthetic uncertain commit')
    result=module.acquire(UncertainCommit(),1,1,'a'*64,'b'*64,'c'*64)
    assert result.status=='uncertain' and result.token is None
    assert claim(engine).status=='conflict'
