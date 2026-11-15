"""Fail-closed output validation and ownership cleanup for the native proof."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import signal
import subprocess

LABEL = 'app.odin.restore-proof'
DEFAULT_PREFIX = 'odin-restore-proof-'


def run(*args, timeout=120):
    try:
        value = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'Restore proof command timed out after {timeout} seconds: {args[0]}') from None
    if value.returncode:
        # Child output can contain generated credentials. Keep failures plain.
        raise RuntimeError(f'Restore proof command failed: {" ".join(args[:3])} (exit {value.returncode})')
    return value.stdout


def expected_facts(kind):
    return json.loads(Path(__file__).with_name('restore_proof_facts.json').read_text())[kind]


def validate_facts(stdout, kind):
    prefix = 'post027-' + kind + '-'
    facts = [line for line in stdout.splitlines() if line.startswith(prefix)]
    expected = expected_facts(kind)
    if len(facts) != len(expected) or any(not re.fullmatch(pattern, fact) for pattern, fact in zip(expected, facts)):
        raise RuntimeError(f'The {kind} restore proof did not report every required check in order')
    if facts[-1] != prefix + 'pass':
        raise RuntimeError(f'The {kind} restore proof did not finish successfully')
    return facts


def cleanup(prefix):
    """Both the exact ownership label and the name prefix are required."""
    failures = []
    for kind in ('container', 'network', 'volume'):
        try:
            names = run('docker', kind, 'ls', *(['--all'] if kind == 'container' else []),
                        '--filter', f'label={LABEL}={prefix}', '--format', '{{.Names}}' if kind == 'container' else '{{.Name}}').splitlines()
            for name in names:
                if not name.startswith(prefix):
                    raise RuntimeError('Restore proof resource has an unexpected name; cleanup stopped')
                try:
                    run('docker', kind, 'rm', *(['--force', '--volumes'] if kind == 'container' else []), name, timeout=30)
                except RuntimeError as error:
                    failures.append(str(error))
        except (RuntimeError, OSError) as error:
            failures.append(str(error))
    if failures:
        raise RuntimeError('Restore proof cleanup failed: ' + '; '.join(failures))


@contextmanager
def owned_run(prefix, timeout=3000):
    if not re.fullmatch(r'odin-restore-proof-[A-Za-z0-9_.-]{0,48}', prefix):
        raise ValueError('Restore proof container prefix must start with odin-restore-proof-')
    # Stable across checkouts and isolated HOME/TMPDIR, unlike a per-run lock.
    lock_path = Path('/tmp') / ('odin-restore-proof-' + hashlib.sha256(prefix.encode()).hexdigest() + '.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another restore proof owns this container prefix') from None
        def interrupted(signum, frame):
            raise InterruptedError('Restore proof was interrupted')
        handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
        def timed_out(signum, frame):
            raise RuntimeError(f'The restore proof timed out after {timeout} seconds')
        handlers[signal.SIGALRM] = signal.signal(signal.SIGALRM, timed_out)
        try:
            cleanup(prefix)  # Reap a killed prior run before creating anything.
            signal.alarm(timeout)
            yield
        finally:
            signal.alarm(0)
            for sig in handlers:
                signal.signal(sig, signal.SIG_IGN)
            try:
                cleanup(prefix)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
