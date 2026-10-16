"""Child process for untrusted 3MF uploads.

``python -m modules.models_library.mesh_worker <memory> <cpu_seconds> <mode> <parent_pid> <name>``
with the package open read-only on stdin (fd 0). The child gets no path to the
upload, starts in an empty temporary directory, has a minimal environment (no
secrets), limits its own address space and CPU time and, on Linux, dies with its
parent. Every parse of an uploaded 3MF (zip directory, XML, JSON, preview mesh)
runs here, never in the API process.

Threat model: resource-exhaustion isolation (memory, CPU, time, output size) for
hostile packages. The child runs as the same uid as the API (the image runs ODIN
as one non-root user, so it cannot switch uid), so this is not a sandbox against
code execution in the parser stack (zipfile, defusedxml, json), which has no known
code-execution sinks for this input.

Output frames on stdout for mesh_isolation:

* ``A``: analysis JSON (status, metadata, plate objects, plate count, file meta),
  flushed before the mesh is attempted, so a mesh that hits a limit keeps the metadata;
* ``M`` (modes ``upload`` and ``mesh``): the compact preview mesh.

Modes: ``upload`` (analysis + mesh), ``metadata`` (analysis only), ``education``
(analysis + the Education rule: exactly one nonempty Metadata/plate_1.gcode),
``mesh`` (mesh only).
"""
import json
import os
import struct
import sys

INPUT = "/dev/fd/0"  # the package, passed as a descriptor


def _limit(memory: int, cpu_seconds: int, parent_pid: int = 0) -> None:
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
        except (OSError, AttributeError):
            pass
    # The parent may have died before the death signal was armed: then we were
    # re-parented and must not run on.
    if parent_pid and os.getppid() != parent_pid:
        os._exit(4)


def _frame(tag: bytes, payload: bytes) -> None:
    sys.stdout.buffer.write(tag + struct.pack("<I", len(payload)) + payload)
    sys.stdout.buffer.flush()


def _analysis(path: str, name: str, mode: str) -> dict:
    import dataclasses
    import re
    import zipfile
    from core.zip_bounds import ArchiveRefused, check_expansion, open_bounded
    from modules.models_library.threemf_parser import parse_3mf, extract_objects_from_plate
    from modules.models_library.print_file_meta import extract_print_file_meta
    try:
        with open_bounded(path) as package:
            check_expansion(package)
            entries = package.infolist()
            names = [entry.filename for entry in entries]
            if mode == "education":
                printable = [entry for entry in entries if entry.filename == "Metadata/plate_1.gcode"]
                if len(printable) != 1 or printable[0].is_dir() or printable[0].file_size <= 0:
                    return {"status": "no_plate"}
            plate_objects = extract_objects_from_plate(package)
    except ArchiveRefused as refused:
        return {"status": "too_large" if refused.reason in ("too_large", "member_too_large") else "too_many_members"}
    except zipfile.BadZipFile:
        return {"status": "bad_zip"}
    metadata = parse_3mf(path, name)
    if not metadata:
        return {"status": "unparsed"}
    plate_count = len({n for n in names if re.fullmatch(r"Metadata/plate_[1-9][0-9]*\.gcode", n)}) or 1
    return {"status": "ok", "metadata": dataclasses.asdict(metadata), "plate_objects": plate_objects,
            "plate_count": plate_count, "file_meta": extract_print_file_meta(path, ".3mf")}


def _mesh_frame(path: str) -> None:
    from core.zip_bounds import open_bounded, check_expansion
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    from modules.models_library.mesh_isolation import encode_mesh
    try:
        with open_bounded(path) as package:
            check_expansion(package)
    except Exception:
        return
    mesh = extract_mesh_from_3mf(path)
    if not mesh:
        return
    try:
        blob = encode_mesh(mesh)
    except (OverflowError, ValueError):
        return  # a coordinate beyond float32: no preview
    _frame(b"M", blob)


def main() -> int:
    memory, cpu_seconds, mode, parent_pid, name = (int(sys.argv[1]), int(sys.argv[2]), sys.argv[3],
                                                   int(sys.argv[4]), sys.argv[5])
    _limit(memory, cpu_seconds, parent_pid)
    delay = os.environ.get("ODIN_MESH_WORKER_TEST_SLEEP")
    if delay:  # test hook: a worker that is too slow
        import time
        time.sleep(float(delay))
    spawn = os.environ.get("ODIN_MESH_WORKER_TEST_GRANDCHILD")
    if spawn:  # test hook: a grandchild that must die with the worker's process group
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
        with open(spawn, "w") as marker:
            marker.write(str(child.pid))
    try:
        if mode != "mesh":
            result = _analysis(INPUT, name, mode)
            _frame(b"A", json.dumps(result, default=str).encode())
            if result["status"] != "ok" or mode in ("metadata", "education"):
                return 0
        _mesh_frame(INPUT)
    except MemoryError:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
