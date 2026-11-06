"""D3 fresh/upgrade/drift checks; PostgreSQL uses D2's exact owned fixture."""
import importlib
import subprocess
import sys
import tarfile
from pathlib import Path
import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from tests.test_contracts.test_education_classes_migration import pg_engine

M = importlib.import_module('core.schema.migrations.050_education_program_reports')


def test_fresh_repeat_and_d2_order(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'d3.db'))
    try:
        applied = bootstrap_database(engine)['applied']
        assert applied.index('python:037-education-memberships') < applied.index('python:049-education-classes') < applied.index(M.MIGRATION_ID)
        assert bootstrap_database(engine)['applied'] == []
        with engine.begin() as c:
            M.validate(c)
            c.exec_driver_sql('ALTER TABLE education_program_marks ADD COLUMN unwanted TEXT')
            with pytest.raises(MigrationError, match='schema differs'):
                M.validate(c)
    finally:
        engine.dispose()


def test_native_roundtrip_fixture_preserves_reporting_evidence(tmp_path):
    from ops.database_parity.education_classes_fixtures import seed as seed_classes
    from ops.database_parity.education_program_fixtures import seed, verify
    import sqlite3
    source = tmp_path / 'program.db'
    restored = tmp_path / 'restored.db'
    engine = create_engine('sqlite:///' + str(source))
    try:
        bootstrap_database(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            seed_classes(connection)
            seed(connection)
            verify(connection)
    finally:
        engine.dispose()
    with sqlite3.connect(source) as original, sqlite3.connect(restored) as copy:
        original.backup(copy)
    engine = create_engine('sqlite:///' + str(restored))
    try:
        assert bootstrap_database(engine)['applied'] == []
        with engine.connect() as connection:
            verify(connection)
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize('dialect', ['sqlite', 'postgresql'])
def test_d2_upgrade_preserves_rows_and_fk_shape(tmp_path, request, dialect):
    # Fixture skips only when the requested container executable cannot start PG.
    if dialect == 'postgresql':
        engine = request.getfixturevalue('pg_engine')
        with engine.begin() as c:
            c.exec_driver_sql('DROP SCHEMA public CASCADE')
            c.exec_driver_sql('CREATE SCHEMA public')
    else:
        engine = create_engine('sqlite:///' + str(tmp_path/'upgrade.db'))
    try:
        # Bootstrap the actual committed release D2 sources (already renumbered to 049) in a separate interpreter.
        # No D3 tables or ledger rows are created and then removed.
        source = tmp_path / 'd2-source'
        source.mkdir()
        archive = tmp_path / 'd2.tar'
        repository = Path(__file__).resolve().parents[2]
        subprocess.run(['git', 'archive', 'e6633ae4', 'backend', '-o', str(archive)],
                       cwd=repository, check=True, capture_output=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter='data')
        code = """
import sys,runpy
sys.path.insert(0,sys.argv[1])
from sqlalchemy import create_engine,inspect
from core.schema import bootstrap_database
engine=create_engine(sys.argv[2])
bootstrap_database(engine)
assert not inspect(engine).has_table('education_program_marks')
with engine.begin() as connection:
 runpy.run_path(sys.argv[3])['seed'](connection)
engine.dispose()
"""
        result = subprocess.run([sys.executable, '-I', '-c', code, str(source/'backend'),
            engine.url.render_as_string(hide_password=False),
            str(repository/'ops/database_parity/education_classes_fixtures.py')],
            cwd=source, text=True, capture_output=True, timeout=120)
        # Do not echo connection arguments or credentials if the process fails.
        assert result.returncode == 0, result.stderr
        from ops.database_parity.education_classes_fixtures import verify
        from core.schema.bootstrap import POST027_MIGRATIONS
        live_tail = [mid for mid, _ in POST027_MIGRATIONS]
        assert bootstrap_database(engine)['applied'] == live_tail[live_tail.index(M.MIGRATION_ID):]
        with engine.begin() as c:
            M.validate(c); verify(c)
            c.execute(text("INSERT INTO education_program_marks(class_id,org_id,user_id,participant,concentrator) VALUES('roundtrip049-class',990049,990049,TRUE,TRUE)"))
            c.execute(text("INSERT INTO education_instructor_hours(id,class_id,org_id,instructor_id,activity_date,hours) VALUES('hours050','roundtrip049-class',990049,990049,'2026-10-01',2.5)"))
        assert bootstrap_database(engine)['applied'] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize('missing', ['education_classes','education_memberships'])
def test_missing_prerequisite_refused_before_ddl(tmp_path, missing):
    from sqlalchemy import inspect
    engine = create_engine('sqlite:///' + str(tmp_path/'missing.db'))
    with engine.begin() as c:
        other = 'education_classes' if missing == 'education_memberships' else 'education_memberships'
        c.exec_driver_sql('CREATE TABLE ' + other + '(id INTEGER)')
        with pytest.raises(MigrationError, match=missing):
            M.apply(c)
        assert not inspect(c).has_table('education_program_marks')
    engine.dispose()


def test_postgres_build_report_executes_job_proof(pg_engine):
    from sqlalchemy.orm import Session
    from ops.database_parity.education_classes_fixtures import seed
    from core.economic_identity import _add_created
    from modules.jobs.models import Job
    from modules.organizations.education_program_reports import build_report
    from modules.organizations.education_program_schemas import ReportFilters
    with pg_engine.begin() as c:
        c.exec_driver_sql('DROP SCHEMA public CASCADE'); c.exec_driver_sql('CREATE SCHEMA public')
    bootstrap_database(pg_engine)
    with pg_engine.begin() as c:
        seed(c)
    with Session(pg_engine) as db:
        db.execute(text("UPDATE users SET role='admin' WHERE id=990049"))
        db.execute(text("UPDATE education_upload_operations SET final_path='/d3/proved.3mf' WHERE operation_id='roundtrip049'"))
        db.execute(text("UPDATE print_files SET stored_path='/d3/proved.3mf' WHERE id=990049"))
        job = _add_created(db, Job(item_name='D3 PG proof', model_id=990049, print_file_id=990049,
            status='completed', submitted_by=990049, charged_to_org_id=990049, charged_to_user_id=990049),
            'job', owner_org_id=990049)
        db.execute(text("UPDATE education_submissions SET job_id=:j,created_at='2026-10-01' WHERE id=990049"), {'j':job.id})
        db.execute(text('INSERT INTO print_archives(job_id,filament_used_grams,actual_duration_seconds) VALUES(:j,10,3600),(:j,5,1800)'), {'j':job.id})
        db.commit()
        report = build_report(db, {'id':990049, 'group_id':990049},
            ReportFilters(start_date='2026-10-01', end_date='2026-10-31'))
        assert report['summary']['jobs'] == 1
        assert report['summary']['excluded_unproved_jobs'] == 0
        assert report['summary']['print_hours'] == 1.5
        assert report['summary']['material_grams'] == 15
