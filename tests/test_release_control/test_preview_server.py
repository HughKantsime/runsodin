"""A browser check only reaches a preview this run started, and leaves nothing listening."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ops.edu_readiness import preview_server
from ops.edu_readiness.preview_server import PreviewError, local_address, owned_preview, vite_command

ROOT = Path(__file__).parents[2]

SERVER = textwrap.dedent('''
    import http.server, json, os, signal, sys
    port, record, ignore_term = int(sys.argv[1]), sys.argv[2], sys.argv[3] == 'ignore-term'
    if ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
        def log_message(self, *_):
            pass
    server = http.server.HTTPServer(('127.0.0.1', port), Handler)
    json.dump({'pid': os.getpid(), 'tracking': os.environ.get('RUNNER_TRACKING_ID'), 'no_color': os.environ.get('NO_COLOR')}, open(record, 'w'))
    print(f'  Local:   http://127.0.0.1:{port}/', flush=True)
    server.serve_forever()
''')
# What npm does: a parent that dies on SIGTERM without stopping the script it started.
PARENT = textwrap.dedent('''
    import subprocess, sys
    subprocess.Popen(sys.argv[1:]).wait()
''')


def _free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphan the system hasn't reaped yet is already dead.
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith('Z')


def _gone(pid, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def launcher(tmp_path):
    """A preview command whose real server is a grandchild, as `npm run preview` makes it."""
    server = tmp_path / 'server.py'
    server.write_text(SERVER)
    parent = tmp_path / 'parent.py'
    parent.write_text(PARENT)
    record = tmp_path / 'server.json'

    def command(kind, port, term='honour-term'):
        script = [sys.executable, str(server), str(port), str(record), term]
        if kind == 'parent':
            return [sys.executable, str(parent), *script]
        (tmp_path / 'package.json').write_text(json.dumps({'name': 'probe', 'version': '1.0.0', 'scripts': {
            'preview': ' '.join(script)}}))
        return ['npm', '--prefix', str(tmp_path), 'run', 'preview']

    command.record = record
    return command


@pytest.mark.parametrize('kind', ['parent', 'npm'])
def test_stops_the_server_behind_the_launcher(launcher, kind, monkeypatch):
    if kind == 'npm':
        assert shutil.which('npm'), 'npm is required for the real launcher contract'
    monkeypatch.setenv('RUNNER_TRACKING_ID', 'github_synthetic')
    port = _free_port()
    with owned_preview(f'http://127.0.0.1:{port}', command=launcher(kind, port)):
        seen = json.loads(launcher.record.read_text())
        assert preview_server._serves('127.0.0.1', port)
        assert _alive(seen['pid'])
    assert _gone(seen['pid'])
    assert not preview_server._listening('127.0.0.1', port)
    # The runner finds a finished job's strays by this variable, so the server must inherit it.
    assert seen['tracking'] == 'github_synthetic'
    assert seen['no_color'] == '1'


def test_server_ignoring_term_is_killed(launcher):
    port = _free_port()
    command = launcher('parent', port, 'ignore-term')
    with owned_preview(f'http://127.0.0.1:{port}', command=command, stop_seconds=0.5):
        seen = json.loads(launcher.record.read_text())
    assert _gone(seen['pid'])
    assert not preview_server._listening('127.0.0.1', port)


def test_terminated_gate_still_stops_its_server(launcher, tmp_path):
    port = _free_port()
    driver = textwrap.dedent(f'''
        import json, time
        from ops.edu_readiness.preview_server import owned_preview
        with owned_preview('http://127.0.0.1:{port}', command={json.dumps(launcher('parent', port))}):
            print('ready', flush=True)
            time.sleep(60)
    ''')
    process = subprocess.Popen([sys.executable, '-c', driver], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == 'ready'
    seen = json.loads(launcher.record.read_text())
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=15)
    assert _gone(seen['pid'])
    assert not preview_server._listening('127.0.0.1', port)


@pytest.fixture
def stale_listener():
    """An earlier run's server, answering every request with 200."""
    server = subprocess.Popen(
        [sys.executable, '-c', textwrap.dedent('''
            import http.server, sys
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.end_headers(); self.wfile.write(b'stale')
                def log_message(self, *_):
                    pass
            server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
            print(server.server_address[1], flush=True)
            server.serve_forever()
        ''')],
        stdout=subprocess.PIPE, text=True,
    )
    port = int(server.stdout.readline())
    yield port
    server.kill()
    server.wait()


def test_refuses_a_listener_it_did_not_start(stale_listener, launcher):
    command = launcher('parent', stale_listener)
    with pytest.raises(PreviewError, match='already has a listener'):
        with owned_preview(f'http://127.0.0.1:{stale_listener}', command=command):
            pytest.fail('the check ran against a server this run did not start')
    assert not launcher.record.exists()
    assert preview_server._listening('127.0.0.1', stale_listener)  # refused, not removed


