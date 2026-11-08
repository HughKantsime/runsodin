"""Native 054 fresh and historical upgrade proof; isolated PostgreSQL only."""
import importlib
import os
import subprocess
import sys
from pathlib import Path
import pytest
from sqlalchemy import text, bindparam
from core.schema import bootstrap_database


@pytest.fixture
def native_pg():
    from tests.test_contracts.test_post027_migration_033_postgres import pg_engine
    yield from pg_engine.__wrapped__()


@pytest.mark.parametrize('history', [None, 'preserved044', 'preserved034'])
def test_controlled_postgres_fresh_and_historical(native_pg, tmp_path, history):
    if history:
        package = importlib.import_module('core.schema.' + history)
        runner = Path(package.__file__).with_name('runner.py')
        code = '''
import os,runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen')['prepare_sources']()
from sqlalchemy import create_engine
from core.schema import bootstrap_database
bootstrap_database(create_engine(os.environ['CONTROLLED_UPGRADE_URL']))
'''
        result = subprocess.run([sys.executable, '-I', '-c', code, str(runner)], cwd=tmp_path,
                                env={**os.environ, 'CONTROLLED_UPGRADE_URL': native_pg.url.render_as_string(hide_password=False)},
                                capture_output=True, timeout=120)
        assert result.returncode == 0, 'Frozen schema bootstrap failed'
    bootstrap_database(native_pg)
    migration = importlib.import_module('core.schema.migrations.054_controlled_parts')
    with native_pg.begin() as connection:
        migration.validate(connection)
        assert connection.execute(text('SELECT count(*) FROM controlled_audiences')).scalar() == 0
    assert bootstrap_database(native_pg)['applied'] == []


def test_controlled_history_postgres_runtime(native_pg, tier_license):
    """Exercise native nullable clock binds, tombstones and correlated read filters."""
    from sqlalchemy.orm import Session
    from core.controlled_parts import allowed, audit_visibility
    from core.controlled_deletion import redact_deleted_job_history
    from tests.test_contracts.test_controlled_parts import mark, actor
    from modules.organizations.routes_controlled_parts import change_mark
    from core.audit import log_audit
    bootstrap_database(native_pg)
    migration = importlib.import_module('core.schema.migrations.055_controlled_history')
    with native_pg.begin() as c:
        migration.validate(c)
        c.execute(text("INSERT INTO groups(id,name) VALUES(1,'Controlled')"))
        c.execute(text("INSERT INTO users(id,username,password_hash,role,group_id,is_active) VALUES(1,'member','unused','operator',1,:active),(2,'outsider','unused','admin',NULL,:active)"), {'active': True})
        c.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
        c.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,1)'))
        c.execute(text("INSERT INTO print_files(id,filename,org_id) VALUES(1,'Secret',1)"))
        c.execute(text("INSERT INTO jobs(id,item_name,print_file_id,status,charged_to_org_id,created_at) VALUES(1,'Secret',1,'pending',1,'2019-01-01')"))
    tier_license('enterprise')
    with Session(native_pg) as db:
        for days in (30, 29, 30, 29):
            change_mark(db, 'files', 1, mark(retention_days=days), actor())
        log_audit(db, 'job.updated', 'job', 1, {'name': 'Secret'})
        db.commit()
        before = db.execute(text("SELECT * FROM audit_logs WHERE action='job.updated'")).one()
        db.execute(text("INSERT INTO printers(id,name) VALUES(1,'Retention')"))
        db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,completed_at,created_at) VALUES(1,1,1,'retention.mp4','ready','2020-01-01','2020-01-01')"))
        redact_deleted_job_history(db, 1)
        db.execute(text('DELETE FROM jobs WHERE id=1')); db.commit()
        assert tuple(db.execute(text("SELECT * FROM audit_logs WHERE action='job.updated'")).one()) == tuple(before)
        assert allowed(db, 'job', 1, actor()) and not allowed(db, 'job', 1, actor(2))
        for who, expected in ((actor(), 1), (actor(2), 0)):
            condition, values = audit_visibility(who, db)
            assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='job.updated' AND " + condition), values).scalar() == expected
        change_mark(db, 'files', 1, mark(controlled=False), actor())
        change_mark(db, 'files', 1, mark(), actor())

        from core.controlled_retention_query import pending_count
        from core.controlled_retention import expired
        from datetime import datetime, timezone
        future = datetime(2100, 1, 1, tzinfo=timezone.utc)
        assert expired(db, 1, now=future)
        assert pending_count(db, now=future) == 1
        db.execute(text("UPDATE controlled_job_tombstones SET purge_previewed_at='2099-01-01'"))
        assert pending_count(db, now=future) == 0

        # Tombstone bounds and FK shape must behave identically on native PG.
        from sqlalchemy import inspect
        foreign = inspect(db.get_bind()).get_foreign_keys('controlled_job_tombstones')
        assert next(f for f in foreign if f['referred_table'] == 'controlled_audiences')['options']['ondelete'] == 'RESTRICT'
        db.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(2,'Public replacement','pending')"))
        log_audit(db, 'job.public_replacement', 'job', 2, {'name': 'Public replacement'})
        db.commit()
        condition, values = audit_visibility(actor(2), db)
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='job.public_replacement' AND " + condition), values).scalar() == 1
        from core.controlled_parts import policies
        from core.controlled_policy_clock import media_deadline
        from core.db_compat import as_utc_datetime
        from datetime import timedelta
        rule = policies(db, 'file', 1)[0]
        completed = as_utc_datetime(rule['marked_at']) + timedelta(days=40)
        assert media_deadline(rule, completed) == completed + timedelta(days=rule['retention_days'])


