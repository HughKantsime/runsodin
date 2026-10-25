"""Packaged guard closure, historical subsets and real SQLite backup admission."""
import importlib
import sqlite3
from pathlib import Path

import pytest

from modules.system import backup_service as service
from tests.test_contracts.test_education_review_workflow import review_db
from tests.backup_restore.test_backup_service import _database


def _columns(path):
    with sqlite3.connect(path) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: {r[1] for r in db.execute('PRAGMA table_info("'+t+'")')} for t in tables}


def test_current_union_and_every_missing_guard_refused(review_db, tmp_path):
    path = Path(review_db.get_bind().url.database)
    allowed = service._canonical_sqlite_triggers()
    assert len(allowed) == 90
    with sqlite3.connect(path) as db:
        actual = {name: (table, service._normalized_schema_sql(sql)) for name, table, sql in db.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")}
    assert actual == allowed
    assert service.validate_database(path)['table_count'] > 30
    # Admission requires the supported dependency families present in this DB.
    required = service._required_sqlite_guards(_columns(path), allowed)
    measured = importlib.import_module('core.schema.migrations.039_measured_filament')
    assert set(measured.sqlite_triggers()) <= required
    assert "timeline_mutation_guard" in required
    for name in required:
        candidate = tmp_path / 'missing.sqlite'
        with sqlite3.connect(path) as original, sqlite3.connect(candidate) as copy:
            original.backup(copy)
            copy.execute('DROP TRIGGER '+name)
        with pytest.raises(service.BackupValidationError, match='missing required canonical trigger guards'):
            service.validate_database(candidate)
        candidate.unlink()


def test_every_dependency_column_and_table_is_required(review_db, tmp_path):
    path = Path(review_db.get_bind().url.database)
    columns = _columns(path)
    allowed = service._canonical_sqlite_triggers()
    service._required_sqlite_guards(columns, allowed)
    maps = [service._COST_COLUMNS, service._SOURCE_COLUMNS, service._MEMBERSHIP_COLUMNS,
            service._PURCHASE_COLUMNS, service._ORIGIN_COLUMNS,
            {'power_target_operations': service._POWER_COLUMNS,
             'audit_actor_erasures': 'user_id erased_at',
             'audit_logs': 'actor_user_id actor_username actor_auth_kind'}]
    checked = set()
    for mapping in maps:
        for table, names in mapping.items():
            for column in names.split():
                if (table,column) in checked:
                    continue
                checked.add((table,column))
                changed = {t:set(c) for t,c in columns.items()}
                changed[table].remove(column)
                with pytest.raises(service.BackupValidationError, match='incomplete supported trigger dependencies'):
                    service._required_sqlite_guards(changed, allowed)
    # Actual owned SQLite copies refuse removed dependency tables before restore.
    for table in ('cost_identities','cost_job_bindings','cost_source_lifetimes',
                  'timelapse_media_versions','audit_actor_erasures','cost_global_origin_exclusions'):
        candidate = tmp_path / 'dependency.sqlite'
        with sqlite3.connect(path) as original, sqlite3.connect(candidate) as copy:
            original.backup(copy)
            copy.execute('DROP TABLE '+table)
        with pytest.raises(service.BackupValidationError):
            service.validate_database(candidate)
        candidate.unlink()
    assert len(checked) > 200


def test_legacy_and_selected_identity_subset(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    _database(path,'legacy')
    service.validate_database(path)
    contract = importlib.import_module('core.schema.migrations.009_cost_foundation')
    with sqlite3.connect(path) as db:
        for kind,name,table,sql in contract.EXPECTED_SQLITE:
            if (kind == 'table' and name == 'cost_identities') or (kind == 'trigger' and table == 'cost_identities'):
                db.execute(sql)
    service.validate_database(path)
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER cost_identities_no_replace')
    with pytest.raises(service.BackupValidationError, match='missing required canonical trigger guards'):
        service.validate_database(path)


@pytest.mark.parametrize('suffix',service._LATE_TRIGGER_MODULES[2:])
def test_late_packaged_integrity_failure_closed(review_db,monkeypatch,suffix):
    module = importlib.import_module('core.schema.migrations.'+suffix)
    def unavailable():
        raise RuntimeError('synthetic source mismatch')
    monkeypatch.setattr(module,'sqlite_triggers' if suffix.startswith(('037_', '038_', '039_', '040_')) else '_source',unavailable)
    with pytest.raises(service.BackupValidationError, match='Packaged backup trigger contract is unavailable'):
        service.validate_database(Path(review_db.get_bind().url.database))


def test_duplicate_packaged_name_closed(monkeypatch):
    module = importlib.import_module('core.schema.migrations.022_economic_source_lifetimes')
    monkeypatch.setattr(module,'_sqlite_triggers',lambda: {
        'cost_identities_no_update': 'CREATE TRIGGER cost_identities_no_update BEFORE UPDATE ON cost_identities BEGIN SELECT 1; END'})
    with pytest.raises(service.BackupValidationError, match='Packaged backup trigger contract is unavailable'):
        service._canonical_sqlite_triggers()


@pytest.mark.parametrize('tamper',['body','target','literal','order'])
def test_late_exact_definition_and_no_execution(review_db,tamper):
    path = Path(review_db.get_bind().url.database)
    with sqlite3.connect(path) as db:
        name = 'ptop_insert_guard'
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?",(name,)).fetchone()[0]
        db.execute('CREATE TABLE synthetic_marker(value TEXT)')
        db.execute('DROP TRIGGER '+name)
        if tamper == 'target':
            sql = sql.replace('ON power_target_operations','ON users')
        elif tamper == 'literal':
            sql = sql.replace('power operation must be fresh','POWER OPERATION MUST BE FRESH')
        elif tamper == 'order':
            sql = sql.replace('BEGIN SELECT','BEGIN SELECT 1; SELECT')
        else:
            sql = sql.split('BEGIN')[0]+"BEGIN INSERT INTO synthetic_marker VALUES ('executed'); END"
        db.execute(sql)
    with pytest.raises(service.BackupValidationError, match='unexpected triggers or views'):
        service.validate_database(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM synthetic_marker').fetchone()[0] == 0


def test_current_copy_stage_apply_rollback_and_finalize(review_db,tmp_path):
    source = Path(review_db.get_bind().url.database)
    live = tmp_path / 'live.sqlite'
    with sqlite3.connect(source) as original, sqlite3.connect(live) as db:
        original.backup(db)
        db.execute("INSERT INTO audit_actor_erasures(user_id,erased_at) VALUES (100,'2026-10-13 12:00:00')")
        db.execute("INSERT INTO system_config(key,value) VALUES ('marker','before')")
    url = 'sqlite:///'+str(live)
    candidate,metadata = service.create_online_backup(url,base_dir=tmp_path/'backup')
    assert metadata['table_count'] > 30
    with sqlite3.connect(candidate) as db:
        db.execute("UPDATE system_config SET value='after' WHERE key='marker'")
    base = tmp_path/'restore'
    service.stage_restore(candidate,url,base_dir=base)
    assert service.apply_pending_restore(url,base_dir=base,pid_file=None)['status'] == 'applied_pending_validation'
    assert service.rollback_pending_restore(url,base_dir=base,pid_file=None)['status'] == 'rolled_back'
    with sqlite3.connect(live) as db:
        assert db.execute("SELECT value FROM system_config WHERE key='marker'").fetchone() == ('before',)
        assert db.execute('SELECT user_id,erased_at FROM audit_actor_erasures').fetchall() == [(100,'2026-10-13 12:00:00')]
    service.stage_restore(candidate,url,base_dir=base)
    service.apply_pending_restore(url,base_dir=base,pid_file=None)
    assert service.finalize_pending_restore(url,base_dir=base,pid_file=None)['status'] == 'restored'
    service.validate_database(live)
    with sqlite3.connect(live) as db:
        assert db.execute("SELECT value FROM system_config WHERE key='marker'").fetchone() == ('after',)
        assert db.execute('SELECT user_id,erased_at FROM audit_actor_erasures').fetchall() == [(100,'2026-10-13 12:00:00')]
        for sql in ("UPDATE audit_actor_erasures SET erased_at='2026-10-14 12:00:00'",'DELETE FROM audit_actor_erasures'):
            with pytest.raises(sqlite3.IntegrityError,match='audit actor erasure is immutable'):
                db.execute(sql)
