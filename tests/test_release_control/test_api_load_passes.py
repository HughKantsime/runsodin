"""api_load times requests with no tracer running and measures memory in a separate traced pass.

tracemalloc made the timed latencies two or more times slower, with a tail nothing else had. These
tests run the real gate logic and, for the memory pass, a real fresh subprocess. Only the workload
is stubbed: the real one is what the gate itself runs."""
import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
import tracemalloc
from pathlib import Path

import pytest

from ops.edu_readiness import api_load

ROOT = Path(__file__).parents[2]
RUN_ID = '20261030T000000Z-abcdef1'
MAC = {'system': 'Darwin', 'machine': 'arm64', 'hostname': 'ollamas-Mac-mini.local', 'cpu': 'Apple M4'}
CLASSES = ('auth', 'printers', 'jobs', 'reports', 'job_create', 'job_approve', 'session_churn')
MIB = 1024 * 1024


def _thresholds():
    return json.loads(api_load.THRESHOLDS_PATH.read_text(encoding='utf-8'))


def _timings(milliseconds=100.0):
    timings = {name: [float(milliseconds)] * (200 if name in ('auth', 'printers', 'jobs', 'reports') else 67) for name in CLASSES}
    timings['session_churn'] = timings['session_churn'][:66]
    return timings


def _websocket(cycles):
    return {'timings': [1.0] * cycles, 'failures': 0, 'subscription_acks': cycles * 2,
            'messages_exchanged': cycles * 9, 'isolation_checks': cycles, 'cross_user_events': 0}


def _report(**changes):
    """What a healthy memory pass writes, built by the gate's own metric code."""
    thresholds = _thresholds()
    cycles = thresholds['websocket_cycles_per_repetition']
    repetitions = [
        api_load._repetition_metric(number, _timings(), [], 0, 1.0, _websocket(cycles), True)
        for number in range(thresholds['repetitions'])
    ]
    report = {'tracing': True, 'memory_delta_bytes': 1024, 'warmup_errors': 0, 'warmup_tenant_leaks': 0,
              'integrity_check': 'ok', 'fd_delta': 0, 'repetitions': repetitions}
    report.update(changes)
    return report


@pytest.fixture
def timed_pass(monkeypatch):
    """The parent's pass, with a stubbed workload that notes whether a tracer was running."""
    seen = {'tracing': [], 'memory_runs': []}

    async def execute(app, tokens, operations, concurrency):
        seen['tracing'].append(tracemalloc.is_tracing())
        return _timings(), [], 0, 1.0

    def websocket(app, target, foreign, cycles):
        seen['tracing'].append(tracemalloc.is_tracing())
        return _websocket(cycles)

    monkeypatch.setattr(api_load, 'host_facts', lambda: MAC)
    monkeypatch.setattr(api_load, '_prepare_database', lambda path: (None, {
        'churn': ['x'] * 150, 'ws_target': ['x'], 'ws_foreign': ['x'], 'viewer': ['x']}))
    monkeypatch.setattr(api_load, '_execute_repetition', execute)
    monkeypatch.setattr(api_load, '_websocket_cycles', websocket)
    return seen


def _gate(tmp_path):
    output = tmp_path / 'api_load.json'
    code = asyncio.run(api_load.run(RUN_ID, output))
    return code, json.loads(output.read_text(encoding='utf-8'))


def _memory_pass_returns(monkeypatch, timed_pass, report):
    def run(environment):
        timed_pass['memory_runs'].append(environment)
        return {'exit_code': 0, 'failure': None, 'duration_seconds': 1.0, 'report': report}
    monkeypatch.setattr(api_load, '_run_memory_pass', run)


# ---------------- the timed pass ----------------

def test_no_tracer_is_active_during_any_timed_repetition(monkeypatch, tmp_path, timed_pass):
    _memory_pass_returns(monkeypatch, timed_pass, _report())
    code, result = _gate(tmp_path)
    assert code == 0, result['findings']
    # warmup plus each repetition's requests and websocket cycles
    assert timed_pass['tracing'] == [False] * (1 + 2 * _thresholds()['repetitions'])
    assert result['metrics']['latency_traced'] is False
    assert [metric['traced'] for metric in result['metrics']['repetitions']] == [False] * 3


