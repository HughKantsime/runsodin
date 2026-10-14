"""Database receipt time across real observation creation and legacy adoption."""
from contextlib import contextmanager
from types import SimpleNamespace
import importlib
import pytest
from sqlalchemy import create_engine,text,inspect
from core.schema.migrator import MigrationError
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch

@pytest.fixture
def source_db(review_db,tmp_path,monkeypatch):
    from core.db_utils import _ConnectionAdapter
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    @contextmanager
    def db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    return engine,db

@pytest.mark.parametrize('source',['job_event','moonraker','mqtt'])
def test_real_observation_source_records_database_received_time(source_db,monkeypatch,source):
    engine,db=source_db
    if source=='job_event':
        from modules.notifications import job_events
        monkeypatch.setattr(job_events,'get_db',db)
        monkeypatch.setattr(job_events,'get_event_bus',lambda:SimpleNamespace(publish=lambda event:None))
        identifier=job_events.job_started(59,'unmatched-synthetic.gcode')
    elif source=='moonraker':
        from modules.printers.monitors import moonraker_monitor
        monkeypatch.setattr(moonraker_monitor,'engine',engine)
        monitor=moonraker_monitor.MoonrakerMonitor(59,'Synthetic','127.0.0.1')
        monitor._job_started(SimpleNamespace(filename='unmatched-synthetic.gcode',total_layers=10,bed_target=60,nozzle_target=200))
        identifier=monitor._current_job_db_id
    else:
        from modules.printers.monitors import mqtt_job_lifecycle
        monkeypatch.setattr(mqtt_job_lifecycle,'get_db',db)
        identifier,_,_=mqtt_job_lifecycle.record_job_started(59,'Synthetic',{'subtask_name':'unmatched-synthetic.gcode','gcode_file':'unmatched-synthetic.gcode'},lambda **kwargs:None,lambda:None)
    assert identifier is not None
    with engine.connect() as conn:
        row=conn.execute(text('SELECT observed_received_at,started_at,CURRENT_TIMESTAMP FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
    from core.dispatch_observations import _utc
    assert row[0] is not None and row[1] is not None
    assert 0<=(_utc(row[2])-_utc(row[0])).total_seconds()<5


def test_moonraker_resume_retains_received_time(source_db,monkeypatch):
    engine,_=source_db
    from modules.printers.monitors import moonraker_monitor
    monkeypatch.setattr(moonraker_monitor,'engine',engine)
    status=SimpleNamespace(filename='unmatched-synthetic.gcode',total_layers=10,bed_target=60,nozzle_target=200)
    first=moonraker_monitor.MoonrakerMonitor(59,'Synthetic','127.0.0.1');first._job_started(status)
    assert first._current_job_db_id
    with engine.begin() as conn:conn.execute(text("UPDATE print_jobs SET observed_received_at='2001-01-01 00:00:00' WHERE id=:id"),{'id':first._current_job_db_id})
    resumed=moonraker_monitor.MoonrakerMonitor(59,'Synthetic','127.0.0.1');resumed._job_started(status)
    assert resumed._current_job_db_id==first._current_job_db_id
    with engine.connect() as conn:
        assert str(conn.execute(text('SELECT observed_received_at FROM print_jobs WHERE id=:id'),{'id':resumed._current_job_db_id}).scalar_one()).startswith('2001-01-01')
        assert conn.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one()==1


def test_job_event_creation_rollback_leaves_no_received_observation(source_db,monkeypatch):
    engine,db=source_db
    from modules.notifications import job_events
    monkeypatch.setattr(job_events,'get_db',db)
    from modules.archives import services
    observed = []
    def refuse(*args, **kwargs):
        observed.append(True)
        raise RuntimeError('Synthetic insertion rollback')
    monkeypatch.setattr(services,'capture_attempt_spools',refuse)
    assert job_events.job_started(59,'unmatched-synthetic.gcode') is None
    assert observed == [True]
    with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one()==0


def test_migration_preserves_historical_unknown_and_repeats_without_default(tmp_path):
    migration=importlib.import_module('core.schema.migrations.013_observation_received_time')
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE print_jobs(id INTEGER PRIMARY KEY,started_at DATETIME)'))
            conn.execute(text("INSERT INTO print_jobs VALUES(1,'2001-01-01')"))
            migration.apply(conn);migration.apply(conn)
            assert conn.execute(text('SELECT observed_received_at FROM print_jobs WHERE id=1')).scalar_one() is None
        column=next(c for c in inspect(engine).get_columns('print_jobs') if c['name']=='observed_received_at')
        assert column['nullable'] and column['default'] is None
    finally:engine.dispose()

@pytest.mark.parametrize('shape',['TEXT','DATETIME NOT NULL','DATETIME DEFAULT CURRENT_TIMESTAMP'])
def test_malformed_adopted_column_refused_without_repair(tmp_path,shape):
    migration=importlib.import_module('core.schema.migrations.013_observation_received_time')
    engine=create_engine('sqlite:///'+str(tmp_path/'bad.db'))
    try:
        with engine.begin() as conn:conn.exec_driver_sql('CREATE TABLE print_jobs(id INTEGER PRIMARY KEY,observed_received_at '+shape+')')
        with pytest.raises(MigrationError),engine.begin() as conn:migration.apply(conn)
    finally:engine.dispose()
