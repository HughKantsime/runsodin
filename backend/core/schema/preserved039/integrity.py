"""Closed 039 restore sources: verified 038 base plus pinned 039 schema sources.

Materialize an isolated private bundle; never import current runtime sources.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

MANIFEST_SHA256 = 'e9874ae0eb176fbdaaeef92fde29d477799fbab9670ee50e7601bc609fd6632e'
_MATERIALIZED = None


class Preserved039Error(RuntimeError):
    pass


def check_package(base=None):
    global _MATERIALIZED
    base = Path(base) if base is not None else Path(__file__).resolve().parent
    if base.is_symlink() or not base.is_dir():
        raise Preserved039Error('Preserved039 package directory is unsafe')
    raw = (base / 'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise Preserved039Error('Preserved039 manifest differs')
    manifest = json.loads(raw)
    source = base.parent / 'preserved038' / 'integrity.py'
    spec = importlib.util.spec_from_file_location('_odin_preserved038_integrity', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        original, original_manifest = module.check_package()
    except Exception as exc:
        raise Preserved039Error('Preserved039 base integrity differs') from exc
    overlay = base / 'overlay'
    expected_overlay = set(manifest['sources']) - set(original_manifest['sources'])
    expected_overlay.update(n for n, h in manifest['sources'].items()
                            if original_manifest['sources'].get(n) != h)
    actual = {p.relative_to(overlay).as_posix() for p in overlay.rglob('*') if p.is_file()}
    if actual != expected_overlay:
        raise Preserved039Error('Preserved039 overlay inventory differs')
    for name in actual:
        path = overlay / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['sources'][name]:
            raise Preserved039Error('Preserved039 overlay integrity differs')
    if _MATERIALIZED is None:
        _MATERIALIZED = tempfile.TemporaryDirectory(prefix='odin-preserved039-sources-')
        bundle = Path(_MATERIALIZED.name) / 'bundle'
        shutil.copytree(original, bundle)
        shutil.copytree(overlay, bundle, dirs_exist_ok=True)
    bundle = Path(_MATERIALIZED.name) / 'bundle'
    for name, digest in manifest['sources'].items():
        path = bundle / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise Preserved039Error('Preserved039 materialized source differs')
    return bundle, manifest
