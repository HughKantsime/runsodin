"""Freeze a released schema era as a preservedNNN restore bundle, from Git objects only.

The bundle is a complete snapshot of the released backend/core and backend/modules
.py, .sql and .json sources (nested preservedNNN packages excluded), stored as an
overlay on the previous bundle. Earlier bundles refreshed only the paths they already
listed, which left the restore child without modules the refreshed code imports.

Selected bytes come from the commit's Git objects, never the working tree. The ledger
is read from a database bootstrapped by those same bytes in an isolated interpreter.

Usage (explicit inputs, no defaults):
  python -m ops.database_parity.freeze_preserved_bundle \
      --commit <full sha> --release 2.0.36 --era 46 --base-era 45 \
      --migration python:046-tray-identity --write
  ... --check     verify the committed bundle against the commit, changing nothing
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / 'backend/core/schema'
SOURCE_ROOTS = ('backend/core', 'backend/modules', 'backend/license_manager.py', 'backend/main.py')
SOURCE_SUFFIXES = ('.py', '.sql', '.json')
_NESTED_BUNDLE = re.compile(r'backend/core/schema/preserved\d{3}/')
_FULL_SHA = re.compile(r'[0-9a-f]{40}')


class FreezeError(RuntimeError):
    pass


def _git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True).stdout


def resolve_commit(commit):
    if not _FULL_SHA.fullmatch(commit):
        raise FreezeError('A full 40-character commit is required')
    try:
        _git('cat-file', '-e', commit + '^{commit}')
    except subprocess.CalledProcessError:
        raise FreezeError('Commit is not available in this checkout: ' + commit) from None
    return commit


def blob(commit, path):
    return _git('cat-file', 'blob', f'{commit}:{path}')


def selected_paths(commit):
    """Repository paths of the complete source inventory at ``commit``."""
    listed = _git('ls-tree', '-r', '--name-only', commit, *SOURCE_ROOTS).decode().splitlines()
    return sorted(path for path in listed
                  if path.endswith(SOURCE_SUFFIXES) and not _NESTED_BUNDLE.match(path))


def registry(commit):
    """POST027_MIGRATIONS ids at ``commit``, read as a literal without importing it."""
    tree = ast.parse(blob(commit, 'backend/core/schema/bootstrap.py'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['POST027_MIGRATIONS']:
            return [item[0] for item in ast.literal_eval(node.value)]
    raise FreezeError('POST027_MIGRATIONS literal not found')


def verify_release(commit, release, era):
    """The commit is the named release, its registry stops at ``era`` and its ledger says so."""
    version = blob(commit, 'VERSION').decode().strip()
    if version != release:
        raise FreezeError(f'Commit VERSION is {version}, not {release}')
    ids = registry(commit)
    if not ids or int(ids[-1].split(':')[1][:3]) != era:
        raise FreezeError(f'Commit registry does not end at era {era}: {ids[-1:]}')
    recorded = json.loads(blob(commit, 'backend/core/schema/released_post027_ledgers.json'))['releases']
    if recorded.get(release) != ids:
        raise FreezeError(f'Recorded ledger of {release} differs from the commit registry')
    return ids


_LEDGER_PROGRAM = '''
import json,sys
sys.path.insert(0,sys.argv[1])
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
import core
assert core.__file__.startswith(sys.argv[1]),core.__file__
engine=create_engine('sqlite:///'+sys.argv[2])
bootstrap_database(engine)
with engine.connect() as c:
    print(json.dumps(sorted(row[0] for row in c.execute(text('SELECT migration_id FROM odin_schema_migrations')))))
engine.dispose()
'''


def independent_ledger(sources):
    """The ledger these exact bytes write to a fresh database, in an isolated interpreter."""
    with tempfile.TemporaryDirectory(prefix='freeze-preserved-') as folder:
        tree = Path(folder) / 'backend'
        for name, raw in sources.items():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        data = Path(folder) / 'data'; data.mkdir()
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
               'ODIN_DATA_DIR': str(data), 'DATABASE_URL': 'sqlite:///' + str(Path(folder) / 'ledger.db'),
               'JWT_SECRET_KEY': hashlib.sha256(os.urandom(32)).hexdigest()}
        result = subprocess.run([sys.executable, '-I', '-c', _LEDGER_PROGRAM, str(tree), str(Path(folder) / 'ledger.db')],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise FreezeError('Released sources could not bootstrap a fresh database: ' + result.stderr[-2000:])
        return json.loads(result.stdout.strip().splitlines()[-1])


def base_manifest(base_era):
    """The base bundle's manifest, after checking it against the base's own pin."""
    folder = SCHEMA / f'preserved{base_era:03}'
    raw = (folder / 'manifest.json').read_bytes()
    pin = re.search(r"^MANIFEST_SHA256 = '([0-9a-f]{64})'$", (folder / 'integrity.py').read_text(), re.M)
    if pin is None or hashlib.sha256(raw).hexdigest() != pin.group(1):
        raise FreezeError(f'Base preserved{base_era:03} manifest differs from its pin')
    return json.loads(raw)


def manifest_bytes(manifest):
    return (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()


def build(commit, release, era, base_era, *, migration):
    """Everything the bundle contains, computed from Git objects and the base manifest."""
    commit = resolve_commit(commit)
    ids = verify_release(commit, release, era)
    if ids[-1] != migration:
        raise FreezeError(f'Commit registry ends at {ids[-1]}, not {migration}')
    base = base_manifest(base_era)
    sources = {path[len('backend/'):]: blob(commit, path) for path in selected_paths(commit)}
    missing = sorted(set(base['sources']) - set(sources))
    if missing:
        raise FreezeError(f'Base sources are absent from the released inventory: {missing}')
    ledger = independent_ledger(sources)
    if set(ledger) != set(base['ledger_ids']) | {migration} or migration not in ledger:
        raise FreezeError(f'Released ledger is not preserved{base_era:03} plus {migration}')
    following = f'python:{era + 1:03}-'
    if any(item.startswith(following) for item in ledger) or any(f'/{era + 1:03}_' in name for name in sources):
        raise FreezeError(f'Released source already contains migration {era + 1:03}')
    digests = {name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()}
    manifest = {
        'ledger_ids': sorted(ledger),
        'profile': f'restore-{era:03}',
        'source_commit': commit,
        'source_note': (f'Complete backend/core and backend/modules source inventory of Release {release}, '
                        f'schema through {era:03}, stored as an overlay on the pinned preserved{base_era:03} base.'),
        'sources': digests,
        'sql_paths': sorted(name for name in sources if name.endswith('.sql')),
    }
    overlay = {name: raw for name, raw in sources.items() if base['sources'].get(name) != digests[name]}
    return manifest, overlay


_INTEGRITY = '''"""Closed {era} restore sources: verified {base} base plus the complete released inventory.

