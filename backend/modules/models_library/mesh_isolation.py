"""Untrusted 3MF uploads are parsed only in a bounded child process.

The package is attacker-shaped: zip members expand enormously, components
multiply, XML can be hostile. The API process never opens it. It starts
``mesh_worker`` with a minimal environment (no secrets), which limits its own
address space and CPU time and, on Linux, dies with its parent, and then
decodes the child's compact, size-capped framed output in a worker thread.

Concurrency is install-wide, across every uvicorn worker: MAX_WORKERS run slots
and MAX_WAITERS wait slots are lock files under the run directory (ODIN_RUN_DIR,
default /data/run). With no free wait slot the caller gets Busy (HTTP 429 with
Retry-After). One overall deadline covers waiting, running and reading.
"""
import array
import asyncio
import errno
import json
import math
import os
import selectors
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MAX_WORKERS = 2          # install-wide running children
MAX_WAITERS = 8          # install-wide callers waiting for a run slot
RETRY_AFTER_SECONDS = 10
TIMEOUT_SECONDS = 30
MEMORY_LIMIT_BYTES = 1536 * 2**20
OUTPUT_LIMIT_BYTES = 32 * 2**20
ANALYSIS_LIMIT_BYTES = 16 * 2**20
_MAGIC = b"ODM1"
_BACKEND = Path(__file__).resolve().parents[2]
_POLL_SECONDS = 0.05
# Test hooks the parent forwards; nothing else from its environment reaches the child.
_FORWARDED_TEST_VARIABLES = ("ODIN_MESH_WORKER_TEST_SLEEP",)

LAST_WORKER_PID = None
_running = 0
import threading as _threading
_running_lock = _threading.Lock()


class Busy(Exception):
    """Every wait slot is taken, or no run slot freed up before the deadline."""

    def __init__(self, retry_after: int = RETRY_AFTER_SECONDS):
        super().__init__("3MF analysis is busy")
        self.retry_after = retry_after


@dataclass
class Analysis:
    status: str                       # ok | too_large | bad_zip | unparsed | failed
    metadata: Optional[dict] = None
    plate_objects: list = field(default_factory=list)
    plate_count: int = 1
    file_meta: Optional[dict] = None
    mesh: Optional[dict] = None


def encode_mesh(mesh: dict) -> bytes:
    """vertices as float32, triangles as uint32, behind a small header.
    Raises OverflowError/ValueError for coordinates float32 cannot hold."""
    verts = array.array("f", mesh["vertices"])
    if not all(math.isfinite(v) for v in verts):
        raise ValueError("non-finite vertex")
    tris = array.array("I", mesh["triangles"])
    return _MAGIC + struct.pack("<II", len(verts), len(tris)) + verts.tobytes() + tris.tobytes()


