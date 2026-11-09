"""Offline rollback detection, not a secure time authority.

Only host time and process elapsed time are available offline. Keep two durable
copies so a database-only restore does not rewind the clock. Storage failures
degrade protection, never availability. See docs/LICENSE_ENFORCEMENT.md.
"""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time

log = logging.getLogger(__name__)
KEY = "license_time_high_water"
FILENAME = ".odin-license-time.json"
SKEW = timedelta(hours=24)
INTERVAL = 300


def instant(value):
    if not isinstance(value, str):
        raise ValueError("Invalid clock timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    try:
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError("Invalid clock timestamp") from exc


def stored_instant(value):
    # SQLite returns JSON text; PostgreSQL drivers decode the JSON scalar.
    if isinstance(value, str) and value.startswith('"'):
        value = json.loads(value)
    return instant(value)


class TimeStore:
    def __init__(self, directory, engine_reader):
        self.path = Path(directory) / FILENAME
        self.engine_reader = engine_reader

    def _file_read(self):
        return instant(json.loads(self.path.read_text())["high_water"])

    def read(self):
        values = []
        try:
            values.append(self._file_read())
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            from sqlalchemy import text
            with self.engine_reader().connect() as connection:
                value = connection.execute(text("SELECT value FROM system_config WHERE key=:key"), {"key": KEY}).scalar()
                if value is not None:
                    values.append(stored_instant(value))
        except Exception:
            # Schema may not exist during first-run setup, or DB may be offline.
            log.debug("License clock database copy unavailable")
        return max(values) if values else None

    def write(self, value):
        # Serialize writers from separate API/daemon processes and merge, never
        # overwrite a newer observation with an older process's checkpoint.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(self.path) + ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    value = max(value, self._file_read())
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".license-time-")
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump({"high_water": value.isoformat()}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                    directory_fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        except OSError:
            log.warning("License clock file checkpoint unavailable; continuing with remaining clock evidence")

        try:
            from sqlalchemy import text
            with self.engine_reader().connect() as connection:
                sqlite = connection.dialect.name == "sqlite"
                timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar() if sqlite else None
                try:
                    # Entitlement checks can run inside an existing writer's
                    # transaction. Do not wait for that same request to commit.
                    if sqlite:
                        connection.exec_driver_sql("PRAGMA busy_timeout=0")
                    else:
                        connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    connection.execute(text("INSERT INTO system_config (key,value) VALUES (:key,:value) "
                        "ON CONFLICT(key) DO NOTHING"), {"key": KEY, "value": json.dumps(value.isoformat())})
                    connection.execute(text("UPDATE system_config SET value=value WHERE key=:key"), {"key": KEY})
                    stored = connection.execute(text("SELECT value FROM system_config WHERE key=:key"), {"key": KEY}).scalar()
                    try:
                        value = max(value, stored_instant(stored))
                    except (ValueError, TypeError):
                        pass
                    connection.execute(text("UPDATE system_config SET value=:value WHERE key=:key"),
                                       {"key": KEY, "value": json.dumps(value.isoformat())})
                    connection.commit()
                finally:
                    connection.rollback()
                    if sqlite:
                        connection.exec_driver_sql(f"PRAGMA busy_timeout={int(timeout)}")
        except Exception:
            log.warning("License clock database checkpoint unavailable; continuing with remaining clock evidence")


class LicenseClock:
    def __init__(self, store, wall=None, monotonic=None):
        self.store = store
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.high_water = store.read()
        self.anchor = None
        self.tick = None
        self.checkpoint = None
        self.lock = threading.RLock()

    def sample(self, *, persist=False):
        with self.lock:
            wall, tick = self.wall(), self.monotonic()
            elapsed_now = wall
            if self.anchor:
                elapsed = timedelta(seconds=max(0, tick - self.tick))
                # Even a corrupt but parseable year-9999 checkpoint must produce
                # a recoverable clock refusal, never an arithmetic crash.
                room = datetime.max.replace(tzinfo=timezone.utc) - self.anchor
                elapsed_now = self.anchor + min(elapsed, room)
            due = self.checkpoint is None or tick - self.checkpoint >= INTERVAL
            if persist and due:
                restored = self.store.read()
                if restored is not None:
                    self.high_water = max(self.high_water or restored, restored)
            now = max(wall, elapsed_now, self.high_water or wall)
            rollback = now - wall > SKEW
            self.anchor, self.tick, self.high_water = now, tick, now
            if persist and due:
                self.store.write(now)
                self.checkpoint = tick
            return now, rollback

    def error(self, issued_at="", *, sample=None):
        now, rollback = sample or self.sample()
        issued = instant(issued_at) if issued_at not in ("", None) else None
        if rollback or (issued is not None and issued - self.wall() > SKEW):
            return ("License clock is more than 24 hours behind its recorded time or signed issue time. "
                    "Correct the server clock. Local password login remains available; "
                    "see the license clock recovery instructions if the clock was advanced accidentally.")
        return None
