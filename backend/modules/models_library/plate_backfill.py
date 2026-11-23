"""One install-wide slice-facts pass. A process exit releases the advisory lock."""
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from threading import Thread

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit import log_audit
from . import plate_cache
from .library_search import _NOT_EDUCATION
from .mesh_isolation import Busy

_IDLE = dict(state="idle", total=0, done=0, not_available=0, started_at=None)
_CANDIDATES = f"FROM print_files f WHERE f.blob_state='present' AND {_NOT_EDUCATION.format(a='f')}"


def _lock():
    root = plate_cache.cache_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise OSError("Plate cache root must be a real directory")
    handle = os.fdopen(os.open(root / "backfill.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "rb+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _save(state):
    plate_cache._atomic_write(plate_cache.cache_root(), "backfill.json", json.dumps(state).encode())


def status():
    raw = plate_cache._read_regular(plate_cache.cache_root() / "backfill.json", 1024)
    state = json.loads(raw) if raw else dict(_IDLE)
    if state["state"] == "running":
        # The writer owns an exclusive lock until it publishes the final state.
        # A shared probe succeeds only after that writer exits.
        with (plate_cache.cache_root() / "backfill.lock").open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return state
            latest = plate_cache._read_regular(plate_cache.cache_root() / "backfill.json", 1024)
            state = json.loads(latest) if latest else dict(_IDLE)
            if state["state"] == "running":
                state["state"] = "interrupted"
    return state


def _run(engine, ids, state, lock):
    try:
        for identifier in ids:
            while True:
                with Session(engine) as db:
                    row = db.execute(text("SELECT f.file_hash,f.stored_path,f.blob_state " + _CANDIDATES +
                                          " AND f.id=:id"), {"id": identifier}).first()
                try:
                    if row and plate_cache.facts_result(row)[1]:
                        state["not_available"] += 1
                    break
                except Busy as busy:
                    time.sleep(max(1, busy.retry_after))
            state["done"] += 1
            _save(state)
        state["state"] = "complete"
    except Exception:
        state["state"] = "failed"
    finally:
        try:
            _save(state)
        finally:
            lock.close()


def start(db, user):
    lock = _lock()
    if lock is None:
        raise HTTPException(409, "A backfill is already running.")
    try:
        ids = [r.id for r in db.execute(text("SELECT f.id,f.file_hash " + _CANDIDATES + " ORDER BY f.id"))
               if not plate_cache.has_cached_result(r.file_hash)]
        state = dict(_IDLE, state="running", total=len(ids), started_at=datetime.now(timezone.utc).isoformat())
        log_audit(db, "print_file.plates_backfill_started", "print_file", details={"user_id": user["id"], "count": len(ids)})
        db.commit()
        _save(state)
        Thread(target=_run, args=(db.get_bind(), ids, state.copy(), lock), daemon=True).start()
        return state
    except BaseException:
        lock.close()
        raise
