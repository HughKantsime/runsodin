"""Child process for 3MF preview extraction: ``python -m modules.models_library.mesh_worker <path> <bytes>``.

Caps its own address space, extracts the preview mesh and writes it to stdout
in the compact binary form read by mesh_isolation. Exit 0 with no output means
"no preview".
"""
import os
import sys


def _limit_memory(limit: int) -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass  # not enforced on this platform (macOS); the parent's kill-on-timeout still bounds it


def main() -> int:
    path, limit = sys.argv[1], int(sys.argv[2])
    _limit_memory(limit)
    delay = os.environ.get("ODIN_MESH_WORKER_TEST_SLEEP")
    if delay:  # test hook: a worker that is too slow
        import time
        time.sleep(float(delay))
    from modules.models_library.threemf_parser import extract_mesh_from_3mf
    from modules.models_library.mesh_isolation import encode_mesh
    try:
        mesh = extract_mesh_from_3mf(path)
    except MemoryError:
        return 3
    if mesh:
        sys.stdout.buffer.write(encode_mesh(mesh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
