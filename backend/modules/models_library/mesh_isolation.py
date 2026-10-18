"""Untrusted 3MF uploads are parsed only in a bounded child process.

The package is attacker-shaped: zip members expand enormously, components
multiply, XML can be hostile. The API process never parses it. It starts
``mesh_worker`` with the file on stdin, an empty temporary working directory and
a minimal environment (no secrets). The child limits its own address space and
CPU time and, on Linux, dies with its parent. The parent decodes the child's
compact, size-capped framed output in a worker thread and schema-validates it:
a malformed or hostile payload means "no metadata", never a 500. On the deadline
or at cleanup the child's whole process group is killed. The threat model is
resource-exhaustion isolation, not a sandbox (see mesh_worker).

Admission (install-wide, across every uvicorn worker):

* a caller first takes one of MAX_WAITERS wait slots, without blocking and on the
  calling thread (``reserve``); with none free it gets Busy at once (HTTP 429 with
  Retry-After). The deadline is fixed at that moment, by the caller;
* only then does the work move to a dedicated executor sized MAX_WORKERS +
  MAX_WAITERS (never the default executor that other services share), where it
  waits, oldest ticket first, for one of MAX_WORKERS run slots until the deadline.

Slots are lock files under the run directory (ODIN_RUN_DIR, default /data/run).
When file locks are unavailable (no run directory, ENOLCK, ENOTSUP) the pool falls
back to a per-process FIFO semaphore with a logged warning, never an error.
"""
import array
import asyncio
import collections
import errno
import functools
import json
import logging
import math
import os
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("odin.api")

MAX_WORKERS = 2          # install-wide running children
MAX_WAITERS = 8          # install-wide admitted callers (waiting or about to run)
RETRY_AFTER_SECONDS = 10
TIMEOUT_SECONDS = 30
MEMORY_LIMIT_BYTES = 1536 * 2**20
OUTPUT_LIMIT_BYTES = 32 * 2**20
ANALYSIS_LIMIT_BYTES = 16 * 2**20
_MAGIC = b"ODM1"
_BACKEND = Path(__file__).resolve().parents[2]
_POLL_SECONDS = 0.02
# Test hooks the parent forwards; nothing else from its environment reaches the child.
_FORWARDED_TEST_VARIABLES = ("ODIN_MESH_WORKER_TEST_SLEEP", "ODIN_MESH_WORKER_TEST_GRANDCHILD")
_STATUSES = frozenset({"ok", "too_large", "too_many_members", "bad_zip", "unparsed", "no_plate", "plate_json_too_large"})

# The only executor 3MF work runs on: every admitted caller (one wait slot) has a thread.
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS + MAX_WAITERS, thread_name_prefix="odin-3mf")

LAST_WORKER_PID = None
_running = 0
_running_lock = threading.Lock()


class Busy(Exception):
    """Every wait slot is taken, or no run slot freed up before the deadline."""

    def __init__(self, retry_after: int = RETRY_AFTER_SECONDS):
        super().__init__("3MF analysis is busy")
        self.retry_after = retry_after


@dataclass
class Analysis:
    status: str                       # ok | too_large | too_many_members | bad_zip | unparsed | no_plate | failed
    metadata: Optional[dict] = None
    plate_objects: list = field(default_factory=list)
    plate_count: int = 1
    plate1_gcode: bool = False        # nonempty Metadata/plate_1.gcode (what scheduling needs)
    file_meta: Optional[dict] = None
    mesh: Optional[dict] = None
    plates: Optional[list] = None


# --------------------------------------------------------------------------- mesh wire format

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
    """Children this process is running (the install-wide limit is the slot pool)."""
    return _running


# --------------------------------------------------------------------------- slot pool

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


_UNSUPPORTED = {errno.ENOLCK, getattr(errno, "ENOTSUP", -1), getattr(errno, "EOPNOTSUPP", -1)}
_BUSY = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}


class _LocksUnavailable(Exception):
    pass


def _try_lock(path: Path, shared=False):
    import fcntl
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise _LocksUnavailable(error) from None
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return fd
    except OSError as error:
        os.close(fd)
        if error.errno in _BUSY:
            return None
        raise _LocksUnavailable(error) from None