def test_controlled_naive_media_times_use_utc_under_new_york(native_pg, monkeypatch):
    """Product connections keep naive media dates stable even with a non-UTC TZ."""
    import time
    from datetime import datetime, timedelta, timezone
    from core.database_config import create_database_engine
    from core.db_compat import as_utc_datetime
    from core.controlled_policy_clock import media_deadline
    previous_tz = os.environ.get('TZ')
    monkeypatch.setenv('TZ', 'America/New_York')
    time.tzset()
    managed = create_database_engine(native_pg.url, role='backup')
    try:
        bootstrap_database(managed)
        with managed.begin() as c:
            assert c.execute(text('SHOW TimeZone')).scalar_one() == 'UTC'
            c.execute(text("INSERT INTO printers(id,name) VALUES(1,'Timezone test')"))
            completed = datetime(2026, 7, 1, 12, 0)
            c.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,completed_at,created_at) VALUES(1,1,'tz.mp4','ready',:time,:time)"), {'time': completed})
            row = c.execute(text('SELECT completed_at,created_at FROM timelapses WHERE id=1')).one()
            expected = completed.replace(tzinfo=timezone.utc)
            for value in row:
                assert as_utc_datetime(value) == expected
                assert media_deadline({'marked_at': '2026-01-01T00:00:00+00:00', 'retention_days': 7}, value) == expected + timedelta(days=7)
    finally:
        managed.dispose()
        if previous_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous_tz
        time.tzset()


