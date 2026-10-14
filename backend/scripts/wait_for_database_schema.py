"""Wait for the designated enterprise bootstrap owner to publish current schema."""

from __future__ import annotations

import os
import time

from sqlalchemy import inspect

from core.database_config import create_database_engine, database_identity, DatabaseConfigurationError
from core.postgres_restore_gate import assert_postgres_restore_ready, RestoreConsumerBlocked
from core.schema import validate_schema
from core.schema.bootstrap import import_all_models


def main() -> int:
    try:
        postgres=database_identity(os.environ['DATABASE_URL'])['dialect']=='postgresql'
    except DatabaseConfigurationError:
        raise DatabaseConfigurationError('DATABASE_URL is invalid') from None
    if postgres:assert_postgres_restore_ready()
    timeout = int(os.getenv("ODIN_SCHEMA_WAIT_SECONDS", "120"))
    role = os.getenv("ODIN_DB_ROLE", "api")
    engine = create_database_engine(
        os.environ["DATABASE_URL"],
        role=role,
        password_file=os.getenv("DATABASE_PASSWORD_FILE"),
    )
    deadline = time.monotonic() + timeout
    import_all_models()
    while time.monotonic() < deadline:
        try:
            if postgres:assert_postgres_restore_ready()
            with engine.connect() as connection:
                if "odin_schema_migrations" in inspect(connection).get_table_names():
                    validate_schema(connection)
                    engine.dispose()
                    return 0
        except RestoreConsumerBlocked:
            engine.dispose()
            raise
        except Exception:
            pass
        time.sleep(1)
    engine.dispose()
    raise RuntimeError("Timed out waiting for the ODIN database schema")


if __name__ == "__main__":
    raise SystemExit(main())
