"""Container-safe ODIN database bootstrap command."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from core.database_config import create_database_engine, database_identity
from core.db_integrity import DAMAGED, boot_refusal, check_database
from core.schema import bootstrap_database


def _refuse_damaged_sqlite(database_url: str) -> bool:
    """Say plainly why a damaged database stops startup, before anything writes to it.

    Migration 016's validator makes the same check on every start, but it comes after earlier
    migrations and its wording ("Media ownership database integrity differs") misleads. Its source
    is checksummed in the migration ledger, so it can't be reworded. A check that couldn't finish
    goes on to the migrations, which fail closed by themselves.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return False
    path = Path(url.database)
    if not path.is_file():
        return False
    report = check_database(path)
    if report.state != DAMAGED:
        return False
    print(boot_refusal(path, report), file=sys.stderr, flush=True)
    return True


def main() -> int:
    database_url = os.getenv("DATABASE_URL", "sqlite:////data/odin.db")
    if _refuse_damaged_sqlite(database_url):
        return 1
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