def test_a_tracer_already_running_fails_the_gate(monkeypatch, tmp_path, timed_pass):
    """PYTHONTRACEMALLOC or -X tracemalloc would quietly bring the old latencies back."""
    _memory_pass_returns(monkeypatch, timed_pass, _report())
    tracemalloc.start()
    try:
        code, result = _gate(tmp_path)
    finally:
        tracemalloc.stop()
    assert code == 1
    assert 'latency was measured under a tracer' in result['findings']
    assert result['metrics']['latency_traced'] is True


def test_the_memory_pass_runs_once_after_the_timed_pass(monkeypatch, tmp_path, timed_pass):
    order = []

    def run(environment):
        order.append(len(timed_pass['tracing']))
        return {'exit_code': 0, 'failure': None, 'duration_seconds': 1.0, 'report': _report()}

    monkeypatch.setattr(api_load, '_run_memory_pass', run)
    code, result = _gate(tmp_path)
    assert code == 0, result['findings']
    assert order == [1 + 2 * _thresholds()['repetitions']]


def test_result_records_which_pass_measured_what(monkeypatch, tmp_path, timed_pass):
    _memory_pass_returns(monkeypatch, timed_pass, _report(memory_delta_bytes=4096))
    code, result = _gate(tmp_path)
    metrics = result['metrics']
    assert code == 0, result['findings']
    assert metrics['memory_delta_bytes'] == 4096
    assert metrics['latency_traced'] is False and metrics['memory_delta_source'] == 'memory_pass'
    assert metrics['memory_pass']['process'] == 'fresh subprocess' and metrics['memory_pass']['traced'] is True
    assert metrics['memory_pass']['latencies_judged'] is False and metrics['memory_pass']['status'] == 'pass'
    assert metrics['memory_pass']['timeout_seconds'] == api_load.MEMORY_PASS_TIMEOUT_SECONDS
    assert [row['repetition'] for row in metrics['memory_pass']['repetitions']] == [1, 2, 3]
    # One result file, as before: the memory pass's own report isn't left for evidence to pick up.
    assert [path.name for path in tmp_path.iterdir()] == ['api_load.json']


def test_a_slow_timed_pass_still_fails_on_its_own_latency(monkeypatch, tmp_path, timed_pass):
    """Moving the tracer must not stop timing from being judged."""
    async def slow(app, tokens, operations, concurrency):
        return _timings(100000.0), [], 0, 1.0

    monkeypatch.setattr(api_load, '_execute_repetition', slow)
    _memory_pass_returns(monkeypatch, timed_pass, _report())
    code, result = _gate(tmp_path)
    assert code == 1
    assert 'repetition 1: read p95' in result['findings'] and 'repetition 3: global p99' in result['findings']


def test_file_descriptor_growth_in_the_timed_pass_still_fails(monkeypatch, tmp_path, timed_pass):
    counts = iter([10, 10 + _thresholds()['maximum_fd_delta'] + 1])
    monkeypatch.setattr(api_load, '_fd_count', lambda: next(counts))
    _memory_pass_returns(monkeypatch, timed_pass, _report(fd_delta=0))
    code, result = _gate(tmp_path)
    assert code == 1 and 'file descriptor growth exceeded threshold' in result['findings']
    assert result['metrics']['fd_delta'] == _thresholds()['maximum_fd_delta'] + 1


def test_integrity_failure_in_the_timed_pass_still_fails(monkeypatch, tmp_path, timed_pass):
    class Connection:
        def execute(self, statement):
            assert statement == 'PRAGMA integrity_check'
            return self

        def fetchone(self):
            return ('database disk image is malformed',)

        def close(self):
            pass

    monkeypatch.setattr(api_load.sqlite3, 'connect', lambda path: Connection())
    _memory_pass_returns(monkeypatch, timed_pass, _report())
    code, result = _gate(tmp_path)
    assert code == 1 and 'post-run SQLite integrity check failed' in result['findings']
    assert result['metrics']['integrity_check'] == 'database disk image is malformed'


# ---------------- the memory pass, in a real fresh subprocess ----------------

