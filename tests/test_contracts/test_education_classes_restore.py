"""D2's preserved archive refusals remain client errors, without PostgreSQL I/O."""
import importlib
from unittest.mock import MagicMock
import pytest


@pytest.mark.parametrize('era', ['045', '046'])
def test_preserved_ledger_difference_is_archive_refused(tmp_path, monkeypatch, era):
    from modules.system import pre026_restore_stage as stage
    from modules.system import postgres_backup_service as backup, restore_artifacts
    from core import database_config
    preserved = importlib.import_module('core.schema.preserved' + era)
    archive = tmp_path / 'owned.dump'
    archive.write_bytes(b'owned-fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *args: getattr(stage, 'PRESERVED' + era))
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', lambda *args: None)
    monkeypatch.setattr(backup, '_maintenance_urls', lambda *args: ('fixture', 'fixture'))
    engine = MagicMock()
    engine.execution_options.return_value = engine
    engine.connect.return_value.__enter__.return_value.execute.return_value.one.return_value = (1, 2)
    monkeypatch.setattr(database_config, 'create_database_engine', lambda *args, **kwargs: engine)
    for name in ('sweep_stale', 'record', 'drop_database', 'forget'):
        monkeypatch.setattr(restore_artifacts, name, lambda *args, **kwargs: None)
    def refuse(*args, **kwargs):
        raise getattr(preserved, 'Preserved' + era + 'Error')('Preserved' + era + ' stage ledger differs: fixture')
    monkeypatch.setattr(preserved, 'restore_into_stage', refuse)
    with pytest.raises(backup.ArchiveRefused, match='stage ledger differs'):
        with stage.prepared_archive(archive, 'postgresql://fixture@localhost/target',
                'postgresql://fixture@localhost/postgres', None, None, tmp_path):
            pytest.fail('An invalid archive must not be yielded')


@pytest.mark.parametrize('tamper', [None, 'missing', 'changed'])
def test_sqlite_backup_accepts_only_canonical_assessment_guards(tmp_path, tamper):
    from sqlalchemy import create_engine
    from core.schema import bootstrap_database
    from modules.system.backup_service import validate_database, BackupValidationError
    path = tmp_path / 'school.db'
    engine = create_engine('sqlite:///' + str(path))
    try:
        bootstrap_database(engine)
        if tamper:
            with engine.begin() as connection:
                connection.exec_driver_sql('DROP TRIGGER trg_assessment_revisions_no_update')
                if tamper == 'changed':
                    connection.exec_driver_sql('CREATE TRIGGER trg_assessment_revisions_no_update '
                        'BEFORE UPDATE ON education_assessment_revisions BEGIN SELECT 1; END')
            with pytest.raises(BackupValidationError, match='trigger'):
                validate_database(path)
        else:
            assert validate_database(path)['table_count'] > 0
    finally:
        engine.dispose()
