"""Pinned historical ORM history survives upgrades without accepting corruption."""
import json
from pathlib import Path
import pytest
from sqlalchemy import create_engine, text, inspect
from core.schema import bootstrap
from core.schema.migrator import ensure_ledger, MigrationError

CATALOG = json.loads(Path(bootstrap.__file__).with_name('orm_baselines.json').read_text())

@pytest.mark.parametrize('entry', CATALOG.values(), ids=CATALOG.keys())
def test_supported_original_baseline_is_preserved(entry):
    bootstrap.import_all_models()
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        ensure_ledger(connection)
        connection.execute(text("INSERT INTO odin_schema_migrations(migration_id,checksum,dialect) VALUES('orm-baseline:1',:checksum,'sqlite')"), {'checksum': entry['checksum']})
        before = connection.execute(text('SELECT * FROM odin_schema_migrations')).all()
        assert not bootstrap._record_orm_baseline(connection)
        assert connection.execute(text('SELECT * FROM odin_schema_migrations')).all() == before
    engine.dispose()

@pytest.mark.parametrize('checksum,dialect', [('0'*64,'sqlite'),(CATALOG['released_1_9_24']['checksum'],'postgresql')])
def test_bad_history_refused_before_any_schema_mutation(tmp_path, checksum, dialect):
    engine = create_engine(f'sqlite:///{tmp_path / "bad.db"}')
    with engine.begin() as connection:
        ensure_ledger(connection)
        connection.execute(text("INSERT INTO odin_schema_migrations(migration_id,checksum,dialect) VALUES('orm-baseline:1',:checksum,:dialect)"), {'checksum':checksum,'dialect':dialect})
        before = connection.execute(text("SELECT name,sql FROM sqlite_master ORDER BY name")).all()
    with pytest.raises(MigrationError, match='history mismatch'):
        bootstrap.bootstrap_database(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name,sql FROM sqlite_master ORDER BY name")).all() == before
        assert connection.execute(text('SELECT checksum,dialect FROM odin_schema_migrations')).one() == (checksum,dialect)
    engine.dispose()

def test_unpinned_current_models_refused_without_ledger_or_tables(monkeypatch):
    engine=create_engine('sqlite:///:memory:')
    monkeypatch.setattr(bootstrap,'_orm_manifest',lambda:{'unreviewed':['id']})
    with pytest.raises(MigrationError,match='not pinned'):
        bootstrap.bootstrap_database(engine)
    assert inspect(engine).get_table_names() == []
    engine.dispose()

def test_recognized_baseline_does_not_excuse_missing_current_schema():
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        ensure_ledger(connection)
        connection.execute(text("INSERT INTO odin_schema_migrations(migration_id,checksum,dialect) VALUES('orm-baseline:1',:checksum,'sqlite')"),{'checksum':CATALOG['released_1_9_24']['checksum']})
        bootstrap._validate_orm_baseline(connection)
        with pytest.raises(RuntimeError,match='missing tables'):
            bootstrap.validate_schema(connection)
    engine.dispose()


def test_historical_models_are_not_the_current_build(monkeypatch):
    engine=create_engine('sqlite:///:memory:')
    monkeypatch.setattr(bootstrap,'_orm_manifest',lambda:CATALOG['released_1_9_24']['manifest'])
    with pytest.raises(MigrationError,match='not pinned'):
        bootstrap.bootstrap_database(engine)
    assert inspect(engine).get_table_names() == []
    engine.dispose()
