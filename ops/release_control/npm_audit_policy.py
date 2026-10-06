"""Full npm audit with one expiring, source-bound build-only risk acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).parents[2]
ADVISORY = 'GHSA-vfj7-8cjw-p6xm'
EXPIRES = '2026-10-19T00:00:00Z'
VERSIONS = {'braces': '3.0.3', 'chokidar': '3.6.0', 'fast-glob': '3.3.3',
            'micromatch': '4.0.8', 'tailwindcss': '3.4.19'}
EDGES = {'chokidar': ['braces'], 'fast-glob': ['micromatch'],
         'micromatch': ['braces'], 'tailwindcss': ['chokidar', 'fast-glob', 'micromatch']}
COVERAGE = {'prod': 64, 'dev': 347, 'optional': 28, 'peer': 9, 'peerOptional': 0, 'total': 411}
FILE_HASHES = {
    'Dockerfile': '61fda65b0ed7808125b7456805fce9cb1e4270c4eb13e0aebb5bc50d56f36baa',
    'frontend/package-lock.json': '06f2e3defb6245115af2c3e74b6a322dd62aff342bf6b60f4f586219eaf95d5b',
}
FRONTEND_HASH = '1b0a6c6bd19852a9cd2e2f136fe78e0ad3364a1f8b707a2d5f2694b4e544d945'


class AuditError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strict_json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AuditError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        def invalid_constant(value):
            raise AuditError('Nonstandard JSON constant')
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise AuditError('Invalid audit JSON') from exc


def frontend_digest(root: Path) -> str:
    paths = []
    for directory, children, files in os.walk(root / 'frontend'):
        current = Path(directory)
        if current == root / 'frontend':
            children[:] = [child for child in children if child not in ('node_modules', 'dist', '.vite')]
        for child in children:
            if (current / child).is_symlink():
                raise AuditError('Frontend symlink not approved')
        paths.extend(str((current / name).relative_to(root)) for name in files)
    payload = bytearray()
    for name in sorted(set(filter(None, paths))):
        if name == 'frontend/package-lock.json':
            continue
        path = root / name
        if path.is_symlink():
            raise AuditError('Frontend symlink not approved')
        payload.extend(name.encode() + b'\0' + digest(path.read_bytes()).encode() + b'\n')
    return digest(bytes(payload))


def check_context(root: Path) -> None:
    if (root / 'VERSION').read_text().strip() != '1.9.17':
        raise AuditError('Exception is only for version 1.9.17')
    for name, expected in FILE_HASHES.items():
        if digest((root / name).read_bytes()) != expected:
            raise AuditError('Reviewed build input changed: ' + name)
    if frontend_digest(root) != FRONTEND_HASH:
        raise AuditError('Reviewed frontend source changed')
    lock = strict_json((root / 'frontend/package-lock.json').read_bytes())
    for name, version in VERSIONS.items():
        node = lock['packages']['node_modules/' + name]
        if node.get('dev') is not True or node.get('version') != version:
            raise AuditError('Exception dependency is not the approved development-only version')


def lock_coverage(root: Path, lock_bytes: bytes | None = None) -> dict[str, int]:
    """Match npm Arborist's inventory counters, including overlapping flags/root."""
    lock = strict_json(lock_bytes if lock_bytes is not None else (root / 'frontend/package-lock.json').read_bytes())
    packages = lock.get('packages') if isinstance(lock, dict) else None
    if (not isinstance(lock, dict) or type(lock.get('lockfileVersion')) is not int
            or lock['lockfileVersion'] != 3 or not isinstance(packages, dict)
            or '' not in packages or len(packages) < 2):
        raise AuditError('Unsupported audit lockfile')
    flags = ('dev', 'optional', 'peer', 'peerOptional')
    counts = dict.fromkeys(('prod', *flags), 0)
    counts['total'] = len(packages) - 1
    for name, node in packages.items():
        if (not isinstance(node, dict) or node.get('link')
                or (name != '' and (not name.startswith('node_modules/')
                    or any(part in ('', '.', '..') for part in name.split('/'))))):
            raise AuditError('Unsupported audit package record')
        if any(key in node and type(node[key]) is not bool for key in (*flags, 'link')):
            raise AuditError('Nonboolean audit package flag')
        prod = True
        for key in flags:
            if node.get(key, False):
                counts[key] += 1
                prod = False
        if prod:
            counts['prod'] += 1
    return counts


