"""Child process for untrusted 3MF uploads: ``python -m modules.models_library.mesh_worker <path> <bytes> <cpu_seconds> <mode>``.

Every parse of an uploaded 3MF (zip directory, XML, JSON, preview mesh) runs
here, never in the API process. The child limits itself first (address space,
CPU seconds, and on Linux dies with its parent), then writes framed results to
stdout for mesh_isolation:

* ``A`` frame: analysis JSON (status, metadata, plate objects, plate count, file meta),
  flushed before the mesh is attempted, so a mesh that blows a limit still leaves
  the metadata;
* ``M`` frame (mode ``upload``/``mesh`` only): the compact preview mesh.

Mode ``mesh`` writes only the mesh frame; mode ``metadata`` skips the mesh.
"""
import json
import os
import struct
import sys


def _limit(memory: int, cpu_seconds: int) -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    except (ImportError, ValueError, OSError):
        pass  # not enforced on this platform (macOS); the parent's deadline still bounds it
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (ImportError, ValueError, OSError):
        pass
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            import signal
            PR_SET_PDEATHSIG = 1
            ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
            if os.getppid() == 1:  # the parent already died before prctl took effect
                os._exit(4)
        except (OSError, AttributeError):
            pass


def _frame(tag: bytes, payload: bytes) -> None:
    sys.stdout.buffer.write(tag + struct.pack("<I", len(payload)) + payload)
    sys.stdout.buffer.flush()


MAX_UNCOMPRESSED = 500 * 1024 * 1024


def _analysis(path: str) -> dict:
    import dataclasses
    import re
    import zipfile
    from modules.models_library.threemf_parser import parse_3mf, extract_objects_from_plate
    from modules.models_library.print_file_meta import extract_print_file_meta
    try:
        with zipfile.ZipFile(path, "r") as package:
            if sum(entry.file_size for entry in package.infolist()) > MAX_UNCOMPRESSED:
                return {"status": "too_large"}
            names = package.namelist()
            plate_objects = extract_objects_from_plate(package)
    except zipfile.BadZipFile:
        return {"status": "bad_zip"}
    metadata = parse_3mf(path)
    if not metadata:
        return {"status": "unparsed"}
    plate_count = len({name for name in names if re.fullmatch(r"Metadata/plate_[1-9][0-9]*\.gcode", name)}) or 1
    return {"status": "ok", "metadata": dataclasses.asdict(metadata), "plate_objects": plate_objects,
            "plate_count": plate_count, "file_meta": extract_print_file_meta(path, ".3mf")}


def _mesh_frame(path: str) -> None:
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    from modules.models_library.mesh_isolation import encode_mesh
    mesh = extract_mesh_from_3mf(path)
    if not mesh:
        return
    try:
        blob = encode_mesh(mesh)
    except (OverflowError, ValueError):
        return  # a coordinate beyond float32: no preview
    _frame(b"M", blob)


def main() -> int:
    path, memory, cpu_seconds, mode = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    _limit(memory, cpu_seconds)
    delay = os.environ.get("ODIN_MESH_WORKER_TEST_SLEEP")
    if delay:  # test hook: a worker that is too slow
        import time
        time.sleep(float(delay))
    try:
        if mode != "mesh":
            result = _analysis(path)
            _frame(b"A", json.dumps(result, default=str).encode())
            if result["status"] != "ok" or mode == "metadata":
                return 0
        _mesh_frame(path)
    except MemoryError:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