def decode_mesh(blob: bytes):
    if len(blob) < 12 or blob[:4] != _MAGIC or len(blob) > OUTPUT_LIMIT_BYTES:
        return None
    nv, nt = struct.unpack("<II", blob[4:12])
    if len(blob) != 12 + 4 * nv + 4 * nt or nv % 3 or nt % 3:
        return None
    verts = array.array("f")
    verts.frombytes(blob[12:12 + 4 * nv])
    if not all(math.isfinite(v) for v in verts):
        return None  # non-finite geometry: no preview, never a 500
    tris = array.array("I")
    tris.frombytes(blob[12 + 4 * nv:])
    if any(i >= nv // 3 for i in tris):
        return None
    return {"vertices": [round(v, 3) for v in verts], "triangles": tris.tolist(),
            "vertex_count": nv // 3, "triangle_count": nt // 3}


def running_workers() -> int:
    """Children this process is running (the install-wide limit is the lock files)."""
    return _running


def run_dir() -> Path:
    configured = os.environ.get("ODIN_RUN_DIR")
    if configured:
        base = Path(configured)
    elif os.path.isdir("/data") and os.access("/data", os.W_OK):
        base = Path("/data/run")
    else:
        base = Path(tempfile.gettempdir()) / f"odin-run-{os.getuid()}"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base


def _try_lock(path: Path):
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as error:
        os.close(fd)
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise


def _first_free(prefix: str, count: int):
    directory = run_dir()
    for index in range(count):
        fd = _try_lock(directory / f"{prefix}-{index}.lock")
        if fd is not None:
            return fd
    return None


def _release(fd) -> None:
    if fd is not None:
        os.close(fd)  # closing the descriptor drops the flock


def _acquire_run_slot(deadline: float):
    waiting = _first_free("threemf-wait", MAX_WAITERS)
    if waiting is None:
        raise Busy()
    try:
        while True:
            slot = _first_free("threemf-run", MAX_WORKERS)
            if slot is not None:
                return slot
            if time.monotonic() >= deadline:
                raise Busy()
            time.sleep(_POLL_SECONDS)
    finally:
        _release(waiting)


def _child_env() -> dict:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": str(_BACKEND), "PYTHONNOUSERSITE": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    for name in _FORWARDED_TEST_VARIABLES:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _read_frames(proc, deadline: float) -> bytes:
    """Everything the child writes before EOF or the deadline, capped."""
    buffer = bytearray()
    selector = selectors.DefaultSelector()
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    selector.register(fd, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return bytes(buffer)
            if not selector.select(min(remaining, 0.5)):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return bytes(buffer)
            buffer += chunk
            if len(buffer) > OUTPUT_LIMIT_BYTES:
                raise ValueError("3MF worker output too large")
    finally:
        selector.close()


def _frames(blob: bytes) -> dict:
    found, offset = {}, 0
    while offset + 5 <= len(blob):
        tag = blob[offset:offset + 1]
        (size,) = struct.unpack("<I", blob[offset + 1:offset + 5])
        end = offset + 5 + size
        if end > len(blob):
            break  # truncated by the deadline or a crash
        found.setdefault(tag, blob[offset + 5:end])
        offset = end
    return found


def analyze_blocking(path, *, mode: str = "upload", timeout: float = TIMEOUT_SECONDS,
                     memory_limit: int = MEMORY_LIMIT_BYTES) -> Analysis:
    """Blocking: call from a worker thread. Raises Busy; otherwise never raises."""
    global LAST_WORKER_PID, _running
    deadline = time.monotonic() + timeout
    slot = _acquire_run_slot(deadline)
    try:
        cpu_seconds = max(1, math.ceil(deadline - time.monotonic()))
        proc = subprocess.Popen(
            [sys.executable, "-s", "-m", "modules.models_library.mesh_worker", os.path.abspath(path), str(memory_limit),
             str(cpu_seconds), mode],
            cwd=str(_BACKEND), env=_child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        LAST_WORKER_PID = proc.pid
        with _running_lock:
            _running += 1
        try:
            try:
                blob = _read_frames(proc, deadline)
            except ValueError:
                blob = b""
            try:
                code = proc.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                code = None
        finally:
            if proc.returncode is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            with _running_lock:
                _running -= 1
    finally:
        _release(slot)
    frames = _frames(blob)
    analysis = Analysis(status="failed")
    if mode == "mesh":
        analysis.status = "ok"
    elif b"A" in frames and len(frames[b"A"]) <= ANALYSIS_LIMIT_BYTES:
        try:
            parsed = json.loads(frames[b"A"])
            analysis = Analysis(status=parsed["status"], metadata=parsed.get("metadata"),
                                plate_objects=parsed.get("plate_objects") or [], plate_count=parsed.get("plate_count") or 1,
                                file_meta=parsed.get("file_meta"))
        except (ValueError, KeyError, TypeError):
            analysis = Analysis(status="failed")
    # A mesh counts only from a child that finished cleanly within the deadline.
    if code == 0 and b"M" in frames:
        analysis.mesh = decode_mesh(frames[b"M"])
    return analysis


async def analyze_3mf_isolated(path, *, mode: str = "upload", timeout: float = TIMEOUT_SECONDS,
                               memory_limit: int = MEMORY_LIMIT_BYTES) -> Analysis:
    """The child runs, and its output is decoded, in a worker thread; raises Busy."""
    return await asyncio.to_thread(analyze_blocking, path, mode=mode, timeout=timeout, memory_limit=memory_limit)


async def extract_mesh_isolated(path, *, timeout: float = TIMEOUT_SECONDS, memory_limit: int = MEMORY_LIMIT_BYTES):
    """The preview mesh as plain data, or None (refused, too slow, too big, crashed, busy)."""
    try:
        return (await analyze_3mf_isolated(path, mode="mesh", timeout=timeout, memory_limit=memory_limit)).mesh
    except Busy:
        return None