def assess(report: dict, exit_code: int, *, now: datetime | None = None, root: Path = ROOT, audited_lock_bytes: bytes | None = None) -> str:
    if not isinstance(report, dict) or type(report.get('auditReportVersion')) is not int or report.get('auditReportVersion') != 2 or 'error' in report:
        raise AuditError('Unsupported or failed audit')
    metadata = report.get('metadata', {})
    if (not isinstance(metadata, dict) or metadata.get('dependencies') != lock_coverage(root, audited_lock_bytes)
            or any(type(value) is not int for value in metadata['dependencies'].values())):
        raise AuditError('Audit dependency coverage changed')
    vulnerabilities = report.get('vulnerabilities')
    if not isinstance(vulnerabilities, dict):
        raise AuditError('Missing vulnerability map')
    counts = dict.fromkeys(['info', 'low', 'moderate', 'high', 'critical'], 0)
    for name, entry in vulnerabilities.items():
        if not isinstance(entry, dict) or entry.get('name') != name or entry.get('severity') not in counts:
            raise AuditError('Malformed vulnerability')
        counts[entry['severity']] += 1
    counts['total'] = len(vulnerabilities)
    actual = metadata.get('vulnerabilities', {})
    if not isinstance(actual, dict) or actual != counts or any(type(value) is not int for value in actual.values()):
        raise AuditError('Audit counts disagree')
    expected_exit = int(bool(counts['high'] or counts['critical']))
    if type(exit_code) is not int or exit_code != expected_exit:
        raise AuditError('Audit process status disagrees')
    if not vulnerabilities:
        return 'pass'
    if metadata['dependencies'] != COVERAGE:
        raise AuditError('Historical exception coverage changed')
    if set(vulnerabilities) != set(VERSIONS):
        raise AuditError('Unapproved vulnerability set')
    for name, entry in vulnerabilities.items():
        if (entry['severity'] != 'high' or entry.get('isDirect') is not (name == 'tailwindcss')
                or entry.get('nodes') != ['node_modules/' + name]):
            raise AuditError('Unapproved dependency exposure')
        via = entry.get('via')
        if name != 'braces':
            if via != EDGES[name]:
                raise AuditError('Unapproved vulnerability graph')
        else:
            if not isinstance(via, list) or len(via) != 1 or not isinstance(via[0], dict):
                raise AuditError('Unapproved advisory')
            allowed = {'source': 1240992, 'name': 'braces', 'dependency': 'braces',
                       'url': 'https://github.com/advisories/' + ADVISORY, 'severity': 'high', 'range': '<=3.0.3'}
            if type(via[0].get('source')) is not int or any(via[0].get(key) != value for key, value in allowed.items()):
                raise AuditError('Unapproved advisory')
    now = now or datetime.now(timezone.utc)
    if now >= datetime.fromisoformat(EXPIRES.replace('Z', '+00:00')):
        raise AuditError('Build-only exception expired')
    check_context(root)
    return 'pass_with_exception'


def audit(output: Path, decision_path: Path, root: Path = ROOT) -> int:
    decision = {'status': 'fail', 'npm_exit_code': None}
    output.parent.mkdir(parents=True, exist_ok=True)
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    # Do not leave successful artifacts behind if a later invocation fails.
    output.write_text('{}\n')
    try:
        with tempfile.TemporaryDirectory(prefix='odin-npm-audit-') as name:
            temporary = Path(name)
            snapshot = {filename: (root / 'frontend' / filename).read_bytes()
                        for filename in ('package.json', 'package-lock.json')}
            for filename in ('package.json', 'package-lock.json'):
                (temporary / filename).write_bytes(snapshot[filename])
            env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ}
            env['HOME'] = name
            command = ['npm', 'audit', '--include=dev', '--include=optional', '--include=peer',
                       '--audit-level=high', '--json', '--registry=https://registry.npmjs.org',
                       '--userconfig=' + str(temporary / 'user.npmrc'),
                       '--globalconfig=' + str(temporary / 'global.npmrc')]
            completed = subprocess.run(command, cwd=temporary, env=env, capture_output=True, timeout=180)
        if any((root / 'frontend' / name).read_bytes() != data
               for name, data in snapshot.items()):
            raise AuditError('Audit inputs changed during execution')
        decision['npm_exit_code'] = completed.returncode
        report = strict_json(completed.stdout)
        # npm error payloads may contain registry credentials; never persist those.
        if not isinstance(report, dict) or 'error' in report:
            raise AuditError('npm audit failed')
        decision['status'] = assess(report, completed.returncode, root=root, audited_lock_bytes=snapshot['package-lock.json'])
        if any((root / 'frontend' / name).read_bytes() != data
               for name, data in snapshot.items()):
            raise AuditError('Audit inputs changed during assessment')
        # Only the validated report shape/scope may enter release artifacts.
        # Failed/unknown registry payloads remain discarded, with generic reasons.
        output.write_bytes(completed.stdout)
        decision['raw_audit_sha256'] = digest(completed.stdout)
        if decision['status'] == 'pass_with_exception':
            decision.update({'advisory': ADVISORY, 'expires_at': EXPIRES,
                             'policy_sha256': digest(Path(__file__).read_bytes())})
    except (AuditError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        decision['status'] = 'fail'
        decision['reason'] = str(exc) if isinstance(exc, AuditError) else 'Audit execution or input failure'
    decision_path.write_text(json.dumps(decision, sort_keys=True, indent=2) + '\n')
    print(json.dumps(decision, sort_keys=True))
    return 0 if decision['status'] in ('pass', 'pass_with_exception') else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--decision', type=Path, required=True)
    args = parser.parse_args()
    return audit(args.output, args.decision)


if __name__ == '__main__':
    raise SystemExit(main())
