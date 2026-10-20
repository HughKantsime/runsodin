"""Closed preserved034 source bytes; no live-source refresh or importer fallback.

The bundle is a byte copy of the restore-stage sources at source_commit (the last
tree whose live files the pre026 restore contract qualified). It hosts the released034 PostgreSQL archive stages so live schema sources can evolve. It also holds
core/timelapse_recipes.py, which migration 019 reads by path for its own source pin
(preserved027 lacks it).
"""
import hashlib
import json
import stat
from pathlib import Path

MANIFEST_SHA256='17eb84d24a3cfc50a0a500d962c8d5239bc5eacb696f669e68ae7c1a5efabaea'
SOURCE_COUNT=392
SQL_COUNT=24
LEDGER_COUNT=55


class Preserved034Error(RuntimeError):
    pass


def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise Preserved034Error('Preserved034 file is not regular')


def check_package(base=None):
    base=Path(base) if base is not None else Path(__file__).absolute().parent
    if base.is_symlink() or not base.is_dir():raise Preserved034Error('Preserved034 package directory is unsafe')
    manifest_path=base/'manifest.json';_regular(manifest_path)
    raw=manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=MANIFEST_SHA256:raise Preserved034Error('Preserved034 manifest differs')
    manifest=json.loads(raw)
    if set(manifest)!={'profile','source_commit','sources','sql_paths','ledger_ids'} or manifest['profile']!='restore-034':
        raise Preserved034Error('Preserved034 manifest shape differs')
    expected=manifest['sources']
    if len(expected)!=SOURCE_COUNT or len(manifest['sql_paths'])!=SQL_COUNT or sorted(n for n in expected if n.endswith('.sql'))!=manifest['sql_paths'] or len(set(manifest['ledger_ids']))!=LEDGER_COUNT:
        raise Preserved034Error('Preserved034 file inventory differs')
    bundle=base/'bundle'
    if bundle.is_symlink() or not bundle.is_dir():raise Preserved034Error('Preserved034 bundle is unsafe')
    actual=set()
    for path in bundle.rglob('*'):
        mode=path.lstat().st_mode
        if stat.S_ISDIR(mode):continue
        if not stat.S_ISREG(mode):raise Preserved034Error('Preserved034 source is unsafe')
        actual.add(path.relative_to(bundle).as_posix())
    if actual!=set(expected):raise Preserved034Error('Preserved034 source inventory differs')
    for name,digest in expected.items():
        if hashlib.sha256((bundle/name).read_bytes()).hexdigest()!=digest:raise Preserved034Error('Preserved034 source integrity differs')
    return bundle,manifest
