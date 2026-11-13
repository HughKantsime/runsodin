"""Closed 056 restore sources: verified 055 base plus the complete released inventory.

Materialize an isolated private bundle; never import current runtime sources.
Written by ops/database_parity/freeze_preserved_bundle.py.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

MANIFEST_SHA256 = 'b32d3212f4c92ac75ddcbf36eaf73dfc117019e6d1fc6ba9a2a9484745d9a9f1'
PROFILE = 'restore-056'
SOURCE_COMMIT = '9112d4071b88d33dbbc5b82da558dfdf74d663db'
ERA_MIGRATION = 'python:056-dispatch-recovery-global'
_MATERIALIZED = None


class Preserved056Error(RuntimeError):
    pass


def _check_metadata(manifest, base_manifest):
    """The pin proves these bytes; this states what the bytes must mean."""
    if manifest.get('profile') != PROFILE or manifest.get('source_commit') != SOURCE_COMMIT:
        raise Preserved056Error('Preserved056 manifest provenance differs')
    if set(manifest.get('ledger_ids') or ()) != set(base_manifest['ledger_ids']) | {ERA_MIGRATION}:
        raise Preserved056Error('Preserved056 manifest ledger differs')
    if manifest.get('sql_paths') != sorted(n for n in manifest['sources'] if n.endswith('.sql')):
        raise Preserved056Error('Preserved056 manifest SQL inventory differs')


def check_package(base=None):
    global _MATERIALIZED
    base = Path(base) if base is not None else Path(__file__).resolve().parent
    if base.is_symlink() or not base.is_dir():
        raise Preserved056Error('Preserved056 package directory is unsafe')
    raw = (base / 'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise Preserved056Error('Preserved056 manifest differs')
    manifest = json.loads(raw)
    source = base.parent / 'preserved055' / 'integrity.py'
    spec = importlib.util.spec_from_file_location('_odin_preserved055_integrity', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        original, original_manifest = module.check_package()
    except Exception as exc:
        raise Preserved056Error('Preserved056 base integrity differs') from exc
    _check_metadata(manifest, original_manifest)
    overlay = base / 'overlay'
    expected_overlay = set(manifest['sources']) - set(original_manifest['sources'])
    expected_overlay.update(n for n, h in manifest['sources'].items()
                            if original_manifest['sources'].get(n) != h)
    actual = {p.relative_to(overlay).as_posix() for p in overlay.rglob('*') if p.is_file()}
    if actual != expected_overlay:
        raise Preserved056Error('Preserved056 overlay inventory differs')
    for name in actual:
        path = overlay / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['sources'][name]:
            raise Preserved056Error('Preserved056 overlay integrity differs')
    if _MATERIALIZED is None:
        _MATERIALIZED = tempfile.TemporaryDirectory(prefix='odin-preserved056-sources-')
        bundle = Path(_MATERIALIZED.name) / 'bundle'
        shutil.copytree(original, bundle)
        shutil.copytree(overlay, bundle, dirs_exist_ok=True)
    bundle = Path(_MATERIALIZED.name) / 'bundle'
    for name, digest in manifest['sources'].items():
        path = bundle / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise Preserved056Error('Preserved056 materialized source differs')
    return bundle, manifest
