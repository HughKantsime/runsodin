"""Signed, ephemeral licenses for owned validation processes only.

Only the verifier's public key is substituted, in memory. No image/source file or
installation license is changed; signatures, tier features and capacity remain
production code paths. This is not evidence of vendor-issued license activation.
"""
from contextlib import contextmanager
import base64
import json
import os
from pathlib import Path
import tempfile


@contextmanager
def signed_tier(tier):
    import license_manager as lm
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    names = ('ODIN_LICENSE_FILE', 'ODIN_REQUIRE_LICENSE_BINDING', 'ODIN_LICENSE_READ_ONLY')
    environment = {name: os.environ.get(name) for name in names}
    # Every piece of license_manager's cache, including the bounded-reload state, so the
    # owned process returns exactly to its previous license (same cached object).
    attributes = {name: getattr(lm, name) for name in
                  ('ODIN_PUBLIC_KEY', 'LICENSE_DIR', '_cached_license', '_cached_mtime',
                   '_cached_identity', '_clock', '_clock_directory', '_cache_checked',
                   '_cache_verified', '_cache_retry', '_cache_failures', '_cache_file_identity')
                  if hasattr(lm, name)}
    with tempfile.TemporaryDirectory(prefix='odin-validation-license-') as directory:
        try:
            for name in names:
                os.environ.pop(name, None)
            lm.LICENSE_DIR = directory
            os.environ['ODIN_LICENSE_FILE'] = str(Path(directory) / lm.LICENSE_FILENAME)
            issuer = Ed25519PrivateKey.generate()
            lm.ODIN_PUBLIC_KEY = issuer.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
            raw = json.dumps({'tier': tier, 'licensee': 'Synthetic validation fixture',
                              'expires_at': '2099-01-01'}).encode()
            lm.save_license_file(base64.urlsafe_b64encode(raw).decode() + '.' +
                                 base64.urlsafe_b64encode(issuer.sign(raw)).decode())
            lm._cached_license = None
            lm._cached_mtime = 0
            info = lm.get_license()
            if not info.valid or info.tier != tier:
                raise RuntimeError('Signed validation license failed verification')
            yield info
        finally:
            for name, value in attributes.items():
                setattr(lm, name, value)
            for name, value in environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def serve_candidate():
    """Temporary Pro backend in the owned candidate; original supervisor stays stopped."""
    import sys
    if os.environ.get('ODIN_RELEASE_GATE') != '1':
        raise SystemExit('Refusing license fixture outside an owned candidate')
    os.chdir('/app/backend')
    sys.path.insert(0, '/app/backend')
    # A fixed PID path is safe here: one fixture at a time in a disposable container.
    pid = Path('/tmp/odin-candidate-licensed.pid')
    with pid.open('x') as stream:
        stream.write(str(os.getpid()))
    try:
        with signed_tier('pro'):
            import signal
            import uvicorn
            # Uvicorn re-raises captured signals after graceful shutdown. Restore
            # a no-op handler so our finally blocks remove the PID/license files.
            signal.signal(signal.SIGTERM, lambda *_: None)
            uvicorn.run('main:app', host='0.0.0.0', port=8000, workers=1, proxy_headers=False)
    finally:
        pid.unlink(missing_ok=True)


@contextmanager
def candidate_pro_backend(container, base_url):
    """Run just the CSV browser journey with a real signed Pro backend.

    The candidate's ordinary Community process is stopped and restored. The
    temporary process loads unmodified image code with an in-memory test issuer.
    No key, license, or shim is persisted in the image or its application volume.
    """
    import re
    import subprocess
    import time
    import requests

    if not re.fullmatch(r'odin-candidate-[a-z0-9][a-z0-9-]{2,63}', container):
        raise ValueError('Expected an owned candidate container')
    prefix = ['docker', 'exec', container]

    def command(*args):
        return subprocess.run([*prefix, *args], check=True, capture_output=True, text=True, timeout=40)

    command('python3', '-c',
            "import os; assert os.environ.get('ODIN_RELEASE_GATE') == '1'; "
            "assert 'odin-candidate-' + os.environ['ODIN_RELEASE_GATE_RUN_ID'] == " + repr(container))

    def wait_tier(tier):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                ready = requests.get(base_url + '/health/ready', timeout=2)
                response = requests.get(base_url + '/api/license', timeout=2)
                if ready.status_code == 200 and response.status_code == 200 and response.json()['tier'] == tier:
                    return
            except (requests.RequestException, ValueError, KeyError):
                pass
            time.sleep(0.25)
        raise RuntimeError(f'Candidate did not become ready with {tier} license')

    wait_tier('community')
    # docker exec omits the entrypoint's exports. Read only these non-secret
    # startup facts from the live API, as its own UID, before stopping it.
    backend_pid = int(command('supervisorctl', '-c', '/etc/supervisor/conf.d/odin.conf', 'pid', 'backend').stdout.strip())
    if backend_pid <= 0:
        raise RuntimeError('Candidate backend is not running')
    startup_names = ('ODIN_AMS_RUNTIME_READY', 'ODIN_BOOT_ID',
                     'ODIN_BRANDING_DIRECTORY', 'ODIN_CAMERA_RELAY_SECRET_ROTATED')
    startup = json.loads(command('/usr/sbin/runuser', '-u', 'odin', '--', 'python3', '-c',
        "import json; from pathlib import Path; "
        f"env = dict(x.split(b'=', 1) for x in Path('/proc/{backend_pid}/environ').read_bytes().split(b'\\0') if b'=' in x); "
        f"print(json.dumps({{k: env[k.encode()].decode() for k in {startup_names!r} if k.encode() in env}}))"
    ).stdout)
    startup_args = ['-e', 'ODIN_DB_ROLE=api']
    for name in startup_names:
        if name in startup:
            startup_args.extend(['-e', name + '=' + startup[name]])
    command('supervisorctl', '-c', '/etc/supervisor/conf.d/odin.conf', 'stop', 'backend')
    process = None
    try:
        process = subprocess.Popen(
            ['docker', 'exec', '--user', 'odin', '-i', *startup_args, container, 'python3', '-'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        process.stdin.write(Path(__file__).read_text())
        process.stdin.close()
        wait_tier('pro')
        yield
    finally:
        try:
            command('python3', '-c',
                    "import os, signal; from pathlib import Path; "
                    "p = Path('/tmp/odin-candidate-licensed.pid'); "
                    "pid = int(p.read_text()) if p.exists() else None; "
                    "os.kill(pid, signal.SIGTERM) if pid else None")
            if process is not None:
                process.wait(timeout=40)
                if process.returncode != 0:
                    raise RuntimeError('Licensed candidate backend exited unsuccessfully')
        finally:
            command('supervisorctl', '-c', '/etc/supervisor/conf.d/odin.conf', 'start', 'backend')
            wait_tier('community')


if __name__ == '__main__':
    serve_candidate()
