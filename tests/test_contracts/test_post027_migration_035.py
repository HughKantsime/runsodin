"""Additive inspection schema preserves old rows and refuses incompatible shapes."""
import importlib
import json
from sqlalchemy import create_engine,inspect,text
import pytest
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS
from modules.system.postgres_backup_service import canonical_tables
M=importlib.import_module('core.schema.migrations.035_production_acceptance')


def test_upgrade_repeat_inventory_and_wrong_existing_shape(tmp_path):
    e=create_engine(f"sqlite:///{tmp_path/'schema.db'}")
    first=bootstrap_database(e)
    assert M.MIGRATION_ID in first['applied']
    for t in M.TABLES:
        assert t.name in canonical_tables()
        assert RAW_REQUIRED_COLUMNS[t.name]==frozenset(c['name'] for c in inspect(e).get_columns(t.name))
    assert bootstrap_database(e)['applied']==[]
    with e.begin() as c:
        c.execute(text("INSERT INTO system_config(key,value) VALUES('035-marker','\"kept\"')"))
        for t in reversed(M.TABLES):t.drop(c)
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID})
    assert bootstrap_database(e)['applied']==[M.MIGRATION_ID]
    with e.begin() as c:
        assert c.execute(text("SELECT value FROM system_config WHERE key='035-marker'")).scalar_one()=='"kept"'
        M.recipes.drop(c)
        c.execute(text('CREATE TABLE production_recipes(id TEXT PRIMARY KEY,owner INTEGER NOT NULL,definition TEXT NOT NULL)'))
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID})
    with pytest.raises(MigrationError):bootstrap_database(e)
    with e.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==0
    e.dispose()


def test_new_preserved034_package_has_exact_source_identity():
    from core.schema import preserved034
    from pathlib import Path
    import hashlib
    bundle,manifest=preserved034.check_package()
    root=Path(__file__).resolve().parents[2]
    import subprocess
    for name,digest in manifest['sources'].items():
        original=subprocess.check_output(['git','show',manifest['source_commit']+':backend/'+name],cwd=root)
        assert hashlib.sha256(original).hexdigest()==digest
    assert set(M.EXPECTED_COLUMNS).isdisjoint(__import__('json').loads((bundle/'core/schema/orm_baselines.json').read_text()))