@pytest.mark.parametrize('lock_order', ['correct', 'removed', 'after_audit'])
def test_controlled_audit_boundary_covers_earlier_sequence_committed_later(native_pg, monkeypatch, lock_order):
    """The same executable probe must reject both missing and late row locks."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    from sqlalchemy.orm import Session
    from core import controlled_deletion, audit
    from core.controlled_parts import policies
    bootstrap_database(native_pg)
    with native_pg.begin() as c:
        c.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
        c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(1,'Secret','pending')"))
        c.execute(text("INSERT INTO controlled_jobs(job_id,audience_id,label,printer_tag,retention_days,marked_at,reason) VALUES(1,1,'CUI','Secure',7,'2026-01-01T00:00:00+00:00','test')"))

    audit_called, started = Event(), Event()
    original_audit = audit.log_audit
    def observed_audit(db, *args, **kwargs):
        audit_called.set()
        entry = original_audit(db, *args, **kwargs)
        if lock_order == 'after_audit':
            # Mutant: move the row lock immediately after log_audit.
            db.execute(text('SELECT id FROM jobs WHERE id=1 FOR UPDATE')).all()
        return entry
    monkeypatch.setattr(audit, 'log_audit', observed_audit)
    if lock_order != 'correct':
        original_capture = controlled_deletion.controlled_job_ids
        def capture_without_lock(db, identifiers):
            original_execute = db.execute
            def execute(statement, *args, **kwargs):
                if str(statement).startswith('SELECT id FROM jobs WHERE id IN') and 'FOR UPDATE' in str(statement):
                    statement = text(statement.text.replace(' FOR UPDATE', '')).bindparams(
                        bindparam('ids', expanding=True))
                return original_execute(statement, *args, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(db, 'execute', execute)
                return original_capture(db, identifiers)
        monkeypatch.setattr(controlled_deletion, 'controlled_job_ids', capture_without_lock)

    pid = []
    def delete():
        with Session(native_pg) as db:
            db.execute(text("SET lock_timeout='8s'"))
            pid.append(db.execute(text('SELECT pg_backend_pid()')).scalar_one())
            started.set()
            controlled_deletion.redact_deleted_job_history(db, 1)
            bound = db.execute(text('SELECT audit_max_id FROM controlled_job_tombstones')).scalar_one()
            db.commit()
            return bound

    with native_pg.connect() as writer, native_pg.connect() as observer, ThreadPoolExecutor(max_workers=1) as pool:
        transaction = writer.begin()
        writer_pid = writer.execute(text('SELECT pg_backend_pid()')).scalar_one()
        writer.execute(text('SELECT id FROM jobs WHERE id=1 FOR UPDATE')).all()
        future = pool.submit(delete)
        try:
            assert started.wait(5), 'deletion worker did not start'
            deadline, blocked = time.monotonic() + 5, False
            while time.monotonic() < deadline and not future.done():
                # Inspect actual PostgreSQL contention, not a scheduling sleep.
                blocked = writer_pid in observer.execute(text('SELECT pg_blocking_pids(:pid)'), {'pid': pid[0]}).scalar_one()
                if blocked:
                    break
                time.sleep(0.01)
            audit_before_writer = audit_called.is_set()
            audit_id = writer.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details) VALUES('job.updated','job',1,:details) RETURNING id"), {'details': '{"name":"Secret"}'}).scalar_one()
            transaction.commit()
        finally:
            if transaction.is_active:
                transaction.rollback()
        bound = future.result(timeout=10)

    def assert_order():
        assert bound >= audit_id, 'deletion bound excludes the locked writer audit row'
        assert blocked, 'deletion did not wait for the job-row writer'
        assert not audit_before_writer, 'deletion logged its bound before acquiring the job-row lock'
    if lock_order == 'correct':
        assert_order()
    else:
        with pytest.raises(AssertionError, match='bound excludes|did not wait|before acquiring'):
            assert_order()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_authenticated_request_after_tombstone(request, tmp_path, dialect):
    """Real JWT resolution must survive the history-only fast path on PostgreSQL."""
    from tests.test_contracts.test_cert_G_harness import run_probe
    data = tmp_path / 'owned-data'
    data.mkdir(mode=0o700)
    env = {'ODIN_DATA_DIR': str(data), 'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'http.db')}
    if dialect == 'postgresql':
        url = request.getfixturevalue('native_pg').url
        secret = tmp_path / 'database-password'
        secret.write_text(url.password)
        secret.chmod(0o600)
        env.update(DATABASE_URL=url._replace(password=None).render_as_string(hide_password=False),
                   DATABASE_PASSWORD_FILE=str(secret))
    result = run_probe('controlled-pg-tombstone', r'''
install_license('pro')
sql("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'outsider',:password,'admin',:active)",
    password=hash_password(PASSWORD), active=True)
c = login('outsider')
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')")
sql("INSERT INTO printers(id,name,slot_count,is_active,tags,timelapse_enabled,shared) VALUES(1,'Printer',4,TRUE,'[]',FALSE,FALSE)")
from sqlalchemy.orm import Session
from tests.test_contracts.test_controlled_round8 import add_job, add_rule, add_media, delete_job, ORIGIN
with Session(engine) as db:
 add_job(db, 5); add_rule(db, 'jobs', 5); add_media(db, 1, 5, ORIGIN)
 delete_job(db, 5); db.commit()
assert sql('SELECT count(*) FROM controlled_jobs')[0][0] == 0
for path in ('/api/printers', '/api/audit-logs', '/api/export/jobs'):
 r = c.get(path)
 assert r.status_code == 200, (path, r.status_code, r.text)
OUT['checked'] = True
''', tmp_path, extra_env=env)
    assert result['checked'] and result['_net_attempts'] == []