# Runs the real pass entry point with a stubbed workload, so the tracer, baseline and report are real.
REAL_PASS = textwrap.dedent('''
    import sys
    sys.path.insert(0, sys.argv[1])
    from ops.edu_readiness import api_load
    mode, report = sys.argv[2:4]
    kept = []
    CLASSES = ('auth', 'printers', 'jobs', 'reports', 'job_create', 'job_approve', 'session_churn')
    milliseconds = 100000.0 if mode == 'slow' else 100.0
    timings = {name: [milliseconds] * (200 if name in CLASSES[:4] else 67) for name in CLASSES}
    timings['session_churn'] = timings['session_churn'][:66]

    async def execute(app, tokens, operations, concurrency):
        warmup = len(operations) == 100
        if mode == 'leak-over' and not warmup:
            kept.append(bytes(30 * 1024 * 1024))
        if mode == 'leak-under' and not warmup:
            kept.append(bytes(20 * 1024 * 1024))
        if mode == 'leak-in-warmup' and warmup:
            kept.append(bytes(90 * 1024 * 1024))
        if mode == 'crash' and not warmup:
            raise RuntimeError('synthetic crash')
        errors = [{'category': 'jobs', 'status': 500}] if mode == 'errors' and not warmup else []
        leaks = 1 if mode == 'tenant-leak' and not warmup else 0
        return timings, errors, leaks, 1.0

    def websocket(app, target, foreign, cycles):
        return {'timings': [1.0] * cycles, 'failures': 0, 'subscription_acks': cycles * 2,
                'messages_exchanged': cycles * 9, 'isolation_checks': cycles, 'cross_user_events': 0}

    api_load._prepare_database = lambda path: (None, {
        'churn': ['x'] * 150, 'ws_target': ['x'], 'ws_foreign': ['x'], 'viewer': ['x']})
    api_load._execute_repetition = execute
    api_load._websocket_cycles = websocket
    sys.exit(api_load.main(['--memory-pass-output', report]))
''')


def _real_pass(monkeypatch, mode):
    monkeypatch.setattr(api_load, '_memory_pass_command',
                        lambda report: [sys.executable, '-c', REAL_PASS, str(ROOT), mode, str(report)])


def _child(monkeypatch, body):
    """A stand-in memory pass: sys.argv[1] is where its report would go."""
    monkeypatch.setattr(api_load, '_memory_pass_command',
                        lambda report: [sys.executable, '-c', textwrap.dedent(body), str(report)])


def test_real_pass_is_traced_and_measures_from_after_warmup(monkeypatch, tmp_path, timed_pass):
    _real_pass(monkeypatch, 'clean')
    code, result = _gate(tmp_path)
    metrics = result['metrics']
    assert code == 0, result['findings']
    assert type(metrics['memory_delta_bytes']) is int
    assert metrics['memory_pass']['traced'] is True and metrics['memory_pass']['exit_code'] == 0
    assert [row['operations'] for row in metrics['memory_pass']['repetitions']] == [1000, 1000, 1000]


def test_a_leak_over_the_limit_in_the_memory_pass_fails_the_gate(monkeypatch, tmp_path, timed_pass):
    assert _thresholds()['maximum_memory_delta_mib'] == 64
    _real_pass(monkeypatch, 'leak-over')
    code, result = _gate(tmp_path)
    assert code == 1 and result['status'] == 'fail'
    assert 'tracked memory growth exceeded threshold' in result['findings']
    assert result['metrics']['memory_delta_bytes'] >= 90 * MIB


def test_a_leak_under_the_limit_passes_and_is_reported(monkeypatch, tmp_path, timed_pass):
    _real_pass(monkeypatch, 'leak-under')
    code, result = _gate(tmp_path)
    assert code == 0, result['findings']
    assert 60 * MIB <= result['metrics']['memory_delta_bytes'] <= 64 * MIB


def test_allocations_during_warmup_stay_before_the_baseline(monkeypatch, tmp_path, timed_pass):
    """The same baseline point as before: warmup caches aren't a leak."""
    _real_pass(monkeypatch, 'leak-in-warmup')
    code, result = _gate(tmp_path)
    assert code == 0, result['findings']
    assert result['metrics']['memory_delta_bytes'] < 8 * MIB


def test_the_memory_pass_latencies_are_not_judged(monkeypatch, tmp_path, timed_pass):
    _real_pass(monkeypatch, 'slow')
    code, result = _gate(tmp_path)
    assert code == 0, result['findings']
    assert all(metric['read_p95_ms'] == 100.0 for metric in result['metrics']['repetitions'])


