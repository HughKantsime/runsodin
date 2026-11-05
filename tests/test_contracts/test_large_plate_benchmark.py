"""Portable, no-hardware benchmark entry point and measurement units."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from ops import large_plate_benchmark as benchmark


@pytest.mark.parametrize('platform,multiplier', [('darwin', 1), ('linux', 1024)])
def test_rss_units(monkeypatch, platform, multiplier):
    import resource
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(resource, 'getrusage', lambda who: SimpleNamespace(ru_maxrss=123))
    assert benchmark.peak_rss_bytes() == 123 * multiplier


@pytest.mark.parametrize('size', ['0', '257', '-1', 'abc'])
def test_cli_rejects_invalid_size(size):
    with pytest.raises(SystemExit) as error:
        benchmark.main(['--gcode-mib', size])
    assert error.value.code == 2


def test_cli_propagates_failed_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout='dispatch failed'))
    assert benchmark.main(['--gcode-mib', '1']) == 1
    captured = capsys.readouterr()
    assert not captured.out and 'dispatch failed' in captured.err


def test_cli_measures_real_dispatches_from_another_directory(tmp_path):
    command = [sys.executable, str(Path(benchmark.__file__).resolve()), '--gcode-mib', '1']
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['gcode_mib'] == 1 and data['dispatch_inspections'] == 2
    assert all(result[0] for result in data['results'])
    assert data['peak_rss_bytes'] > 1024**2


def test_cli_forwards_requested_size(monkeypatch, capsys):
    def run(command, **kwargs):
        assert kwargs['env']['ODIN_LARGE_PLATE_BENCHMARK'] == '154'
        assert command[0] == sys.executable and command[1:3] == ['-m', 'pytest']
        return SimpleNamespace(returncode=0, stdout='LARGE_PLATE_BENCHMARK={"gcode_mib":154}\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert benchmark.main(['--gcode-mib', '154']) == 0
    assert json.loads(capsys.readouterr().out) == {'gcode_mib': 154}
