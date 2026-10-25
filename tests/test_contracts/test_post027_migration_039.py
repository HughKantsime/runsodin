"""Fresh/repeat/adoption/drift, native guards and retained reference integrity."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
M=importlib.import_module('core.schema.migrations.039_measured_filament')


@pytest.fixture
def engine(tmp_path):
    e=create_engine('sqlite:///'+str(tmp_path/'migration.db'))
    bootstrap_database(e)
    yield e
    e.dispose()


def qualify(engine):
    assert set(M.TABLES)<=set(inspect(engine).get_table_names())
    assert bootstrap_database(engine)['applied']==[]
    with engine.begin() as c:
        M.apply(c);M.validate(c)
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID})
    assert bootstrap_database(engine)['applied']==[M.MIGRATION_ID]


def test_sqlite_fresh_repeat_adoption(engine):qualify(engine)


@pytest.mark.parametrize('drift',["DROP TRIGGER terminal_filament_measurements_no_delete", "ALTER TABLE filament_cost_events ADD COLUMN drift TEXT"])
def test_sqlite_drift_refused(engine,drift):
    with engine.begin() as c:c.exec_driver_sql(drift)
    with engine.begin() as c:
        with pytest.raises(MigrationError):M.apply(c)


def test_partial_schema_refused(engine):
    with engine.begin() as c:c.exec_driver_sql('DROP TABLE filament_cost_events')
    with engine.begin() as c:
        with pytest.raises(MigrationError,match='Partial'):M.apply(c)


def test_retained_owner_and_birth_required(engine):
    from sqlalchemy.exc import IntegrityError
    with engine.begin() as c:
        with pytest.raises(IntegrityError):
            c.execute(text("""INSERT INTO terminal_filament_measurements
             (observation_id,attempt_uid,attempt_birth,owner_scope,owner_key,order_uid,item_uid,allocation_key,outcome,measurement_basis,snapshot_json)
             VALUES(:obs,:attempt,:birth,'org',1,:order_uid,:item,'attempt-total','failed','unavailable','{}')"""),
             {'obs':'a'*32,'attempt':'a'*36,'birth':'b'*32,'order_uid':'b'*36,'item':'c'*36})


# Pinned owned disposable native PostgreSQL; no external or production database.
from tests.test_contracts.test_post027_migration_033_postgres import pg_engine as _native_pg_engine

@pytest.fixture(scope='module')
def pg_engine(request):
    pytest.importorskip('psycopg',reason='PostgreSQL qualification unavailable: psycopg is not installed in this sandbox')
    yield from _native_pg_engine.__wrapped__()


def test_postgres_fresh_repeat_adoption_drift(pg_engine):
    bootstrap_database(pg_engine)
    qualify(pg_engine)
    with pg_engine.begin() as c:
        c.exec_driver_sql('ALTER TABLE filament_cost_events ADD COLUMN drift TEXT')
    with pg_engine.begin() as c:
        with pytest.raises(MigrationError):M.validate(c)