class _LocalPool:
    """Per-process FIFO fallback when file locks cannot be used."""

    def __init__(self):
        self.condition = threading.Condition()
        self.waiting = 0
        self.queue = collections.deque()
        self.running = 0

    def reserve(self):
        with self.condition:
            if self.waiting >= MAX_WAITERS:
                return None
            self.waiting += 1
            ticket = object()
            self.queue.append(ticket)
            return ticket

    def acquire(self, ticket, deadline):
        with self.condition:
            while not (self.queue[0] is ticket and self.running < MAX_WORKERS):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False  # release_wait drops the ticket from the queue
                self.condition.wait(remaining)
            self.queue.popleft()
            self.running += 1
            self.condition.notify_all()
            return True

    def release_wait(self, ticket):
        with self.condition:
            if ticket in self.queue:
                self.queue.remove(ticket)
            self.waiting -= 1
            self.condition.notify_all()

    def release_run(self):
        with self.condition:
            self.running -= 1
            self.condition.notify_all()


_LOCAL = _LocalPool()
_warned = []


def _fallback_warning(reason) -> None:
    if not _warned:
        _warned.append(True)
        log.warning("3MF worker pool: file locks unavailable (%s); limiting per process instead", reason)


@dataclass
class Ticket:
    """An admitted caller: holds one wait slot and its deadline (fixed at admission)."""
    deadline: float
    wait_fd: Optional[int] = None
    index: int = -1
    order: int = 0
    local: object = None
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.local is not None:
            _LOCAL.release_wait(self.local)
        elif self.wait_fd is not None:
            os.close(self.wait_fd)  # closing the descriptor drops the flock


def reserve(timeout: float = TIMEOUT_SECONDS) -> Ticket:
    """Take a wait slot now, without blocking; raises Busy when the queue is full."""
    deadline = time.monotonic() + timeout
    try:
        directory = run_dir()
        for index in range(MAX_WAITERS):
            fd = _try_lock(directory / f"threemf-wait-{index}.lock")
            if fd is not None:
                order = time.time_ns()
                try:
                    os.ftruncate(fd, 0)
                    os.pwrite(fd, str(order).encode(), 0)
                except OSError:
                    os.close(fd)
                    raise
                return Ticket(deadline=deadline, wait_fd=fd, index=index, order=order)
        raise Busy()
    except (_LocksUnavailable, OSError) as error:
        _fallback_warning(error)
    local = _LOCAL.reserve()
    if local is None:
        raise Busy()
    return Ticket(deadline=deadline, local=local)


def _older_waiter_alive(ticket: Ticket, directory: Path) -> bool:
    """True while another live waiter holds an older ticket (rough install-wide FIFO)."""
    for index in range(MAX_WAITERS):
        if index == ticket.index:
            continue
        path = directory / f"threemf-wait-{index}.lock"
        try:
            with open(path, "rb") as handle:
                raw = handle.read(32)
        except OSError:
            continue
        try:
            order = int(raw)
        except ValueError:
            continue
        if order >= ticket.order:
            continue
        probe = _try_lock(path, shared=True)
        if probe is None:
            return True  # held by its waiter: it goes first
        os.close(probe)  # stale (its holder is gone)
    return False


def _acquire_run_slot(ticket: Ticket):
    """Blocking, on the 3MF executor: a run slot (fd, or 'local'), else Busy at the deadline."""
    if ticket.local is not None:
        if not _LOCAL.acquire(ticket.local, ticket.deadline):
            raise Busy()
        return "local"
    directory = run_dir()
    while True:
        if not _older_waiter_alive(ticket, directory):
            for index in range(MAX_WORKERS):
                fd = _try_lock(directory / f"threemf-run-{index}.lock")
                if fd is not None:
                    return fd
        if time.monotonic() >= ticket.deadline:
            raise Busy()
        time.sleep(_POLL_SECONDS)


def _release_run_slot(slot) -> None:
    if slot == "local":
        _LOCAL.release_run()
    elif slot is not None:
        os.close(slot)


# --------------------------------------------------------------------------- the child

def _child_env() -> dict:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": str(_BACKEND), "PYTHONNOUSERSITE": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    for name in _FORWARDED_TEST_VARIABLES:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session: grandchildren too
    except (ProcessLookupError, PermissionError):
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    proc.wait()


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


# --------------------------------------------------------------------------- analysis schema

def _number(value, low=0.0, high=1e12, optional=False):
    if value is None and optional:
        return True
    return (type(value) in (int, float) and math.isfinite(value) and low <= value <= high)


def _integer(value, low=0, high=10**9):
    return type(value) is int and low <= value <= high


def _text(value, limit=1024, optional=False):
    return (value is None and optional) or (type(value) is str and len(value) <= limit)


