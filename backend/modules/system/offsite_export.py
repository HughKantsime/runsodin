"""All the off-server backup job can do on this host, through one forced SSH command.

    latest                      newest scheduled backup's receipt, and the guardian's state
    send <filename>             that backup's bytes on stdout
    confirm <filename> <sha256> the job stored and verified it off this server

The authorized_keys entry (restrict, command=docker exec ...) means the key can run this and
nothing else; the arguments arrive in SSH_ORIGINAL_COMMAND and never reach a shell. The job holds
the encryption key and the NAS access. Neither is on this server.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from modules.system.backup_schedule import BACKUP_NAME, RECEIPT_SUFFIX, _scheduled_backups
from modules.system.backup_service import BackupValidationError, paths_from_database_url
from modules.system.db_guardian_status import offsite_path, read_status, status_path, write_status

USAGE = 2
REFUSED = 1


def _receipt(backups: Path, name: str) -> dict | None:
    if not BACKUP_NAME.fullmatch(name):
        return None
    receipt = read_status(backups / (name + RECEIPT_SUFFIX))
    return receipt if receipt.get("sha256") and (backups / name).is_file() else None


def latest(database: Path, backups: Path, output) -> int:
    scheduled = _scheduled_backups(backups) if backups.is_dir() else []
    status = read_status(status_path(database))
    newest = scheduled[0][1].name if scheduled else None
    receipt = _receipt(backups, newest) if newest else None
    payload = {
        "filename": newest if receipt else None,
        "sha256": receipt.get("sha256") if receipt else None,
        "size_bytes": receipt.get("size_bytes") if receipt else None,
        "created_at": receipt.get("created_at") if receipt else None,
        "integrity_state": (status.get("integrity") or {}).get("state"),
        "backup_state": (status.get("backup") or {}).get("state"),
    }
    output.write((json.dumps(payload, sort_keys=True) + "\n").encode())
    return 0


def send(backups: Path, name: str, output) -> int:
    if _receipt(backups, name) is None:
        return REFUSED
    with (backups / name).open("rb") as source:
        shutil.copyfileobj(source, output, 1024 * 1024)
    return 0


def confirm(database: Path, backups: Path, name: str, sha256: str) -> int:
    receipt = _receipt(backups, name)
    if receipt is None or receipt["sha256"] != sha256:
        return REFUSED
    write_status(offsite_path(database), {
        "filename": name, "sha256": sha256, "confirmed_at": datetime.now(timezone.utc).isoformat()})
    return 0


def main(arguments: list[str], output) -> int:
    try:
        paths = paths_from_database_url(os.getenv("DATABASE_URL", "sqlite:////data/odin.db"))
    except BackupValidationError:
        return REFUSED
    if arguments == ["latest"]:
        return latest(paths.database, paths.backups, output)
    if len(arguments) == 2 and arguments[0] == "send":
        return send(paths.backups, arguments[1], output)
    if len(arguments) == 3 and arguments[0] == "confirm":
        return confirm(paths.database, paths.backups, arguments[1], arguments[2])
    return USAGE


if __name__ == "__main__":
    code = main(os.environ.get("SSH_ORIGINAL_COMMAND", "").split(), sys.stdout.buffer)
    sys.stdout.buffer.flush()
    raise SystemExit(code)
