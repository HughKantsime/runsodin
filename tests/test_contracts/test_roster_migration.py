"""D4 own post027 migration; same after037 ordering and PG fixture as D2."""
import importlib
from sqlalchemy import create_engine, text, inspect
import pytest
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from tests.test_contracts.test_education_classes_migration import pg_engine
M = importlib.import_module('core.schema.migrations.051_roster_sync')


def test_fresh_repeat_and_shape(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'roster.db'))
    try:
        applied = bootstrap_database(engine)['applied']
        assert applied.index('python:037-education-memberships') < applied.index('python:049-education-classes') < applied.index(M.MIGRATION_ID)
        assert bootstrap_database(engine)['applied'] == []
        with engine.begin() as c:
            M.validate(c)
            c.exec_driver_sql('ALTER TABLE education_roster_identities ADD COLUMN drift TEXT')
            with pytest.raises(MigrationError, match='Roster schema differs'): M.validate(c)
    finally: engine.dispose()


def test_d2_upgrade_retains_classes_and_memberships(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'upgrade.db'))
    bootstrap_database(engine)
    with engine.begin() as c:
        # Remove only D4's additive footprint, reproducing the installed D2 schema.
        for name in reversed(M.DDL): c.exec_driver_sql('DROP TABLE ' + name)
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:m'), {'m':M.MIGRATION_ID})
        c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(990051,'School',TRUE)"))
    assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]
    with engine.connect() as c:
        assert c.execute(text('SELECT name FROM groups WHERE id=990051')).scalar() == 'School'
    engine.dispose()


def test_postgres_fresh_and_pre037_order(pg_engine):
    # Docker fixture skips when the requested ODIN_CONTAINER_EXEC=/usr/bin/false
    # makes PostgreSQL unavailable. Claude executes this unchanged during review.
    from core.schema import preserved036
    from pathlib import Path
    import subprocess, sys, tempfile
    for contract in (None, preserved036):
        with pg_engine.begin() as c:
            c.exec_driver_sql('DROP SCHEMA public CASCADE'); c.exec_driver_sql('CREATE SCHEMA public')
        if contract:
            code = "import runpy,sys;runpy.run_path(sys.argv[1],run_name='frozen036')['prepare_sources']();from sqlalchemy import create_engine;from core.schema import bootstrap_database;bootstrap_database(create_engine(sys.argv[2]))"
            with tempfile.TemporaryDirectory() as cwd:
                r = subprocess.run([sys.executable, '-I', '-c', code, str(Path(contract.__file__).with_name('runner.py')),
                    pg_engine.url.render_as_string(hide_password=False)], cwd=cwd, capture_output=True, text=True, timeout=90)
                assert r.returncode == 0, 'Preserved036 bootstrap failed'
        applied = bootstrap_database(pg_engine)['applied']
        assert applied.index('python:037-education-memberships') < applied.index(M.MIGRATION_ID)
        with pg_engine.begin() as c: M.validate(c)
        assert bootstrap_database(pg_engine)['applied'] == []


def test_native_roundtrip_fixture_keeps_d2_and_roster_foreign_keys(tmp_path):
    from ops.database_parity.education_classes_fixtures import seed as seed_classes, verify as verify_classes
    from ops.database_parity.education_roster_fixtures import seed, verify
    engine = create_engine('sqlite:///' + str(tmp_path/'roundtrip.db'))
    bootstrap_database(engine)
    with engine.begin() as c:
        c.exec_driver_sql('PRAGMA foreign_keys=ON')
        seed_classes(c); seed(c); verify_classes(c); verify(c)
    engine.dispose()


@pytest.mark.parametrize('missing', ['education_classes', 'education_memberships'])
def test_migration_refuses_missing_prerequisite(tmp_path, missing):
    engine = create_engine('sqlite:///' + str(tmp_path/'missing.db'))
    try:
        with engine.begin() as c:
            other = 'education_memberships' if missing == 'education_classes' else 'education_classes'
            c.exec_driver_sql('CREATE TABLE ' + other + '(id INTEGER)')
            with pytest.raises(MigrationError, match='Roster migration requires ' + missing): M.apply(c)
            assert not any(inspect(c).has_table(table) for table in M.DDL)
    finally: engine.dispose()
