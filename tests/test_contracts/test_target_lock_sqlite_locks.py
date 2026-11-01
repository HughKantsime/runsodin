"""The target lock must never open the database file.

On POSIX, closing any descriptor for a file drops every lock the process holds on that file, SQLite's
included. Another process closing what then looks like the last connection checkpoints and deletes
the WAL under a connection that is still live. Before SQLite 3.51 that corrupts the database, and a
rollback goes back to 3.46.1, so this can't rely on the library. Throwaway databases only.
"""
import fcntl
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from core import dispatch_target_lock
from core.dispatch_target_lock import acquire_target_lock

ROOT = Path(__file__).resolve().parents[2]
KEY = "a" * 64
PAY = "x" * 400
# Where SQLite holds its SHARED lock on the database file.
SHARED_FIRST, SHARED_SIZE = 0x40000002, 510
LIBRARY_RESISTS = sqlite3.sqlite_version_info >= (3, 51, 0)

HOLDER = r"""
import os, sqlite3, sys
from sqlalchemy import create_engine
from core.dispatch_target_lock import acquire_target_lock
db, mode = sys.argv[1], sys.argv[2]
key = "a" * 64
pay = "x" * 400
connection = None
for line in sys.stdin:
    command = line.split()
    try:
        if command[0] == "open":
            connection = sqlite3.connect(db, timeout=10, isolation_level=None)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE"); connection.execute("INSERT INTO a_rows(p) VALUES(?)", (pay,)); connection.execute("COMMIT")
            out = "1"
        elif command[0] == "drop":
            if mode == "raw":
                os.close(os.open(db, os.O_RDONLY)); out = "raw"
            elif mode == "lock":
                result = acquire_target_lock(create_engine(f"sqlite:///{db}"), key)
                held = result.handle.held()
                out = f"{result.status} {held} {result.handle.close().released}"
            elif mode == "conflict":
                engine = create_engine(f"sqlite:///{db}")
                first = acquire_target_lock(engine, key)
                second = acquire_target_lock(engine, key)
                out = f"{first.status} {second.status} {first.handle.close().released}"
            else:
                out = "nothing"
        elif command[0] == "write":
            done = 0
            for _ in range(int(command[1])):
                connection.execute("BEGIN IMMEDIATE"); connection.execute("INSERT INTO a_rows(p) VALUES(?)", (pay,)); connection.execute("COMMIT")
                done += 1
            out = str(done)
        elif command[0] == "close":
            connection.close(); out = "closed"
        else:
            break
    except Exception as error:
        out = f"ERROR {type(error).__name__} {error}"
    print(out, flush=True)
"""

WRITER = r"""
import sqlite3, sys
db, count = sys.argv[1], int(sys.argv[2])
connection = sqlite3.connect(db, timeout=10, isolation_level=None)
done = 0
try:
    for _ in range(count):
        connection.execute("BEGIN IMMEDIATE"); connection.execute("INSERT INTO b_rows(p) VALUES(?)", ("x" * 400,)); connection.execute("COMMIT")
        done += 1
except Exception as error:
    print("ERROR", type(error).__name__, error, file=sys.stderr)
connection.close()
print(done)
"""


def _environment():
    return {**os.environ, "PYTHONPATH": f"{ROOT / 'backend'}{os.pathsep}{ROOT}", "JWT_SECRET_KEY": "x", "PYTHONDONTWRITEBYTECODE": "1"}


def _database(tmp_path, name="odin.db"):
    folder = tmp_path / name.replace(".", "-")
    folder.mkdir(mode=0o755)
    database = folder / name
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE a_rows(id INTEGER PRIMARY KEY, p TEXT NOT NULL)")
    connection.execute("CREATE TABLE b_rows(id INTEGER PRIMARY KEY, p TEXT NOT NULL)")
    # Free pages for both writers to allocate from, which is what makes the damage show.
    connection.execute("CREATE TABLE junk(p TEXT)")
    connection.execute("BEGIN")
    for _ in range(3000):
        connection.execute("INSERT INTO junk VALUES(?)", (PAY,))
    connection.execute("COMMIT")
    connection.execute("DROP TABLE junk")
    connection.close()
    database.chmod(0o644)
    return database


class Holder:
    """Another process with a live connection, as a request or a monitor has."""

    def __init__(self, database, mode):
        self.process = subprocess.Popen([sys.executable, "-c", HOLDER, str(database), mode], env=_environment(), cwd=ROOT / "backend",
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.watchdog = threading.Timer(240, self.process.kill)
        self.watchdog.start()

    def __call__(self, command):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return self.process.stdout.readline().strip()

    def finish(self):
        self.watchdog.cancel()
        self.process.kill()
        self.process.wait()


def _sqlite_lock_held_elsewhere(database):
    """From a process with no connection of its own, so closing this descriptor costs nothing."""
    descriptor = os.open(database, os.O_RDWR)
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB, SHARED_SIZE, SHARED_FIRST)
    except OSError:
        return True
    else:
        fcntl.lockf(descriptor, fcntl.LOCK_UN, SHARED_SIZE, SHARED_FIRST)
        return False
    finally:
        os.close(descriptor)


