"""Every SQLite engine role enforces foreign keys (fresh boot needs it for migration 025)."""
import pytest
from sqlalchemy import text


@pytest.mark.parametrize('role',['api','bootstrap'])
def test_sqlite_engine_enables_foreign_keys_on_every_connection(tmp_path,role):
    from core.database_config import create_database_engine
    engine=create_database_engine(f'sqlite:///{tmp_path}/fk.db',role=role)
    for _ in range(2):  # NullPool: each connect is a fresh DB-API connection
        with engine.connect() as conn:
            assert conn.execute(text('PRAGMA foreign_keys')).scalar_one()==1
    engine.dispose()
