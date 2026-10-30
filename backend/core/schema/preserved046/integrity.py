"""Closed 046 restore sources: verified 045 base plus the complete released inventory.

Materialize an isolated private bundle; never import current runtime sources.
Written by ops/database_parity/freeze_preserved_bundle.py.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

MANIFEST_SHA256 = '0b9916340f24af243d5c3dd76dcd0328baf2a09fe01708118c8c6af9ab71d3ac'
PROFILE = 'restore-046'
SOURCE_COMMIT = '0b7a55318a640d4aac57ff82f5d15990c65641c6'
ERA_MIGRATION = 'python:046-tray-identity'
_MATERIALIZED = None


class Preserved046Error(RuntimeError):
    pass


def _check_metadata(manifest, base_manifest):
    """The pin proves these bytes; this states what the bytes must mean."""
    if manifest.get('profile') != PROFILE or manifest.get('source_commit') != SOURCE_COMMIT:
        raise Preserved046Error('Preserved046 manifest provenance differs')
    if set(manifest.get('ledger_ids') or ()) != set(base_manifest['ledger_ids']) | {ERA_MIGRATION}:
        raise Preserved046Error('Preserved046 manifest ledger differs')
    if manifest.get('sql_paths') != sorted(n for n in manifest['sources'] if n.endswith('.sql')):
        raise Preserved046Error('Preserved046 manifest SQL inventory differs')


def check_package(base=None):
    global _MATERIALIZED
    base = Path(base) if base is not None else Path(__file__).resolve().parent
    if base.is_symlink() or not base.is_dir():
        raise Preserved046Error('Preserved046 package directory is unsafe')
    raw = (base / 'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise Preserved046Error('Preserved046 manifest differs')
    manifest = json.loads(raw)
    source = base.parent / 'preserved045' / 'integrity.py'
    spec = importlib.util.spec_from_file_location('_odin_preserved045_integrity', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        original, original_manifest = module.check_package()
    except Exception as exc:
        raise Preserved046Error('Preserved046 base integrity differs') from exc
    _check_metadata(manifest, original_manifest)
    overlay = base / 'overlay'
    expected_overlay = set(manifest['sources']) - set(original_manifest['sources'])
    expected_overlay.update(n for n, h in manifest['sources'].items()
                            if original_manifest['sources'].get(n) != h)
    actual = {p.relative_to(overlay).as_posix() for p in overlay.rglob('*') if p.is_file()}
    if actual != expected_overlay:
        raise Preserved046Error('Preserved046 overlay inventory differs')
    for name in actual:
        path = overlay / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['sources'][name]:
            raise Preserved046Error('Preserved046 overlay integrity differs')
    if _MATERIALIZED is None:
        _MATERIALIZED = tempfile.TemporaryDirectory(prefix='odin-preserved046-sources-')
        bundle = Path(_MATERIALIZED.name) / 'bundle'
        shutil.copytree(original, bundle)
        shutil.copytree(overlay, bundle, dirs_exist_ok=True)
    bundle = Path(_MATERIALIZED.name) / 'bundle'
    for name, digest in manifest['sources'].items():
        path = bundle / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise Preserved046Error('Preserved046 materialized source differs')
    return bundle, manifest
