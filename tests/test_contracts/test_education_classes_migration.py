"""D2 migration preserves the released 046 ledger and retained graphs."""
import importlib
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import uuid
import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database, preserved046
from core.schema.migrator import MigrationError

M = importlib.import_module('core.schema.migrations.049_education_classes')


def test_frozen046_boot_upgrade_and_repeat(tmp_path):
    code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen046')['prepare_sources']()
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:046-tray-identity' in bootstrap_database(engine)['applied']
with engine.begin() as c:
 c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Retained school',1)"))
assert bootstrap_database(engine)['applied']==[]
'''
    result = subprocess.run([sys.executable, '-I', '-c', code,
        str(Path(preserved046.__file__).with_name('runner.py')), str(tmp_path/'old.db')],
        cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///'+str(tmp_path/'old.db'))
    try:
        assert M.MIGRATION_ID in bootstrap_database(engine)['applied']
        assert bootstrap_database(engine)['applied'] == []
        with engine.connect() as c:
            assert c.execute(text('SELECT name FROM groups WHERE id=1')).scalar_one() == 'Retained school'
            M.validate(c)
    finally:
        engine.dispose()


def test_wrong_adopted_shape_refused(tmp_path):
    engine = create_engine('sqlite:///'+str(tmp_path/'wrong.db'))
    bootstrap_database(engine)
    with engine.begin() as c:
        c.exec_driver_sql('ALTER TABLE education_classes ADD COLUMN accidental TEXT')
        with pytest.raises(MigrationError, match='Classes schema differs'):
            M.validate(c)


@pytest.mark.parametrize('state', ['a ctive', 'Active'])
def test_changed_constraint_literal_refused(tmp_path, state):
    engine = create_engine('sqlite:///'+str(tmp_path/'literal.db'))
    bootstrap_database(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE education_classes')
        ddl = M.DDL['education_classes'].replace(
            "CHECK(state IN ('active','archived'))",
            "CHECK(state IN ('" + state + "','archived'))")
        connection.exec_driver_sql('CREATE TABLE education_classes' + ddl)
        with pytest.raises(MigrationError, match='Classes schema differs'):
            M.validate(connection)
    engine.dispose()


def test_released046_routed(tmp_path, monkeypatch):
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    archive = tmp_path/'owned.dump'; archive.write_bytes(b'x'*100)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: preserved046.ledger_ids())
    assert stage.identify_contract(archive, None, None, tmp_path) is stage.PRESERVED046


def test_native_roundtrip_fixture_obeys_current_foreign_keys(tmp_path):
    from ops.database_parity.education_classes_fixtures import seed, verify
    engine = create_engine('sqlite:///'+str(tmp_path/'rows.db'))
    bootstrap_database(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        seed(connection)
        verify(connection)
    engine.dispose()


@pytest.fixture(params=['sqlite', 'postgresql'])
def native_engine(request, tmp_path):
    if request.param == 'postgresql':
        engine = request.getfixturevalue('pg_engine')
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP SCHEMA public CASCADE')
            connection.exec_driver_sql('CREATE SCHEMA public')
        return engine
    engine = create_engine('sqlite:///' + str(tmp_path / 'native.db'))
    request.addfinalizer(engine.dispose)
    return engine


@pytest.fixture(scope='module')
def pg_engine():
    """Own enough tmpfs for this file's repeated full-schema fresh boots."""
    import psycopg  # noqa: F401 - fail loudly if the product driver is missing
    from tests.test_contracts.test_post027_migration_033_postgres import IMAGE, _docker
    if not shutil.which('docker') or _docker('info', timeout=20).returncode:
        pytest.skip('Docker unavailable')
    name = 'certfixB_d2_049_' + uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(24)
    engine = None
    try:
        started = _docker('run', '-d', '--name', name, '-p', '127.0.0.1::5432', '--tmpfs',
            '/var/lib/postgresql/data:rw,size=512m', '-e', 'POSTGRES_PASSWORD=' + password, IMAGE, timeout=180)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 90
        while _docker('exec', name, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1', timeout=10).returncode:
            assert time.monotonic() < deadline, 'owned PostgreSQL not ready'
            time.sleep(0.3)
        port = _docker('port', name, '5432/tcp').stdout.strip().rsplit(':', 1)[-1]
        engine = create_engine(f'postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/postgres')
        deadline = time.monotonic() + 30
        while True:
            try:
                with engine.connect():
                    break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.3)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        _docker('rm', '-f', name)


def test_fresh_boot_native_shape_portfolio_and_repeat(native_engine):
    from sqlalchemy.orm import Session
    from modules.organizations.education_classes_portfolio import portfolio
    from ops.database_parity.education_classes_fixtures import seed, verify
    result = bootstrap_database(native_engine)
    assert M.MIGRATION_ID in result['applied']
    assert result['applied'].index('python:037-education-memberships') < result['applied'].index(M.MIGRATION_ID)
    assert bootstrap_database(native_engine)['applied'] == []
    with native_engine.begin() as connection:
        seed(connection)
        verify(connection)
        M.validate(connection)
    with Session(native_engine) as db:
        principal = {'id': 990049, 'group_id': 990049, 'role': 'viewer'}
        assert portfolio(db, principal)['items'][0]['grade'] == 'A'
        # Supply a real student grant for the assignment-filtered query.
        db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(990049,990049,990049,'student','active',990049)"))
        assert portfolio(db, principal, assignment_id='roundtrip049-assignment')['items'][0]['grade'] == 'A'


def test_native_shape_rejects_changed_foreign_target(native_engine):
    bootstrap_database(native_engine)
    with native_engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE education_assessment_revisions')
        ddl = M.DDL['education_assessment_revisions'].replace(
            'REFERENCES education_assignment_submissions(submission_id,org_id)',
            'REFERENCES education_submissions(id,org_id)')
        connection.exec_driver_sql('CREATE TABLE education_assessment_revisions' + ddl)
        with pytest.raises(MigrationError, match='Classes schema differs'):
            M.validate(connection)


def test_native_shape_accepts_reflected_checks_and_rejects_changed_default(native_engine):
    bootstrap_database(native_engine)
    with native_engine.begin() as connection:
        M.validate(connection)  # native CHECKs and casts, including PG ANY/ARRAY
        if native_engine.dialect.name == 'postgresql':
            connection.exec_driver_sql("ALTER TABLE education_assignment_submissions ALTER COLUMN grade SET DEFAULT 'B'")
        else:
            connection.exec_driver_sql('DROP TABLE education_assessment_revisions')
            connection.exec_driver_sql('DROP TABLE education_assignment_submissions')
            connection.exec_driver_sql('CREATE TABLE education_assignment_submissions' +
                M.DDL['education_assignment_submissions'].replace("grade VARCHAR(100) NOT NULL DEFAULT ''", "grade VARCHAR(100) NOT NULL DEFAULT 'B'"))
        with pytest.raises(MigrationError, match='Classes schema differs'):
            M.validate(connection)


@pytest.mark.parametrize('statement', [
    "UPDATE education_assessment_revisions SET new_grade='forged'",
    'DELETE FROM education_assessment_revisions',
    'replace_or_truncate',
])
def test_native_assessment_history_is_append_only(native_engine, statement):
    from sqlalchemy.exc import DBAPIError
    from ops.database_parity.education_classes_fixtures import seed, verify
    bootstrap_database(native_engine)
    with native_engine.begin() as connection:
        seed(connection)
    if statement == 'replace_or_truncate':
        statement = ('INSERT OR REPLACE INTO education_assessment_revisions SELECT * FROM education_assessment_revisions'
                     if native_engine.dialect.name == 'sqlite' else 'TRUNCATE education_assessment_revisions')
    with native_engine.begin() as connection:
        with pytest.raises(DBAPIError, match='assessment revisions are immutable'):
            with connection.begin_nested():
                connection.exec_driver_sql(statement)
        verify(connection)
        M.validate(connection)


@pytest.mark.parametrize('fail', [False, True])
def test_native_erasure_restores_guard_and_rolls_back_on_failure(native_engine, monkeypatch, fail):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session
    from modules.organizations import education_classes_privacy as privacy
    from ops.database_parity.education_classes_fixtures import seed, verify
    bootstrap_database(native_engine)
    with native_engine.begin() as connection:
        seed(connection)
    with Session(native_engine) as db:
        if fail:
            def refused(*args, **kwargs):
                raise RuntimeError('injected scrub failure')
            monkeypatch.setattr(privacy, '_scrub_rows', refused)
            with pytest.raises(RuntimeError, match='injected scrub failure'):
                privacy.erase_classes(db, 990049, {'d2-roundtrip'})
            # Even if the caller commits after catching failure, no partial scrub escapes.
        else:
            privacy.erase_classes(db, 990049, {'d2-roundtrip'})
        db.commit()
    with native_engine.begin() as connection:
        if fail:
            verify(connection)
        else:
            assert connection.execute(text('SELECT old_grade,new_grade,old_feedback,new_feedback,reviewed_by '
                'FROM education_assessment_revisions')).one() == ('', '', '', '', None)
        M.validate(connection)
        for sql in ("UPDATE education_assessment_revisions SET new_grade='forged'",
                    'DELETE FROM education_assessment_revisions'):
            with pytest.raises(DBAPIError, match='assessment revisions are immutable'):
                with connection.begin_nested():
                    connection.exec_driver_sql(sql)


def test_postgres_erasure_holds_assessment_lock_before_disabling_history_guard(pg_engine):
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session
    from modules.organizations.education_classes_privacy import erase_classes
    from ops.database_parity.education_classes_fixtures import seed
    with pg_engine.begin() as connection:
        connection.exec_driver_sql('DROP SCHEMA public CASCADE')
        connection.exec_driver_sql('CREATE SCHEMA public')
    bootstrap_database(pg_engine)
    with pg_engine.begin() as connection:
        seed(connection)
    checked = []

    def before_guard(connection, cursor, statement, parameters, context, executemany):
        if 'DISABLE TRIGGER trg_assessment_revisions_no_update' not in statement:
            return
        with pg_engine.connect() as contender:
            with pytest.raises(DBAPIError) as error:
                contender.execute(text('SELECT submission_id FROM education_assignment_submissions '
                                       'ORDER BY submission_id FOR UPDATE NOWAIT')).all()
            assert error.value.orig.sqlstate == '55P03'
        checked.append(True)

    event.listen(pg_engine, 'before_cursor_execute', before_guard)
    try:
        with Session(pg_engine) as db:
            erase_classes(db, 990049, {'d2-roundtrip'})
            db.commit()
        assert checked == [True]
    finally:
        event.remove(pg_engine, 'before_cursor_execute', before_guard)


def test_native_missing_assessment_guard_is_refused(native_engine):
    bootstrap_database(native_engine)
    with native_engine.begin() as connection:
        connection.exec_driver_sql('DROP TRIGGER trg_assessment_revisions_no_update' +
            (' ON education_assessment_revisions' if native_engine.dialect.name == 'postgresql' else ''))
        with pytest.raises(MigrationError, match='Assessment revision guards differ'):
            M.validate(connection)


@pytest.mark.parametrize('sqlstate,failures', [('40P01', 1), ('40P01', 2), ('40001', 1)])
def test_erasure_retries_only_one_deadlock_with_savepoint_rollback(native_engine, monkeypatch, sqlstate, failures):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session
    from modules.organizations import education_classes_privacy as privacy
    from ops.database_parity.education_classes_fixtures import seed, verify
    bootstrap_database(native_engine)
    with native_engine.begin() as connection:
        seed(connection)
    original = privacy._erase_classes
    attempts = 0

    def interrupted(db, user_id, identifiers):
        nonlocal attempts
        attempts += 1
        # Each attempt must start with all previous partial scrubbing rolled back.
        verify(db.connection())
        original(db, user_id, identifiers)
        if attempts <= failures:
            if native_engine.dialect.name == 'postgresql':
                # A real server error leaves the savepoint in an aborted state.
                db.execute(text("DO $$ BEGIN RAISE EXCEPTION 'injected conflict' USING ERRCODE = '" + sqlstate + "'; END $$"))
            else:
                error = RuntimeError('injected conflict')
                error.sqlstate = sqlstate
                raise DBAPIError('injected conflict', {}, error)

    monkeypatch.setattr(privacy, '_erase_classes', interrupted)
    succeeds = sqlstate == '40P01' and failures == 1
    with Session(native_engine) as db:
        db.execute(text("UPDATE models SET name='Outer transaction retained' WHERE id=990049"))
        if succeeds:
            privacy.erase_classes(db, 990049, {'d2-roundtrip'})
        else:
            with pytest.raises(DBAPIError):
                privacy.erase_classes(db, 990049, {'d2-roundtrip'})
        db.commit()
    assert attempts == (2 if sqlstate == '40P01' else 1)
    with native_engine.begin() as connection:
        assert connection.execute(text('SELECT name FROM models WHERE id=990049')).scalar_one() == 'Outer transaction retained'
        M.validate(connection)
        if succeeds:
            assert connection.execute(text('SELECT old_grade,new_grade,old_feedback,new_feedback,reviewed_by '
                'FROM education_assessment_revisions')).one() == ('', '', '', '', None)
        else:
            verify(connection)
        with pytest.raises(DBAPIError, match='assessment revisions are immutable'):
            with connection.begin_nested():
                connection.exec_driver_sql("UPDATE education_assessment_revisions SET new_grade='forged'")