def test_listener_that_appears_after_the_check_is_not_mistaken_for_this_run(stale_listener, monkeypatch):
    """Something else answers 200, but this run's server never announced its bind."""
    monkeypatch.setattr(preview_server, '_listening', lambda *_: False)
    silent = [sys.executable, '-c', 'import time; time.sleep(60)']
    with pytest.raises(PreviewError, match='not ready'):
        with owned_preview(f'http://127.0.0.1:{stale_listener}', command=silent, ready_seconds=0.5):
            pytest.fail('the check ran against a server this run did not start')


def test_taken_port_exit_fails_the_check():
    port = _free_port()
    busy = [sys.executable, '-c', 'import sys; print("Port is already in use"); sys.exit(1)']
    with pytest.raises(PreviewError, match='already in use'):
        with owned_preview(f'http://127.0.0.1:{port}', command=busy):
            pytest.fail('the check ran without a server')


def test_vite_is_told_to_die_rather_than_pick_another_port():
    command = vite_command(4173)
    assert command[:5] == ['npm', '--prefix', str(ROOT / 'frontend'), 'run', 'preview']
    assert command[command.index('--port') + 1] == '4173'
    assert command[command.index('--host') + 1] == '127.0.0.1'
    assert '--strictPort' in command


@pytest.mark.parametrize('url', ['https://127.0.0.1:4173', 'http://example.test:4173', 'http://127.0.0.1'])
def test_only_a_local_http_url_with_a_port_is_accepted(url):
    with pytest.raises(ValueError):
        local_address(url)


def _stub_tools(tmp_path):
    """npm builds nothing and no browser check runs: the gate must stop at the listener."""
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    marker = tmp_path / 'browser-check-ran'
    (stubs / 'npm').write_text('#!/bin/sh\nexit 0\n')
    (stubs / 'node').write_text(f'#!/bin/sh\ntouch {marker}\nexit 0\n')
    for name in ('npm', 'node'):
        (stubs / name).chmod(0o755)
    return stubs, marker


def _gate_env(stubs, port, **extra):
    return {'PATH': f'{stubs}:{os.environ["PATH"]}', 'EDU_FRONTEND_URL': f'http://127.0.0.1:{port}', **extra}


def test_privacy_gate_fails_on_a_stale_listener(stale_listener, tmp_path):
    stubs, marker = _stub_tools(tmp_path)
    result = subprocess.run(
        [sys.executable, 'ops/edu_readiness/run_privacy_browser.py', '--run-dir', str(tmp_path / 'run')],
        cwd=ROOT, env=_gate_env(stubs, stale_listener), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0 and 'already has a listener' in result.stderr
    assert not marker.exists()


def test_accessibility_gate_fails_on_a_stale_listener(stale_listener, tmp_path):
    stubs, marker = _stub_tools(tmp_path)
    run_dir = tmp_path / 'run'
    result = subprocess.run(
        [sys.executable, 'ops/edu_readiness/run_accessibility.py', '--run-id', '20261030T000000Z-abcdef1', '--run-dir', str(run_dir)],
        cwd=ROOT, env=_gate_env(stubs, stale_listener), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert not marker.exists()
    evidence = json.loads((run_dir / 'accessibility.json').read_text())
    assert evidence['status'] == 'fail'
    assert any('already has a listener' in finding for finding in evidence['findings'])


def test_edu_browser_gate_fails_on_a_stale_listener(stale_listener, tmp_path):
    marker = tmp_path / 'browser-check-ran'
    browser_python = tmp_path / 'python'
    # Delegates the helper to the real interpreter and records any attempt to run the check.
    browser_python.write_text(textwrap.dedent(f'''\
        #!/bin/sh
        case "$1" in
          ops/edu_readiness/preview_server.py) exec "{sys.executable}" "$@" ;;
          *) touch "{marker}"; exit 0 ;;
        esac
    '''))
    browser_python.chmod(0o755)
    result = subprocess.run(
        ['ops/demo/run_edu_browser_gate.sh'], cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, 'EDU_BROWSER_PYTHON': str(browser_python), 'EDU_FRONTEND_URL': f'http://127.0.0.1:{stale_listener}'},
    )
    assert result.returncode != 0 and 'already has a listener' in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize('path', [
    'ops/demo/run_edu_browser_gate.sh', 'ops/edu_readiness/run_accessibility.py', 'ops/edu_readiness/run_privacy_browser.py',
])
def test_browser_gates_have_no_path_that_reuses_a_running_server(path):
    source = (ROOT / path).read_text()
    assert 'preview_server' in source
    for forbidden in ('urlopen', 'run preview', '"preview"', 'curl'):
        assert forbidden not in source