def _write_elsewhere(database, count):
    done = subprocess.run([sys.executable, "-c", WRITER, str(database), str(count)], capture_output=True, text=True, timeout=120)
    lines = done.stdout.split()
    return int(lines[-1]) if lines else 0


@pytest.mark.parametrize("mode,expected,keeps_lock", [
    ("control", "nothing", True),
    ("raw", "raw", False),  # what the lock used to do: this proves the check can see the failure
    ("lock", "acquired True True", True),
    ("conflict", "acquired conflict True", True),
])
def test_only_a_raw_open_drops_the_lock_sqlite_holds(tmp_path, mode, expected, keeps_lock):
    database = _database(tmp_path)
    holder = Holder(database, mode)
    try:
        assert holder("open") == "1"
        assert _sqlite_lock_held_elsewhere(database) is True
        assert holder("drop") == expected
        assert _sqlite_lock_held_elsewhere(database) is keeps_lock
    finally:
        holder.finish()


@pytest.mark.parametrize("mode", ["control", "raw", "lock"])
def test_the_real_lock_cannot_get_the_wal_deleted_under_a_live_connection(tmp_path, mode):
    """The reviewer's reproduction, on whichever library this runs on, with the real function."""
    database = _database(tmp_path)
    holder = Holder(database, mode)
    a_committed = b_committed = 0
    errors = []
    try:
        a_committed += int(holder("open"))
        holder("drop")
        b_committed += _write_elsewhere(database, 1)
        wal_survived = Path(f"{database}-wal").exists()
        for _ in range(12):
            written = holder("write 150")
            if written.isdigit():
                a_committed += int(written)
            else:
                errors.append(written)
            b_committed += _write_elsewhere(database, 150)
        holder("close")
    finally:
        holder.finish()
    if mode == "raw":
        # Nothing about the data: before 3.51 this is the corruption, and it needn't show the same way every run.
        assert wal_survived is LIBRARY_RESISTS, "the reproduction has stopped reproducing, so it proves nothing"
        return
    assert wal_survived is True and errors == []
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("SELECT count(*) FROM a_rows").fetchone()[0] == a_committed
        assert connection.execute("SELECT count(*) FROM b_rows").fetchone()[0] == b_committed
    finally:
        connection.close()


AUDIT = r"""
import json, os, sys
db = sys.argv[1]
live = {db + suffix for suffix in ("", "-wal", "-shm")}
relative = {os.path.basename(path) for path in live}
seen = []
def hook(event, arguments):
    if event != "open" or not isinstance(arguments[0], (str, bytes, os.PathLike)):
        return
    path = os.fsdecode(arguments[0])
    # A relative name is one opened through a directory descriptor. A backup's scratch copy is also called odin.db.
    if path in live or (not os.path.isabs(path) and os.path.basename(path) in relative):
        seen.append(path)
sys.addaudithook(hook)
"""

LOCK_FLOW = AUDIT + r"""
from sqlalchemy import create_engine
from core.dispatch_target_lock import acquire_target_lock
engine = create_engine(f"sqlite:///{db}")
first = acquire_target_lock(engine, "a" * 64)
assert first.status == "acquired" and first.handle.held()
assert acquire_target_lock(engine, "a" * 64).status == "conflict"
assert first.handle.close().released
os.chmod(os.path.dirname(db), 0o775)
assert acquire_target_lock(engine, "b" * 64).status == "uncertain"
print(json.dumps(seen))
seen.clear()
os.close(os.open(db, os.O_RDONLY))
print(json.dumps(seen))
"""


