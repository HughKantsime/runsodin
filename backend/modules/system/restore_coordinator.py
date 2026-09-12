"""Container-startup entry point for applying a staged SQLite restore."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules.system.backup_service import (
    apply_pending_restore,
    finalize_pending_restore,
    rollback_pending_restore,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--pid-file", type=Path, default=Path("/var/run/supervisord.pid"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--finalize", action="store_true")
    action.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.finalize:
        result = finalize_pending_restore(args.database_url, pid_file=args.pid_file)
    elif args.rollback:
        result = rollback_pending_restore(args.database_url, pid_file=args.pid_file)
    else:
        result = apply_pending_restore(args.database_url, pid_file=args.pid_file)
    if result:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