def _json_tree(value, depth=0, budget=None):
    """Plain JSON data, bounded in depth and size (safety facts)."""
    budget = budget if budget is not None else [20000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 8:
        return False
    if value is None or type(value) in (bool, int, str):
        return type(value) is not str or len(value) <= 4096
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return len(value) <= 1024 and all(_json_tree(v, depth + 1, budget) for v in value)
    if type(value) is dict:
        return (len(value) <= 1024 and all(type(k) is str and len(k) <= 256 for k in value)
                and all(_json_tree(v, depth + 1, budget) for v in value.values()))
    return False


_METADATA_RULES = {
    "filename": lambda v: _text(v, 1024), "project_name": lambda v: _text(v, 1024),
    "print_time_seconds": lambda v: _integer(v, 0, 10**8), "total_weight_grams": lambda v: _number(v, 0, 1e7),
    "layer_count": lambda v: _integer(v, 0, 10**7), "layer_height": lambda v: _number(v, 0, 100),
    "nozzle_diameter": lambda v: _number(v, 0, 100, optional=True), "printer_model": lambda v: _text(v, 256, True),
    "supports_used": lambda v: type(v) is bool, "bed_type": lambda v: _text(v, 256),
    "filaments": lambda v: type(v) is list and len(v) <= 64 and all(_filament(f) for f in v),
    "safety_facts": lambda v: type(v) is dict and _json_tree(v),
    "thumbnail_b64": lambda v: v is None or (type(v) is str and len(v) <= ANALYSIS_LIMIT_BYTES),
}


def _filament(value):
    return (type(value) is dict and set(value) == {"slot", "type", "color", "used_meters", "used_grams"}
            and _integer(value["slot"], 0, 1024) and _text(value["type"], 128) and _text(value["color"], 64)
            and _number(value["used_meters"], 0, 1e7) and _number(value["used_grams"], 0, 1e7))


def _plate_object(value):
    return (type(value) is dict and set(value) == {"name", "is_wipe_tower", "checked"}
            and _text(value["name"], 512) and type(value["is_wipe_tower"]) is bool and type(value["checked"]) is bool)


def _file_meta(value):
    return (type(value) is dict and set(value) == {"bed_x_mm", "bed_y_mm", "compatible_api_types", "safety_facts"}
            and _number(value["bed_x_mm"], 0, 1e5, optional=True) and _number(value["bed_y_mm"], 0, 1e5, optional=True)
            and _text(value["compatible_api_types"], 256, True)
            and type(value["safety_facts"]) is dict and _json_tree(value["safety_facts"]))


def validate_analysis(parsed) -> Analysis:
    """The child's analysis payload, schema-checked; any violation is 'failed' (no metadata)."""
    if type(parsed) is not dict or parsed.get("status") not in _STATUSES:
        return Analysis(status="failed")
    if parsed["status"] != "ok":
        return Analysis(status=parsed["status"]) if set(parsed) == {"status"} else Analysis(status="failed")
    if set(parsed) != {"status", "metadata", "plate_objects", "plate_count", "plate1_gcode", "file_meta"}:
        return Analysis(status="failed")
    if type(parsed["plate1_gcode"]) is not bool:
        return Analysis(status="failed")
    metadata, objects, count, file_meta = (parsed["metadata"], parsed["plate_objects"], parsed["plate_count"],
                                           parsed["file_meta"])
    if (type(metadata) is not dict or set(metadata) != set(_METADATA_RULES)
            or not all(rule(metadata[key]) for key, rule in _METADATA_RULES.items())):
        return Analysis(status="failed")
    if type(objects) is not list or len(objects) > 1024 or not all(_plate_object(o) for o in objects):
        return Analysis(status="failed")
    if not _integer(count, 1, 1024) or not _file_meta(file_meta):
        return Analysis(status="failed")
    return Analysis(status="ok", metadata=metadata, plate_objects=objects, plate_count=count,
                    plate1_gcode=parsed["plate1_gcode"], file_meta=file_meta)


def _plate_summary(value):
    return (type(value) is dict and set(value) == {"plate_index", "sellable_pieces", "filament_grams", "print_time_seconds"}
            and _integer(value["plate_index"], 0, 1023)
            and (value["sellable_pieces"] is None or _integer(value["sellable_pieces"], 0, 10**6))
            and _number(value["filament_grams"], 0, 1e7, optional=True)
            and (value["print_time_seconds"] is None or _integer(value["print_time_seconds"], 0, 10**8)))


def validate_plates(parsed) -> Analysis:
    """The child's plates payload, schema-checked; any violation is 'failed'."""
    if type(parsed) is not dict or parsed.get("status") not in _STATUSES:
        return Analysis(status="failed")
    if parsed["status"] != "ok":
        return Analysis(status=parsed["status"]) if set(parsed) == {"status"} else Analysis(status="failed")
    plates = parsed.get("plates")
    if set(parsed) != {"status", "plates"} or type(plates) is not list or len(plates) > 1024 \
            or not all(_plate_summary(p) for p in plates):
        return Analysis(status="failed")
    return Analysis(status="ok", plates=plates)


def _decode(blob: bytes, mode: str, clean_exit: bool) -> Analysis:
    try:
        frames = _frames(blob)
        if mode == "plates":
            if not clean_exit or b"P" not in frames or len(frames[b"P"]) > ANALYSIS_LIMIT_BYTES:
                return Analysis(status="failed")
            return validate_plates(json.loads(frames[b"P"]))
        if mode == "mesh":
            analysis = Analysis(status="ok")
        elif b"A" in frames and len(frames[b"A"]) <= ANALYSIS_LIMIT_BYTES:
            analysis = validate_analysis(json.loads(frames[b"A"]))
        else:
            analysis = Analysis(status="failed")
        # A mesh counts only from a child that finished cleanly within the deadline.
        if clean_exit and b"M" in frames and analysis.status == "ok":
            analysis.mesh = decode_mesh(frames[b"M"])
        return analysis
    except Exception:  # includes RecursionError: a hostile child never fails the parent
        return Analysis(status="failed")


# --------------------------------------------------------------------------- entry points

def run_admitted(path, ticket: Ticket, *, mode: str = "upload", name: Optional[str] = None,
                 memory_limit: int = MEMORY_LIMIT_BYTES) -> Analysis:
    """Blocking, on the 3MF executor, for an admitted caller. Raises Busy; else never raises."""
    global LAST_WORKER_PID, _running
    try:
        slot = _acquire_run_slot(ticket)
    finally:
        ticket.release()  # the wait slot is only for waiting
    workdir = None
    blob, code = b"", None
    try:
        cpu_seconds = max(1, math.ceil(ticket.deadline - time.monotonic()))
        workdir = tempfile.mkdtemp(prefix="odin-3mf-")
        with open(path, "rb") as package:
            proc = subprocess.Popen(
                [sys.executable, "-s", "-m", "modules.models_library.mesh_worker", str(memory_limit), str(cpu_seconds),
                 mode, str(os.getpid()), os.path.basename(name or str(path))],
                cwd=workdir, env=_child_env(), stdin=package, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        LAST_WORKER_PID = proc.pid
        with _running_lock:
            _running += 1
        try:
            try:
                blob = _read_frames(proc, ticket.deadline)
            except ValueError:
                blob = b""
            try:
                code = proc.wait(max(0.0, ticket.deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                code = None
        finally:
            _kill_group(proc)
            proc.stdout.close()
            with _running_lock:
                _running -= 1
    except OSError:
        return Analysis(status="failed")
    finally:
        _release_run_slot(slot)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return _decode(blob, mode, code == 0)


def analyze_blocking(path, *, mode: str = "upload", timeout: float = TIMEOUT_SECONDS, ticket: Optional[Ticket] = None,
                     name: Optional[str] = None, memory_limit: int = MEMORY_LIMIT_BYTES) -> Analysis:
    """Blocking entry for code already on the 3MF executor (or tests). Raises Busy."""
    return run_admitted(path, ticket or reserve(timeout), mode=mode, name=name, memory_limit=memory_limit)


async def run_on_executor(function, *args, **kwargs):
    """Run blocking 3MF work (and callers that hold a Ticket) on the dedicated executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(EXECUTOR, functools.partial(function, *args, **kwargs))


async def analyze_3mf_isolated(path, *, mode: str = "upload", timeout: float = TIMEOUT_SECONDS,
                               name: Optional[str] = None, memory_limit: int = MEMORY_LIMIT_BYTES) -> Analysis:
    """Admission (and the deadline) here, at the call site; raises Busy at once when full."""
    ticket = reserve(timeout)
    try:
        return await run_on_executor(run_admitted, path, ticket, mode=mode, name=name, memory_limit=memory_limit)
    finally:
        ticket.release()


async def extract_mesh_isolated(path, *, timeout: float = TIMEOUT_SECONDS, memory_limit: int = MEMORY_LIMIT_BYTES):
    """The preview mesh as plain data, or None (refused, too slow, too big, crashed, busy)."""
    try:
        return (await analyze_3mf_isolated(path, mode="mesh", timeout=timeout, memory_limit=memory_limit)).mesh
    except Busy:
        return None


def plate_summaries_blocking(path, *, timeout: float = TIMEOUT_SECONDS) -> Analysis:
    """Per-plate summaries of a stored package, parsed in the bounded child (same pool,
    admission and deadline as uploads). Blocking: for sync routes. Raises Busy."""
    return analyze_blocking(path, mode="plates", timeout=timeout)
