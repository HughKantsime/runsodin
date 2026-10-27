"""Job foundation schema qualification, including guard adoption refusals."""
import importlib
import pytest
from sqlalchemy import create_engine, inspect, text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
M = importlib.import_module('core.schema.migrations.040_job_foundation')

@pytest.fixture
def engine(tmp_path):
    e = create_engine('sqlite:///'+str(tmp_path/'foundation.db'))
    bootstrap_database(e)
    yield e
    e.dispose()


def test_fresh_repeat_inventory(engine):
    from core.schema.bootstrap import RAW_REQUIRED_COLUMNS
    assert bootstrap_database(engine)['applied'] == []
    with engine.begin() as c:
        M.apply(c)
        for table in M.TABLES:
            assert set(RAW_REQUIRED_COLUMNS[table]) == {v['name'] for v in inspect(c).get_columns(table)}
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})
    assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]

@pytest.mark.parametrize('drift', ['DROP TRIGGER timeline_mutation_guard', 'ALTER TABLE job_summary_outbox ADD COLUMN drift TEXT'])
def test_drift_refused(engine, drift):
    with engine.begin() as c:c.exec_driver_sql(drift)
    with engine.begin() as c:
        with pytest.raises(MigrationError):M.apply(c)


def test_partial_refused(engine):
    with engine.begin() as c:c.exec_driver_sql('DROP TABLE stock_snapshots')
    with engine.begin() as c:
        with pytest.raises(MigrationError, match='Partial'):M.apply(c)


from tests.test_contracts.test_post027_migration_033_postgres import pg_engine as _native_pg_engine

@pytest.fixture(scope='module')
def pg_engine():
    pytest.importorskip('psycopg', reason='PostgreSQL driver unavailable')
    yield from _native_pg_engine.__wrapped__()


def test_postgres_fresh_repeat_and_inventory(pg_engine):
    bootstrap_database(pg_engine)
    assert bootstrap_database(pg_engine)['applied']==[]
    with pg_engine.begin() as c:M.apply(c);M.validate(c)


def test_postgres_timeline_mutability_and_deletion(pg_engine):
    from sqlalchemy.exc import IntegrityError
    bootstrap_database(pg_engine)
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(411,'timeline','unused','operator',TRUE)"))
        c.execute(text("INSERT INTO printers(id,name) VALUES(411,'timeline')"))
        c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(411,'timeline','pending')"))
        c.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,kind,actor_user_id,occurred_at) VALUES('pg-event',411,411,'bed_clear_confirmed',411,CURRENT_TIMESTAMP)"))
        for sql in ("UPDATE job_timeline_events SET detail='private'", "UPDATE job_timeline_events SET kind='failed'"):
            with pytest.raises(Exception):
                with c.begin_nested():c.exec_driver_sql(sql)
        c.execute(text("UPDATE job_timeline_events SET actor_user_id=NULL WHERE id='pg-event'"))
        c.execute(text('DELETE FROM printers WHERE id=411'))
        assert c.execute(text("SELECT printer_id,actor_user_id FROM job_timeline_events WHERE id='pg-event'")).one()==(None,None)
        c.execute(text('DELETE FROM jobs WHERE id=411'))
        assert c.execute(text("SELECT count(*) FROM job_timeline_events WHERE id='pg-event'")).scalar_one()==0


def test_postgres_monitor_alert_values_insert(pg_engine):
    import ast
    from pathlib import Path
    from core.base import AlertType,AlertSeverity
    bootstrap_database(pg_engine)
    pairs=set()
    paths=list(Path('backend/modules/printers/monitors').glob('*.py'))+[Path('backend/modules/notifications/job_events.py')]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node,ast.Call):continue
            if not any(k.arg=='alert_type' for k in node.keywords):continue
            values={k.arg:k.value for k in node.keywords}
            if all(isinstance(values.get(k),ast.Attribute) for k in ('alert_type','severity')):
                pairs.add((AlertType[values['alert_type'].attr].value,AlertSeverity[values['severity'].attr].value))
    assert pairs
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(412,'alerts','unused','operator',TRUE)"))
        for atype,severity in pairs:
            c.execute(text("INSERT INTO alerts(user_id,alert_type,severity,title,message,is_read,is_dismissed) VALUES(412,:atype,:severity,'monitor contract','',false,false)"),{'atype':atype,'severity':severity})


def test_pre040_frozen_database_upgrades_and_preserves_jobs(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from core.schema import preserved039
    database=tmp_path/'pre040.db'
    code='''import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen039')['prepare_sources']()
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
e=create_engine('sqlite:///'+sys.argv[2]);bootstrap_database(e)
with e.begin() as c:c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(40040,'preserved','pending')"))
'''
    result=subprocess.run([sys.executable,'-I','-c',code,str(Path(preserved039.__file__).with_name('runner.py')),str(database)],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr
    e=create_engine('sqlite:///'+str(database))
    try:
        assert bootstrap_database(e)['applied'][:2]==[M.MIGRATION_ID, 'python:041-inventory-color']
        with e.connect() as c:assert c.execute(text('SELECT item_name FROM jobs WHERE id=40040')).scalar_one()=='preserved'
    finally:e.dispose()


def test_published_1924_entrypoint_upgrade_through_040(tmp_path):
    from tests.test_contracts.test_post027_migration_032 import test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command
    test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path)
    e=create_engine('sqlite:///'+str(tmp_path/'odin.db'))
    try:
        with e.connect() as c:
            assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==1
            M.validate(c)
    finally:e.dispose()
