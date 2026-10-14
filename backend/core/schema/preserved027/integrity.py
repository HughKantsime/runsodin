"""Closed preserved027 source bytes; no live-source refresh or importer fallback."""
import hashlib
import json
import stat
from pathlib import Path

MANIFEST_SHA256='dceb41a9c6afc326d2c0f54995c03392d7eefd3f3bfbbe116e3b688c49dc78ba'

class Preserved027Error(RuntimeError):
    pass

def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise Preserved027Error('Preserved027 file is not regular')

def check_package(base=None):
    base=Path(base) if base is not None else Path(__file__).absolute().parent
    if base.is_symlink() or not base.is_dir():raise Preserved027Error('Preserved027 package directory is unsafe')
    manifest_path=base/'manifest.json';_regular(manifest_path)
    raw=manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=MANIFEST_SHA256:raise Preserved027Error('Preserved027 manifest differs')
    manifest=json.loads(raw)
    if set(manifest)!= {'profile','original_sources','producer_sources','sql_paths'} or manifest['profile']!='pre-priced-027' or len(manifest['original_sources'])!=115:
        raise Preserved027Error('Preserved027 manifest shape differs')
    expected={**manifest['original_sources'],**manifest['producer_sources']}
    if len(expected)!=116 or len(manifest['sql_paths'])!=24 or sorted(n for n in expected if n.endswith('.sql'))!=manifest['sql_paths']:
        raise Preserved027Error('Preserved027 file inventory differs')
    bundle=base/'bundle'
    if bundle.is_symlink() or not bundle.is_dir():raise Preserved027Error('Preserved027 bundle is unsafe')
    actual=set()
    for path in bundle.rglob('*'):
        mode=path.lstat().st_mode
        if stat.S_ISDIR(mode):continue
        if not stat.S_ISREG(mode):raise Preserved027Error('Preserved027 source is unsafe')
        actual.add(path.relative_to(bundle).as_posix())
    if actual!=set(expected):raise Preserved027Error('Preserved027 source inventory differs')
    for name,digest in expected.items():
        if hashlib.sha256((bundle/name).read_bytes()).hexdigest()!=digest:raise Preserved027Error('Preserved027 source integrity differs')
    return bundle,manifest
