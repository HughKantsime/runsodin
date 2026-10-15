"""3MF preview extraction in a bounded child process.

The mesh is attacker-shaped (zip members expand enormously, components
multiply). Extraction runs in at most MAX_WORKERS child processes per event
loop, each with an address-space limit where the OS enforces one, killed on
timeout; the parent only decodes a compact, size-capped result.
"""
import array
import asyncio
import os
import struct
import sys
from pathlib import Path

MAX_WORKERS = 2
TIMEOUT_SECONDS = 30
MEMORY_LIMIT_BYTES = 1536 * 2**20
OUTPUT_LIMIT_BYTES = 32 * 2**20
_MAGIC = b"ODM1"
_BACKEND = Path(__file__).resolve().parents[2]

LAST_WORKER_PID = None
_running = 0
_slots = {}


def encode_mesh(mesh: dict) -> bytes:
    """vertices as float32, triangles as uint32, behind a small header."""
    verts = array.array("f", mesh["vertices"])
    tris = array.array("I", mesh["triangles"])
    return _MAGIC + struct.pack("<II", len(verts), len(tris)) + verts.tobytes() + tris.tobytes()


def decode_mesh(blob: bytes):
    if len(blob) < 12 or blob[:4] != _MAGIC:
        return None
    nv, nt = struct.unpack("<II", blob[4:12])
    if len(blob) != 12 + 4 * nv + 4 * nt or nv % 3 or nt % 3:
        return None
    verts = array.array("f")
    verts.frombytes(blob[12:12 + 4 * nv])
    tris = array.array("I")
    tris.frombytes(blob[12 + 4 * nv:])
    if any(i >= nv // 3 for i in tris):
        return None
    return {"vertices": [round(v, 3) for v in verts], "triangles": tris.tolist(),
            "vertex_count": nv // 3, "triangle_count": nt // 3}


def running_workers() -> int:
    return _running


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots.clear()  # one live loop per process in production
        _slots[loop] = asyncio.Semaphore(MAX_WORKERS)
    return _slots[loop]


async def _read_capped(stream, limit):
    chunks, size = [], 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise ValueError("mesh output too large")
        chunks.append(chunk)


async def extract_mesh_isolated(path, *, timeout: float = TIMEOUT_SECONDS, memory_limit: int = MEMORY_LIMIT_BYTES):
    """The preview mesh as plain data, or None (refused, too slow, too big, crashed)."""
    global LAST_WORKER_PID, _running
    async with _slot():
        env = dict(os.environ, PYTHONPATH=str(_BACKEND) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "modules.models_library.mesh_worker", str(path), str(memory_limit),
            cwd=str(_BACKEND), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        LAST_WORKER_PID = proc.pid
        _running += 1
        try:
            blob = await asyncio.wait_for(_read_capped(proc.stdout, OUTPUT_LIMIT_BYTES), timeout)
            code = await asyncio.wait_for(proc.wait(), timeout)
        except (asyncio.TimeoutError, ValueError):
            blob, code = b"", -1
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            _running -= 1
    if code != 0 or not blob:
        return None
    return decode_mesh(blob)
