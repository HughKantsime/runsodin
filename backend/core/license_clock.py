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
import math

log = logging.getLogger(__name__)
KEY = "license_time_high_water"
FILENAME = ".odin-license-time.json"
SKEW = timedelta(hours=24)
INTERVAL = 300
RECOVERY_WINDOW = 5


def elapsed_time():
    """Elapsed host time including suspend, without consulting the wall clock."""
    if hasattr(time, 'CLOCK_BOOTTIME'):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    if os.uname().sysname == 'Darwin':
        return _darwin_elapsed()
    return time.monotonic()


def _darwin_elapsed():
    import ctypes
    class Timebase(ctypes.Structure):
        _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
    lib = ctypes.CDLL(None)
    lib.mach_continuous_time.restype = ctypes.c_uint64
    scale = Timebase()
    lib.mach_timebase_info(ctypes.byref(scale))
    return lib.mach_continuous_time() * scale.numer / scale.denom / 1e9



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
        self.conflicting_checkpoints = bool(values and max(values) - min(values) > SKEW)
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
        self.monotonic = monotonic or elapsed_time
        self.recovery_sample = None
        self.high_water = store.read()
        self.anchor = None
        self.tick = None
        self.checkpoint = None
        self.lock = threading.RLock()

    def sample(self, *, persist=False, issued_at=""):
        """Detect running-process drift; downtime is not evidence of tampering."""
        with self.lock:
            wall, tick = self.wall(), self.monotonic()
            reference = self.anchor or self.high_water or wall
            self.reason_code = None
            try:
                if not math.isfinite(tick) or (self.tick is not None and tick < self.tick):
                    raise ValueError("nonfinite_or_reversed_monotonic")
                if self.anchor is not None:
                    reference = self.anchor + timedelta(seconds=tick - self.tick)
                due = self.checkpoint is None or tick - self.checkpoint >= INTERVAL
                restored = self.store.read() if persist and due else self.high_water
                # A stale DB/file copy is normal after restore. Only a checkpoint
                # ahead of the current wall clock is suspect.
                if restored is not None:
                    reference = max(reference, restored)
                    if restored - wall > SKEW:
                        self.reason_code = "future_checkpoint"
                if self.anchor is not None and wall - reference > SKEW:
                    self.reason_code = "forward_jump"
                elif reference - wall > SKEW:
                    self.reason_code = self.reason_code or "rollback"
                issued = instant(issued_at) if issued_at not in ("", None) else None
                if issued is not None and issued - wall > SKEW:
                    self.reason_code = "before_issue_time"
                if self.reason_code == "forward_jump":
                    previous = self.recovery_sample
                    if previous is not None:
                        old_wall, old_tick = previous
                        elapsed = tick - old_tick
                        agrees = abs((wall - old_wall).total_seconds() - elapsed) <= 2
                        if agrees and elapsed >= RECOVERY_WINDOW:
                            self.reason_code = None
                        elif not agrees:
                            self.recovery_sample = (wall, tick)
                    else:
                        self.recovery_sample = (wall, tick)
                else:
                    self.recovery_sample = None
                if self.reason_code:
                    return reference, True
                now = max(reference, wall)
            except (ValueError, OverflowError, TypeError):
                self.reason_code = "invalid_clock_arithmetic"
                return reference, True
            self.recovery_sample = None
            self.anchor, self.tick, self.high_water = now, tick, now
            if persist and due:
                self.store.write(now)
                self.checkpoint = tick
            return now, False

    def error(self, issued_at="", *, sample=None):
        now, uncertain = sample or self.sample(issued_at=issued_at)
        issued = instant(issued_at) if issued_at not in ("", None) else None
        if uncertain or (issued is not None and issued - self.wall() > SKEW):
            return ("License clock differs by more than 24 hours from trusted elapsed time, "
                    "a checkpoint or signed issue time, or has invalid arithmetic. "
                    "Correct the server clock. Local password login remains available; "
                    "running-process forward jumps recover after five seconds of stable elapsed time.")
        return None
