"""Closed 057 restore sources: verified 056 base plus the complete released inventory.

Materialize an isolated private bundle; never import current runtime sources.
Written by ops/database_parity/freeze_preserved_bundle.py.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

MANIFEST_SHA256 = '9b610319802176b184fa369e3670594fb7aa18b541b112707ee42d7ea24002da'
PROFILE = 'restore-057'
SOURCE_COMMIT = '3911bd94784e19fc47c3d0d5ffe430a13d4d15fc'
ERA_MIGRATION = 'python:057-slot-feed-policy'
_MATERIALIZED = None


class Preserved057Error(RuntimeError):
    pass


def _check_metadata(manifest, base_manifest):
    """The pin proves these bytes; this states what the bytes must mean."""
    if manifest.get('profile') != PROFILE or manifest.get('source_commit') != SOURCE_COMMIT:
        raise Preserved057Error('Preserved057 manifest provenance differs')
    if set(manifest.get('ledger_ids') or ()) != set(base_manifest['ledger_ids']) | {ERA_MIGRATION}:
        raise Preserved057Error('Preserved057 manifest ledger differs')
    if manifest.get('sql_paths') != sorted(n for n in manifest['sources'] if n.endswith('.sql')):
        raise Preserved057Error('Preserved057 manifest SQL inventory differs')


def check_package(base=None):
    global _MATERIALIZED
    base = Path(base) if base is not None else Path(__file__).resolve().parent
    if base.is_symlink() or not base.is_dir():
        raise Preserved057Error('Preserved057 package directory is unsafe')
    raw = (base / 'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise Preserved057Error('Preserved057 manifest differs')
    manifest = json.loads(raw)
    source = base.parent / 'preserved056' / 'integrity.py'
    spec = importlib.util.spec_from_file_location('_odin_preserved056_integrity', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        original, original_manifest = module.check_package()
    except Exception as exc:
        raise Preserved057Error('Preserved057 base integrity differs') from exc
    _check_metadata(manifest, original_manifest)
    overlay = base / 'overlay'
    expected_overlay = set(manifest['sources']) - set(original_manifest['sources'])
    expected_overlay.update(n for n, h in manifest['sources'].items()
                            if original_manifest['sources'].get(n) != h)
    actual = {p.relative_to(overlay).as_posix() for p in overlay.rglob('*') if p.is_file()}
    if actual != expected_overlay:
        raise Preserved057Error('Preserved057 overlay inventory differs')
    for name in actual:
        path = overlay / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['sources'][name]:
            raise Preserved057Error('Preserved057 overlay integrity differs')
    if _MATERIALIZED is None:
        _MATERIALIZED = tempfile.TemporaryDirectory(prefix='odin-preserved057-sources-')
        bundle = Path(_MATERIALIZED.name) / 'bundle'
        shutil.copytree(original, bundle)
        shutil.copytree(overlay, bundle, dirs_exist_ok=True)
    bundle = Path(_MATERIALIZED.name) / 'bundle'
    for name, digest in manifest['sources'].items():
        path = bundle / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise Preserved057Error('Preserved057 materialized source differs')
    return bundle, manifest
