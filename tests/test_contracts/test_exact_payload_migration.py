"""Actual additive upgrade, adoption and idempotent migration receipts."""
from pathlib import Path
import pytest
from sqlalchemy import create_engine, inspect, text
from core.schema.migrator import run_migration_files, MigrationError

MIGRATION=Path(__file__).resolve().parents[2]/'backend/modules/jobs/migrations/002_exact_print_payload.sql'
IDENTIFIER='modules/jobs/migrations/002_exact_print_payload.sql'


def test_payload_migration_upgrades_and_preserves_legacy_job(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE jobs(id INTEGER PRIMARY KEY,item_name VARCHAR(200))'))
        conn.execute(text("INSERT INTO jobs VALUES(1,'Legacy job')"))
    run_migration_files(engine,[(IDENTIFIER,MIGRATION)])
    with engine.begin() as conn:
        row=conn.execute(text('SELECT item_name,print_file_id,plate_index,ams_mapping,payload_sha256 FROM jobs')).one()
        assert tuple(row)==('Legacy job',None,None,None,None)
        conn.execute(text("UPDATE jobs SET print_file_id=9,plate_index=1,ams_mapping='[3]',payload_sha256=:hash WHERE id=1"),{'hash':'a'*64})
    run_migration_files(engine,[(IDENTIFIER,MIGRATION)])
    with engine.connect() as conn:
        assert tuple(conn.execute(text('SELECT print_file_id,plate_index,ams_mapping,payload_sha256 FROM jobs')).one())==(9,1,'[3]','a'*64)
        assert conn.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':IDENTIFIER}).scalar_one()==1
    engine.dispose()


def test_payload_migration_rejects_wrong_adopted_column_shape(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path / "bad.db"}')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE jobs(id INTEGER PRIMARY KEY,print_file_id TEXT)'))
    with pytest.raises(MigrationError):
        run_migration_files(engine,[(IDENTIFIER,MIGRATION)])
    engine.dispose()