Materialize an isolated private bundle; never import current runtime sources.
Written by ops/database_parity/freeze_preserved_bundle.py.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

MANIFEST_SHA256 = '{pin}'
PROFILE = '{profile}'
SOURCE_COMMIT = '{commit}'
ERA_MIGRATION = '{migration}'
_MATERIALIZED = None


class Preserved{era}Error(RuntimeError):
    pass


def _check_metadata(manifest, base_manifest):
    """The pin proves these bytes; this states what the bytes must mean."""
    if manifest.get('profile') != PROFILE or manifest.get('source_commit') != SOURCE_COMMIT:
        raise Preserved{era}Error('Preserved{era} manifest provenance differs')
    if set(manifest.get('ledger_ids') or ()) != set(base_manifest['ledger_ids']) | {{ERA_MIGRATION}}:
        raise Preserved{era}Error('Preserved{era} manifest ledger differs')
    if manifest.get('sql_paths') != sorted(n for n in manifest['sources'] if n.endswith('.sql')):
        raise Preserved{era}Error('Preserved{era} manifest SQL inventory differs')


def check_package(base=None):
    global _MATERIALIZED
    base = Path(base) if base is not None else Path(__file__).resolve().parent
    if base.is_symlink() or not base.is_dir():
        raise Preserved{era}Error('Preserved{era} package directory is unsafe')
    raw = (base / 'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise Preserved{era}Error('Preserved{era} manifest differs')
    manifest = json.loads(raw)
    source = base.parent / 'preserved{base}' / 'integrity.py'
    spec = importlib.util.spec_from_file_location('_odin_preserved{base}_integrity', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        original, original_manifest = module.check_package()
    except Exception as exc:
        raise Preserved{era}Error('Preserved{era} base integrity differs') from exc
    _check_metadata(manifest, original_manifest)
    overlay = base / 'overlay'
    expected_overlay = set(manifest['sources']) - set(original_manifest['sources'])
    expected_overlay.update(n for n, h in manifest['sources'].items()
                            if original_manifest['sources'].get(n) != h)
    actual = {{p.relative_to(overlay).as_posix() for p in overlay.rglob('*') if p.is_file()}}
    if actual != expected_overlay:
        raise Preserved{era}Error('Preserved{era} overlay inventory differs')
    for name in actual:
        path = overlay / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['sources'][name]:
            raise Preserved{era}Error('Preserved{era} overlay integrity differs')
    if _MATERIALIZED is None:
        _MATERIALIZED = tempfile.TemporaryDirectory(prefix='odin-preserved{era}-sources-')
        bundle = Path(_MATERIALIZED.name) / 'bundle'
        shutil.copytree(original, bundle)
        shutil.copytree(overlay, bundle, dirs_exist_ok=True)
    bundle = Path(_MATERIALIZED.name) / 'bundle'
    for name, digest in manifest['sources'].items():
        path = bundle / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise Preserved{era}Error('Preserved{era} materialized source differs')
    return bundle, manifest
'''


def _wrapper(text, era, base_era):
    """Adapt a base wrapper file: names, action, and the era on both ends of the acknowledgement."""
    new, old = f'{era:03}', f'{base_era:03}'
    for prefix in ('Preserved', 'preserved', 'released', 'restore'):
        text = text.replace(prefix + old, prefix + new)
    text = text.replace(f'({base_era})', f'({era})').replace(f'era={base_era}', f'era={era}')
    if old in text or f'({base_era})' in text or f'era={base_era}' in text:
        raise FreezeError(f'Wrapper still names era {base_era}')
    return text


def control_files(manifest, era, base_era, *, migration):
    raw = manifest_bytes(manifest)
    base = SCHEMA / f'preserved{base_era:03}'
    return {
        'manifest.json': raw,
        '__init__.py': _wrapper((base / '__init__.py').read_text(), era, base_era).encode(),
        'runner.py': _wrapper((base / 'runner.py').read_text(), era, base_era).encode(),
        'integrity.py': _INTEGRITY.format(era=f'{era:03}', base=f'{base_era:03}', pin=hashlib.sha256(raw).hexdigest(),
                                          profile=manifest['profile'], commit=manifest['source_commit'],
                                          migration=migration).encode(),
    }


def write(target, manifest, overlay, control):
    """Replace the bundle directory. Only this generator's own output path is touched."""
    expected = {f'preserved{int(manifest["profile"].split("-")[1]):03}'}
    if target.parent != SCHEMA or target.name not in expected:
        raise FreezeError('Refusing to write outside the era bundle directory')
    if target.exists():
        shutil.rmtree(target)
    for name, raw in {**{f'overlay/{name}': raw for name, raw in overlay.items()}, **control}.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def check(target, manifest, overlay, control):
    """Differences between the committed bundle and what the commit produces."""
    expected = {**{f'overlay/{name}': raw for name, raw in overlay.items()}, **control}
    actual = {path.relative_to(target).as_posix() for path in target.rglob('*')
              if path.is_file() and '__pycache__' not in path.parts}
    problems = [f'missing: {name}' for name in sorted(set(expected) - actual)]
    problems += [f'extra: {name}' for name in sorted(actual - set(expected))]
    problems += [f'differs: {name}' for name in sorted(actual & set(expected))
                 if (target / name).read_bytes() != expected[name]]
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--commit', required=True, help='Full SHA of the released commit')
    parser.add_argument('--release', required=True, help='Version the commit released, such as 2.0.36')
    parser.add_argument('--era', required=True, type=int, help='Schema era the bundle freezes, such as 46')
    parser.add_argument('--base-era', required=True, type=int, help='Era of the storage base bundle, such as 45')
    parser.add_argument('--migration', required=True, help='Registry id the era ends at, such as python:046-tray-identity')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    manifest, overlay = build(args.commit, args.release, args.era, args.base_era, migration=args.migration)
    control = control_files(manifest, args.era, args.base_era, migration=args.migration)
    target = SCHEMA / f'preserved{args.era:03}'
    if args.write:
        write(target, manifest, overlay, control)
        print(f'wrote {target.relative_to(ROOT)}: {len(manifest["sources"])} sources, {len(overlay)} overlay files')
        return 0
    problems = check(target, manifest, overlay, control)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(f'{target.relative_to(ROOT)} matches {args.commit}: {len(manifest["sources"])} sources, {len(overlay)} overlay files')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
