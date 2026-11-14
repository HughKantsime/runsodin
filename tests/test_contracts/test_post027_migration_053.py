"""Independent additive term policy migration, upgrade and drift contracts."""
import importlib
import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
M = importlib.import_module('core.schema.migrations.053_term_retention')


def test_previous_head_upgrades_without_touching_rows(tmp_path, monkeypatch):
    from core.schema import bootstrap as boot
    original = boot.POST027_MIGRATIONS
    engine = create_engine('sqlite:///' + str(tmp_path / 'old.db'))
    with pytest.MonkeyPatch.context() as bootstrap_patch:
        bootstrap_patch.setattr(boot, 'POST027_MIGRATIONS', tuple(
            (migration_id, name) for migration_id, name in original if migration_id != M.MIGRATION_ID))
        # Only suppress new-table validation while constructing the old shape.
        bootstrap_patch.setattr(boot, 'RAW_REQUIRED_TABLES', boot.RAW_REQUIRED_TABLES - set(M.COLUMNS))
        bootstrap_patch.setattr(boot, 'RAW_REQUIRED_COLUMNS', {k:v for k,v in boot.RAW_REQUIRED_COLUMNS.items() if k not in M.COLUMNS})
        bootstrap_database(engine)
        with engine.begin() as c:
            c.execute(text("INSERT INTO groups(id,name) VALUES(91,'kept')"))
    assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert c.execute(text('SELECT name FROM groups WHERE id=91')).scalar() == 'kept'
        M.validate(c)
    engine.dispose()


def test_wrong_existing_shape_refused_without_ledger(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'drift.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE education_term_policies(id INTEGER PRIMARY KEY)')
    with pytest.raises(MigrationError, match='column shape'):
        bootstrap_database(engine)
    with engine.connect() as c:
        assert not c.execute(text('SELECT 1 FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID}).first()
    engine.dispose()


def test_frozen046_isolated_boot_then_live_upgrade(tmp_path):
    import subprocess, sys
    from pathlib import Path
    from core.schema import preserved046
    runner = Path(preserved046.__file__).with_name('runner.py')
    code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen046')['prepare_sources']()
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:046-tray-identity' in bootstrap_database(engine)['applied']
assert bootstrap_database(engine)['applied']==[]
with engine.begin() as c:c.execute(text("INSERT INTO groups(id,name) VALUES(91,'frozen')"))
'''
    result = subprocess.run([sys.executable, '-I', '-c', code, str(runner), str(tmp_path/'frozen.db')],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(tmp_path/'frozen.db'))
    from core.schema.bootstrap import POST027_MIGRATIONS
    assert bootstrap_database(engine)['applied'] == [
        migration_id for migration_id, _ in POST027_MIGRATIONS
        if migration_id not in preserved046.ledger_ids()
    ]
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert c.execute(text('SELECT name FROM groups WHERE id=91')).scalar() == 'frozen'
    engine.dispose()


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved046 stage ledger differs: owned fixture', True),
    ('Preserved046 package hash differs', False),
])
def test_restore_classifies_preserved046_stage_errors(tmp_path,monkeypatch,message,archive_refusal):
    from core.schema import preserved046
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


@pytest.mark.parametrize('changed', ['manifest.json', 'overlay/core/schema/migrations/046_tray_identity.py'])
def test_frozen046_tampering_is_refused(tmp_path, changed):
    from pathlib import Path
    import shutil
    from core.schema import preserved046
    from core.schema.preserved046.integrity import check_package, Preserved046Error
    source = Path(preserved046.__file__).parent
    target = tmp_path/'preserved046'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved045').symlink_to(source.parent/'preserved045', target_is_directory=True)
    with (target/changed).open('ab') as handle: handle.write(b'\n# tampered\n')
    with pytest.raises(Preserved046Error, match='differs'): check_package(target)


@pytest.mark.parametrize('drift', ['foreign_key', 'primary_key'])
def test_constraint_drift_is_refused(tmp_path, drift):
    engine = create_engine('sqlite:///' + str(tmp_path / 'constraints.db'))
    bootstrap_database(engine)
    with engine.begin() as c:
        ddl = c.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='education_term_cleanup'").scalar()
        c.exec_driver_sql('DROP TABLE education_term_cleanup')
        if drift == 'foreign_key':
            ddl = ddl.replace('ON DELETE RESTRICT', 'ON DELETE CASCADE')
        else:
            ddl = ddl.replace('PRIMARY KEY(term_id,submission_id,kind)', 'PRIMARY KEY(submission_id,term_id,kind)')
        c.exec_driver_sql(ddl)
        with pytest.raises(MigrationError, match='constraints differ|primary key differs'):
            M.validate(c)
    engine.dispose()


def test_preserved046_orm_baseline_is_byte_exact():
    import json
    from pathlib import Path
    from core.schema import preserved046
    root = Path(preserved046.__file__).parent
    catalog = json.loads((root.parent/'orm_baselines.json').read_text())
    # 057 (2.2.0) changes the models, so it pins one new baseline. Every baseline
    # released through 046 must still serialize to the frozen bytes without it.
    assert set(catalog) - set(json.loads((root/'overlay/core/schema/orm_baselines.json').read_text())) == {'slot_feed_policy_057_20261112'}
    del catalog['slot_feed_policy_057_20261112']
    assert (root/'overlay/core/schema/orm_baselines.json').read_bytes() == (json.dumps(catalog, indent=2) + '\n').encode()
