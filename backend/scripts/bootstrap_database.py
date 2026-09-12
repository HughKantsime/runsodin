"""Container-safe ODIN database bootstrap command."""

from __future__ import annotations

import json
import os

from core.database_config import create_database_engine, database_identity
from core.schema import bootstrap_database


def main() -> int:
    database_url = os.getenv("DATABASE_URL", "sqlite:////data/odin.db")
    engine = create_database_engine(
        database_url,
        role="bootstrap",
        password_file=os.getenv("DATABASE_PASSWORD_FILE"),
    )
    result = bootstrap_database(engine)
    print(json.dumps({**database_identity(database_url), **result}, sort_keys=True))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
