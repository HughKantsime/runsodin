"""D1 forward migration, exact shapes, tenant FKs and released046 restore routing."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import pytest
from sqlalchemy import create_engine, inspect, text
from core.schema import bootstrap_database, preserved046, preserved029
from core.schema.audit_integrity_version import MIGRATION_ID as AUDIT_MIGRATION_ID
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS
from core.schema.migrator import MigrationError

M = importlib.import_module('core.schema.migrations.048_training_signoff')
ROOT = Path(__file__).resolve().parents[2]


def test_frozen046_upgrade_preserves_rows_and_exact_ledger(tmp_path):
    code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen046')['prepare_sources']()
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:046-tray-identity' in bootstrap_database(engine)['applied']
with engine.begin() as c:
 c.execute(text("INSERT INTO printers(id,name) VALUES(1,'Retained')"))
assert bootstrap_database(engine)['applied']==[]
'''
    result = subprocess.run([sys.executable, '-I', '-c', code,
        str(Path(preserved046.__file__).with_name('runner.py')), str(tmp_path/'old.db')],
        cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///'+str(tmp_path/'old.db'))
    assert bootstrap_database(engine)['applied'] == [AUDIT_MIGRATION_ID, M.MIGRATION_ID, 'python:049-education-classes', 'python:050-education-program-reports']
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert c.execute(text('SELECT name FROM printers')).scalar_one() == 'Retained'
        M.validate(c)


def test_inventory_and_fresh_boot(tmp_path):
    from modules.system.postgres_backup_service import canonical_tables
    engine = create_engine('sqlite:///'+str(tmp_path/'fresh.db'))
    assert M.MIGRATION_ID in bootstrap_database(engine)['applied']
    assert bootstrap_database(engine)['applied'] == []
    for table, columns in M.COLUMNS.items():
        assert table in canonical_tables()
        assert RAW_REQUIRED_COLUMNS[table] == frozenset(columns.split())
        assert set(columns.split()) == {c['name'] for c in inspect(engine).get_columns(table)}


def test_wrong_existing_shape_refuses_without_recording_ledger(tmp_path):
    engine = create_engine('sqlite:///'+str(tmp_path/'wrong.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE education_trainings(id INTEGER PRIMARY KEY)')
    with pytest.raises(MigrationError, match='Training schema shape'):
        bootstrap_database(engine)
    with engine.connect() as c:
        assert not c.execute(text('SELECT 1 FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID}).first()


@pytest.mark.parametrize('version', ['2.0.31', '2.0.44'])
def test_released046_is_routed_without_rewriting_ledger(tmp_path, monkeypatch, version):
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    releases = json.loads((ROOT/'backend/core/schema/released_post027_ledgers.json').read_text())['releases']
    assert preserved046.ledger_ids() == preserved029.ledger_ids() | set(releases[version])
    archive = tmp_path/'owned.dump'; archive.write_bytes(b'x'*100)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: preserved046.ledger_ids())
    assert stage.identify_contract(archive, None, None, tmp_path) is stage.PRESERVED046


def test_frozen046_manifest_detects_tampering(tmp_path):
    from core.schema.preserved046.integrity import check_package, Preserved046Error
    (tmp_path/'manifest.json').write_text('{}')
    with pytest.raises(Preserved046Error, match='manifest differs'):
        check_package(tmp_path)


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved046 stage ledger differs: owned fixture', True),
    ('Preserved046 package hash differs', False),
])
def test_restore_classifies_preserved046_stage_errors(tmp_path,monkeypatch,message,archive_refusal):
    from unittest.mock import MagicMock
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    from core import database_config
    archive=tmp_path/'owned.dump';archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage,'identify_contract',lambda *a:stage.PRESERVED046)
    monkeypatch.setattr(backup,'_require_distinct_maintenance_secret',lambda *a:None)
    monkeypatch.setattr(backup,'_maintenance_urls',lambda *a:('unused','unused'))
    engine=MagicMock();engine.execution_options.return_value=engine
    monkeypatch.setattr(database_config,'create_database_engine',lambda *a,**k:engine)
    for name in ('sweep_stale','record','forget','drop_database'):
        monkeypatch.setattr(restore_artifacts,name,lambda *a,**k:None)
    def refuse(*a,**k): raise preserved046.Preserved046Error(message)
    monkeypatch.setattr(preserved046,'restore_into_stage',refuse)
    expected=backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected,match=message):
        with stage.prepared_archive(archive,'postgresql://localhost/target','postgresql://localhost/maintenance',None,None,tmp_path):
            pytest.fail('Invalid preserved stage must not be yielded')



# Exercise the public bootstrap separately from 037's custom-index preservation
# test: the older 002 validator intentionally refuses that test's custom index.
from tests.test_contracts.test_education_membership_migration import engine
from tests.test_contracts.test_post027_migration_033_postgres import pg_engine


def test_full_bootstrap_from_pre037_orders_memberships_before_training(engine, monkeypatch):
    import core.schema.bootstrap as bootstrap
    with monkeypatch.context() as old:
        original = bootstrap.POST027_MIGRATIONS
        dedicated = bootstrap.apply_dedicated_python_migration
        regular = bootstrap.apply_python_migration
        omitted = {'education_memberships', 'education_trainings', 'education_training_records', 'education_classes', 'education_assignments', 'education_assignment_submissions', 'education_assessment_revisions', 'education_program_marks', 'education_instructor_hours'}
        old.setattr(bootstrap, 'POST027_MIGRATIONS', tuple(pair for pair in original if pair[0] not in {'python:037-education-memberships', M.MIGRATION_ID, 'python:049-education-classes', 'python:050-education-program-reports'}))
        old.setattr(bootstrap, 'apply_dedicated_python_migration', lambda e, n: False if n.endswith('037_education_memberships') else dedicated(e, n))
        old.setattr(bootstrap, 'apply_python_migration', lambda c, n: False if n.endswith(('048_training_signoff', '049_education_classes', '050_education_program_reports')) else regular(c, n))
        old.setattr(bootstrap, 'RAW_REQUIRED_TABLES', bootstrap.RAW_REQUIRED_TABLES - omitted)
        old.setattr(bootstrap, 'RAW_REQUIRED_COLUMNS', {k: v for k, v in bootstrap.RAW_REQUIRED_COLUMNS.items() if k not in omitted})
        bootstrap_database(engine)
    assert not inspect(engine).has_table('education_memberships')
    with engine.begin() as c:
        c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Retained school',TRUE)"))
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'Retained student','fixture','viewer',TRUE,1)"))
    assert bootstrap_database(engine)['applied'] == ['python:037-education-memberships', M.MIGRATION_ID, 'python:049-education-classes', 'python:050-education-program-reports']
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert c.execute(text('SELECT user_id,org_id FROM education_memberships')).one() == (1, 1)
        M.validate(c)