@pytest.mark.parametrize('mode,finding', [
    ('errors', 'memory pass repetition 1: errors'),
    ('errors', 'memory pass repetition 2: 5xx'),
    ('tenant-leak', 'memory pass repetition 3: tenant leaks'),
])
def test_errors_in_the_real_memory_pass_fail_the_gate(monkeypatch, tmp_path, timed_pass, mode, finding):
    _real_pass(monkeypatch, mode)
    code, result = _gate(tmp_path)
    assert code == 1 and finding in result['findings']
    assert result['metrics']['memory_pass']['status'] == 'fail'


def test_a_crash_in_the_real_memory_pass_fails_the_gate(monkeypatch, tmp_path, timed_pass):
    _real_pass(monkeypatch, 'crash')
    code, result = _gate(tmp_path)
    assert code == 1
    assert 'memory pass: exited with 1' in result['findings']
    assert result['metrics']['memory_delta_bytes'] is None


# ---------------- the memory pass failing closed ----------------

@pytest.mark.parametrize('body,finding', [
    ('import sys; sys.exit(3)', 'memory pass: exited with 3'),
    ('import os, signal; os.kill(os.getpid(), signal.SIGKILL)', 'memory pass: exited with -9'),
    ('pass', 'memory pass: wrote no usable report'),
    ('import sys; open(sys.argv[1], "w").write("not json")', 'memory pass: wrote no usable report'),
    ('import sys; open(sys.argv[1], "w").write(\'{"memory_delta_bytes": 1\')', 'memory pass: wrote no usable report'),
    ('import sys; open(sys.argv[1], "w").write("")', 'memory pass: wrote no usable report'),
    ('import sys; open(sys.argv[1], "w").write("[]")', 'memory pass: its report is incomplete'),
    ('import sys; open(sys.argv[1], "w").write("null")', 'memory pass: its report is incomplete'),
    ('import sys; open(sys.argv[1], "w").write("{}")', 'memory pass: its report is incomplete'),
], ids=['exit-3', 'killed', 'skipped-no-report', 'garbage', 'truncated', 'empty', 'list', 'null', 'empty-object'])
def test_a_crashed_skipped_or_unreadable_memory_pass_fails_the_gate(monkeypatch, tmp_path, timed_pass, body, finding):
    _child(monkeypatch, body)
    code, result = _gate(tmp_path)
    assert code == 1 and result['status'] == 'fail'
    assert finding in result['findings']
    assert result['metrics']['memory_delta_bytes'] is None
    assert result['metrics']['memory_pass']['status'] == 'fail'


def test_a_good_report_with_a_failed_exit_still_fails_the_gate(monkeypatch, tmp_path, timed_pass):
    report = json.dumps(_report())
    _child(monkeypatch, f'''
        import sys
        open(sys.argv[1], "w").write({report!r})
        sys.exit(2)
    ''')
    code, result = _gate(tmp_path)
    assert code == 1 and 'memory pass: exited with 2' in result['findings']


def test_a_hung_memory_pass_is_killed_and_fails_the_gate(monkeypatch, tmp_path, timed_pass):
    """A pass that would finish well if left alone: only the bound can stop this from passing."""
    pid_file = tmp_path / 'pass-child.pid'
    report = json.dumps(_report())
    _child(monkeypatch, f'''
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pid_file)!r}, "w").write(str(child.pid))
        time.sleep(8)
        open(sys.argv[1], "w").write({report!r})
    ''')
    monkeypatch.setattr(api_load, 'MEMORY_PASS_TIMEOUT_SECONDS', 2)
    started = time.monotonic()
    code, result = _gate(tmp_path)
    assert code == 1
    assert 'memory pass: timed out after 2 s' in result['findings']
    assert time.monotonic() - started < 7
    assert result['metrics']['memory_delta_bytes'] is None
    # Its own children go with it, so a hung pass can't keep running after the gate has failed.
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith('Z'):
            break
        time.sleep(0.05)
    else:
        pytest.fail('the memory pass left a process running')


