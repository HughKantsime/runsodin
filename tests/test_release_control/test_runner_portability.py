"""Portability contracts without starting browsers, brokers, mounts, or Docker."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.release_control import aggregate

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('platform,xdg,override,expected', [
    ('linux', None, None, '/home/odin/.cache/ms-playwright'),
    ('linux', '/cache', None, '/cache/ms-playwright'),
    ('darwin', None, None, '/home/odin/Library/Caches/ms-playwright'),
    ('linux', None, '/browsers', '/browsers'),
    ('darwin', None, '/browsers', '/browsers'),
])
def test_browser_cache_is_resolved_before_home_isolation(monkeypatch, platform, xdg, override, expected):
    monkeypatch.setattr(aggregate.sys, 'platform', platform)
    monkeypatch.setattr(Path, 'home', lambda: Path('/home/odin'))
    for key, value in [('XDG_CACHE_HOME', xdg), ('PLAYWRIGHT_BROWSERS_PATH', override)]:
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    assert aggregate.playwright_browsers_path() == expected


@pytest.mark.parametrize('free,passes', [(4 * 1024**3, True), (1024**3, False), (1000000 * 1024**3, False)])
def test_linux_browser_upload_fixture_uses_real_filesystem_without_hdiutil(tmp_path, free, passes):
    # Load only the fixture definition: importing its module would load Playwright.
    source = ROOT / 'tests/education_browser/test_pilot_journey.py'
    node = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == 'upload_volume')
    node.decorator_list = []
    namespace = {'sys': SimpleNamespace(platform='linux'), 'shutil': SimpleNamespace(disk_usage=lambda _: SimpleNamespace(free=free))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    fixture = namespace['upload_volume'](tmp_path)
    if passes:
        assert next(fixture) == tmp_path / 'upload-volume'
        with pytest.raises(StopIteration):
            next(fixture)
    else:
        with pytest.raises(AssertionError, match='CV06 needs'):
            next(fixture)
