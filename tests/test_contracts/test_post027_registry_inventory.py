"""Every registered post-027 migration keeps the backup inventory exact.

PostgreSQL backups refuse a live schema whose tables differ from canonical_tables()
(validate_live_postgres_objects), and restores refuse table data outside it
(_ordered_restore_data). A table created by a migration on its own MetaData object and
not listed in RAW_REQUIRED_TABLES/COLUMNS (or the shared Base) would break both. The
native PostgreSQL round trip is ops/database_parity/run_post027_restore.py.
"""
import importlib

from sqlalchemy import create_engine, inspect

from core.schema import bootstrap_database
from core.schema.bootstrap import POST027_MIGRATIONS, RAW_REQUIRED_COLUMNS
from modules.system.postgres_backup_service import canonical_tables


def test_fresh_and_upgraded_schemas_hold_exactly_the_backup_inventory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'inventory.db'}")
    try:
        bootstrap_database(engine)
        physical = inspect(engine)
        tables = {name for name in physical.get_table_names() if not name.startswith("sqlite_")}
        assert tables == set(canonical_tables())
        for table, columns in RAW_REQUIRED_COLUMNS.items():
            assert set(columns) <= {c["name"] for c in physical.get_columns(table)}, table
    finally:
        engine.dispose()


def test_each_registered_migration_validates_on_the_booted_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'validate.db'}")
    try:
        bootstrap_database(engine)
        with engine.connect() as connection:
            for _migration_id, module_name in POST027_MIGRATIONS:
                importlib.import_module(module_name).validate(connection)
    finally:
        engine.dispose()