def test_a_memory_pass_that_cannot_start_fails_the_gate(monkeypatch, tmp_path, timed_pass):
    monkeypatch.setattr(api_load, '_memory_pass_command', lambda report: [str(tmp_path / 'no-such-interpreter')])
    code, result = _gate(tmp_path)
    assert code == 1
    assert any(finding.startswith('memory pass: could not start') for finding in result['findings'])


@pytest.mark.parametrize('name,changes,finding', [
    ('no-tracer', {'tracing': False}, 'memory pass: ran without its tracer'),
    ('warmup-errors', {'warmup_errors': 1}, 'memory pass: warmup returned 1 unexpected responses'),
    ('warmup-leak', {'warmup_tenant_leaks': 1}, 'memory pass: warmup exposed 1 cross-tenant records'),
    ('integrity', {'integrity_check': 'malformed'}, 'memory pass: post-run SQLite integrity check failed'),
    ('short', {'repetitions': []}, 'memory pass: ran 0 of 3 repetitions'),
    ('delta-string', {'memory_delta_bytes': '1024'}, 'memory pass: its report is incomplete'),
    ('delta-bool', {'memory_delta_bytes': False}, 'memory pass: its report is incomplete'),
    ('delta-null', {'memory_delta_bytes': None}, 'memory pass: its report is incomplete'),
    ('delta-float', {'memory_delta_bytes': 1.5}, 'memory pass: its report is incomplete'),
], ids=lambda value: value if isinstance(value, str) and ':' not in value else '')
def test_a_memory_pass_report_that_shows_a_problem_fails_the_gate(monkeypatch, tmp_path, timed_pass, name, changes, finding):
    _memory_pass_returns(monkeypatch, timed_pass, _report(**changes))
    code, result = _gate(tmp_path)
    assert code == 1 and finding in result['findings']


@pytest.mark.parametrize('key,value,finding', [
    ('errors', 1, 'errors'),
    ('unexpected_5xx', 1, '5xx'),
    ('tenant_leaks', 1, 'tenant leaks'),
    ('operations', 999, 'operation count'),
    ('websocket_failures', 1, 'websocket failures'),
    ('websocket_cross_user_events', 1, 'websocket cross-user isolation'),
    ('traced', False, 'ran without its tracer'),
])
def test_a_problem_in_any_memory_pass_repetition_fails_the_gate(monkeypatch, tmp_path, timed_pass, key, value, finding):
    report = _report()
    report['repetitions'][1][key] = value
    _memory_pass_returns(monkeypatch, timed_pass, report)
    code, result = _gate(tmp_path)
    assert code == 1 and f'memory pass repetition 2: {finding}' in result['findings']


def test_an_untraced_memory_pass_gives_no_figure_to_judge(monkeypatch, tmp_path, timed_pass):
    _memory_pass_returns(monkeypatch, timed_pass, _report(tracing=False, memory_delta_bytes=0))
    code, result = _gate(tmp_path)
    assert code == 1 and result['metrics']['memory_delta_bytes'] is None


def test_the_memory_pass_gets_the_environment_from_before_the_fixture(monkeypatch, tmp_path, timed_pass):
    """The fixture points this process at its own database, which must not reach the other pass."""
    monkeypatch.delenv('DATABASE_URL', raising=False)

    def fixture(path):
        monkeypatch.setitem(os.environ, 'DATABASE_URL', 'sqlite:///timed-pass.db')
        return None, {'churn': ['x'] * 150, 'ws_target': ['x'], 'ws_foreign': ['x'], 'viewer': ['x']}

    monkeypatch.setattr(api_load, '_prepare_database', fixture)
    _memory_pass_returns(monkeypatch, timed_pass, _report())
    code, result = _gate(tmp_path)
    assert code == 0, result['findings']
    assert 'DATABASE_URL' not in timed_pass['memory_runs'][0]


def test_the_memory_pass_is_this_script_in_a_new_process():
    command = api_load._memory_pass_command(Path('/x/report.json'))
    assert command == [sys.executable, str(Path(api_load.__file__).resolve()), '--memory-pass-output', '/x/report.json']


def test_the_timed_pass_needs_its_own_arguments():
    with pytest.raises(SystemExit):
        api_load.main([])
    with pytest.raises(SystemExit):
        api_load.main(['--run-id', RUN_ID])