def test_the_lock_code_never_opens_the_database_file(tmp_path):
    database = _database(tmp_path)
    done = subprocess.run([sys.executable, "-c", LOCK_FLOW, str(database)], env=_environment(), cwd=ROOT / "backend",
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    by_the_lock, by_the_check = (json.loads(line) for line in done.stdout.splitlines()[-2:])
    assert by_the_lock == []
    assert by_the_check == [str(database)], "the hook has to see a raw open, or the empty list proves nothing"


BACKUP_FLOW = AUDIT + r"""
import sqlite3
from sqlalchemy import create_engine
from core.db_integrity import check_database
from core.schema import bootstrap_database
from modules.system.backup_schedule import create_scheduled_backup
from modules.system.backup_service import create_online_backup, require_healthy_source, stage_restore
from modules.system.db_guardian_status import guardian_health
url = f"sqlite:///{db}"
bootstrap_database(create_engine(url))
connection = sqlite3.connect(db, timeout=10, isolation_level=None)
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("SELECT count(*) FROM users").fetchone()
seen.clear()
assert check_database(db).state == "ok"
require_healthy_source(__import__("pathlib").Path(db))
guardian_health(url)
scheduled = create_scheduled_backup(url)
manual, _ = create_online_backup(url)
stage_restore(manual, url, actor_id=None)
print(json.dumps(seen), flush=True)
sys.stdin.readline()
connection.close()
"""


def test_the_backup_and_restore_paths_a_running_process_uses_never_open_it(tmp_path):
    """The API and the guardian hold live connections when they run these."""
    folder = tmp_path / "data"
    folder.mkdir(mode=0o755)
    database = folder / "odin.db"
    process = subprocess.Popen([sys.executable, "-c", BACKUP_FLOW, str(database)], env=_environment(), cwd=ROOT / "backend",
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(240, process.kill)
    watchdog.start()
    try:
        line = process.stdout.readline()
        assert line, process.stderr.read()[-2000:]
        assert json.loads(line) == []
        assert _sqlite_lock_held_elsewhere(database) is True
    finally:
        watchdog.cancel()
        process.kill()
        process.wait()


# --- Every identity and safety check the descriptor gave still holds without one.

def _plain_database(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir(mode=0o755)
    database = folder / "odin.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t(id INTEGER)")
    connection.close()
    return database


def _acquire(database):
    return acquire_target_lock(create_engine(f"sqlite:///{database}"), KEY)


def test_a_lock_holds_while_nothing_changes_and_releases_once(tmp_path):
    database = _plain_database(tmp_path)
    result = _acquire(database)
    assert result.status == "acquired" and result.handle.held() is True
    assert _acquire(database).status == "conflict"
    assert result.handle.close().released is True
    assert result.handle.held() is False
    assert _acquire(database).handle.close().released is True


@pytest.mark.parametrize("change", ["replaced", "deleted", "symlinked", "hard_linked"])
def test_a_database_that_changes_under_the_lock_loses_it(tmp_path, change):
    database = _plain_database(tmp_path)
    result = _acquire(database)
    assert result.status == "acquired"
    if change == "replaced":
        other = database.with_name("other.db")
        shutil.copyfile(database, other)
        os.replace(other, database)
    elif change == "deleted":
        database.unlink()
    elif change == "symlinked":
        other = database.with_name("other.db")
        os.replace(database, other)
        database.symlink_to(other)
    else:
        os.link(database, database.with_name("second-name.db"))
    assert result.handle.held() is False
    assert result.handle.close().released is False


def test_a_parent_directory_that_stops_being_trusted_loses_it(tmp_path):
    database = _plain_database(tmp_path)
    result = _acquire(database)
    database.parent.chmod(0o775)
    assert result.handle.held() is False


@pytest.mark.parametrize("shape", ["missing", "symlink", "hard_link", "fifo", "directory", "open_parent"])
def test_an_unsafe_database_is_refused(tmp_path, shape):
    database = _plain_database(tmp_path)
    if shape == "missing":
        database.unlink()
    elif shape == "symlink":
        target = database.with_name("target.db")
        os.replace(database, target)
        database.symlink_to(target)
    elif shape == "hard_link":
        os.link(database, database.with_name("second-name.db"))
    elif shape == "fifo":
        database.unlink()
        os.mkfifo(database)  # opening one would hang: not opening it is part of the fix
    elif shape == "directory":
        database.unlink()
        database.mkdir()
    else:
        database.parent.chmod(0o775)
    assert _acquire(database).status == "uncertain"
    assert not Path(f"{database}.odin-sender-locks").exists(), "refused before it leaves anything beside the database"


def _with(value, **changes):
    fields = list(value)
    for name, field in changes.items():
        fields[{"st_uid": stat.ST_UID, "st_dev": stat.ST_DEV}[name]] = field
    return os.stat_result(fields)


def test_a_database_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    database = _plain_database(tmp_path)
    real = dispatch_target_lock._database_stat
    monkeypatch.setattr(dispatch_target_lock, "_database_stat", lambda fd, name: _with(real(fd, name), st_uid=os.geteuid() + 1))
    assert _acquire(database).status == "uncertain"


def test_a_database_mounted_apart_from_its_directory_is_unsupported(tmp_path, monkeypatch):
    """The filesystem check reads the parent, so it can only speak for a file on the same device."""
    database = _plain_database(tmp_path)
    real = dispatch_target_lock._database_stat
    monkeypatch.setattr(dispatch_target_lock, "_database_stat", lambda fd, name: _with(real(fd, name), st_dev=real(fd, name).st_dev + 1))
    assert _acquire(database).status == "unsupported"


def test_a_network_filesystem_is_unsupported(tmp_path, monkeypatch):
    database = _plain_database(tmp_path)
    checked = []
    monkeypatch.setattr(dispatch_target_lock, "_local_filesystem", lambda fd: checked.append(stat.S_ISDIR(os.fstat(fd).st_mode)) or False)
    assert _acquire(database).status == "unsupported"
    assert checked == [True], "it must look at the directory: the file is never opened"


def test_the_local_filesystem_check_works_on_a_directory_descriptor(tmp_path):
    descriptor = os.open(_plain_database(tmp_path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert dispatch_target_lock._local_filesystem(descriptor) is True
    finally:
        os.close(descriptor)
