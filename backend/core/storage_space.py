"""Measure the volume that will contain a storage directory before it exists."""
from pathlib import Path


def existing_storage_directory(path):
    probe = Path(path).resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        raise OSError("Storage filesystem probe is not a directory")
    return probe
