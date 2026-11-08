"""Additive global decisions: exact inventory, immutable rows and released upgrades."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from core.schema import bootstrap_database
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS, RAW_REQUIRED_TABLES
from core.schema.migrator import MigrationError

M = importlib.import_module('core.schema.migrations.056_dispatch_recovery_global')
OLD = importlib.import_module('core.schema.migrations.038_dispatch_recovery')


def test_inventory_and_postgres_guards():
    from modules.system.postgres_backup_service import canonical_tables
    from modules.system.postgres_canonical_executables import canonical_functions, canonical_triggers
    assert M.history.name in RAW_REQUIRED_TABLES & canonical_tables()
    assert RAW_REQUIRED_COLUMNS[M.history.name] == set(M.history.c.keys()) == set(OLD.history.c.keys()) - {'owner_org_id', 'charged_org_id'}
    for name, body in M.pg_functions().items():
        assert canonical_functions()[name][0] == body
    for key, ddl in M.pg_triggers().items():
        assert canonical_triggers()[key] == (ddl, 'odin_global_recovery_immutable', 'public', 'O')
    assert any('BEFORE TRUNCATE' in ddl for ddl in M.pg_triggers().values())


def test_fresh_boot_repeat_and_immutable_history(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'fresh.db'))
    first = bootstrap_database(engine)
    assert first['applied'].count(M.MIGRATION_ID) == 1
    with engine.begin() as c:
        c.execute(M.history.insert().values(audit_id=1, actor_id=3, actor_identity='admin', printer_id=5, job_id=226,
            attempt_handle='dr1.'+'a'*64, snapshot_handle='ds1.'+'b'*64, decision='release',
            reason_category='verified_terminal_reconciled', facts='{"owner_org_id":null}'))
    second = bootstrap_database(engine)
    assert second['applied'] == [] and second['schema_fingerprint'] == first['schema_fingerprint']
    for sql in ("UPDATE dispatch_recovery_global_history SET facts='{}'", 'DELETE FROM dispatch_recovery_global_history'):
        with engine.begin() as c, pytest.raises(IntegrityError, match='immutable global recovery history'):
            c.exec_driver_sql(sql)
    with engine.connect() as c:
        assert c.execute(text('SELECT job_id FROM dispatch_recovery_global_history')).scalar_one() == 226
        assert inspect(c).get_foreign_keys(M.history.name) == []
    engine.dispose()


@pytest.mark.parametrize('weakening', ['shape', 'check', 'nullable', 'trigger'])
def test_wrong_existing_shape_refused_without_ledger(tmp_path, weakening):
    engine = create_engine('sqlite:///' + str(tmp_path / 'wrong.db'))
    with engine.begin() as c:
        if weakening == 'shape':
            c.exec_driver_sql('CREATE TABLE dispatch_recovery_global_history(id INTEGER PRIMARY KEY)')
        else:
            M.apply(c)
            if weakening == 'trigger':
                c.exec_driver_sql('DROP TRIGGER global_recovery_no_update')
            else:
                ddl = c.execute(text("SELECT sql FROM sqlite_master WHERE name='dispatch_recovery_global_history'")).scalar_one()
                c.exec_driver_sql('DROP TABLE dispatch_recovery_global_history')
                ddl = ddl.replace("decision IN ('hold','reconcile-retain','release')", '1=1') if weakening == 'check' else ddl.replace('actor_identity VARCHAR(128) NOT NULL', 'actor_identity VARCHAR(128)')
                c.exec_driver_sql(ddl)
                for trigger in M.sqlite_triggers().values():
                    c.exec_driver_sql(trigger)
    with pytest.raises(MigrationError):
        bootstrap_database(engine)
    with engine.connect() as c:
        assert not c.execute(text('SELECT 1 FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID}).first()
    engine.dispose()


def test_published_1924_fixture_upgrades_via_entrypoint(tmp_path):
    root = Path(__file__).resolve().parents[2]
    path = tmp_path / 'published.db'
    with sqlite3.connect(path) as c:
        c.executescript((root / 'tests/fixtures/released_databases/odin-v1.9.24-sqlite.sql').read_text())
    env = {**os.environ, 'DATABASE_URL': 'sqlite:///' + str(path), 'DATABASE_PATH': str(path),
           'ODIN_DB_ROLE': 'bootstrap', 'ODIN_DATA_DIR': str(tmp_path), 'JWT_SECRET_KEY': 'migration056-contract-only'}
    results = [subprocess.run([sys.executable, '-m', 'scripts.bootstrap_database'], cwd=root/'backend',
               env=env, capture_output=True, text=True, timeout=90) for _ in range(2)]
    assert all(r.returncode == 0 for r in results), [r.stderr[-3000:] for r in results]
    first, second = (json.loads(r.stdout.strip().splitlines()[-1]) for r in results)
    assert M.MIGRATION_ID in first['applied'] and second['applied'] == []
    assert first['schema_fingerprint'] == second['schema_fingerprint']
