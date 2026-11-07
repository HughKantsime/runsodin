"""Censuses exclude released source copies while continuing to scan live code."""
from pathlib import Path

from tests import _frozen_bundles as frozen


def test_exclusions_match_only_packaged_source_copies():
    assert frozen.BACKEND / 'core/schema/preserved053/overlay' in frozen.FROZEN_BUNDLES
    for path in frozen.BACKEND.rglob('*.py'):
        parts = path.relative_to(frozen.BACKEND).parts
        expected = (len(parts) > 4 and parts[:2] == ('core', 'schema')
                    and parts[2].startswith('preserved') and parts[3] in ('bundle', 'overlay'))
        assert frozen.is_frozen_source(path) == expected, path


def test_future_bundle_does_not_hide_bridges_or_similarly_named_live_code(tmp_path, monkeypatch):
    historical = [
        'core/schema/preserved999/bundle/modules/jobs/old.py',
        'core/schema/preserved999/overlay/modules/jobs/old.py',
    ]
    live = [
        'core/schema/preserved999/runner.py',
        'core/schema/preserved999/integrity.py',
        'core/schema/preserved999/__init__.py',
        'core/schema/preserved999/overlay_extra/live.py',
        'core/schema/migrations/live.py',
        'modules/preserved999/overlay/live.py',
        'modules/jobs/live.py',
    ]
    for name in historical + live:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(frozen, 'FROZEN_BUNDLES', frozen.frozen_bundles(tmp_path))
    assert all(frozen.is_frozen_source(tmp_path / name) for name in historical)
    assert not any(frozen.is_frozen_source(tmp_path / name) for name in live)

    # Exercise the census itself: a future bundle's writer disappears, live writers remain.
    from tests.test_contracts import job_census
    for name in historical + live:
        (tmp_path / name).write_text("sql = 'UPDATE jobs SET status=:status WHERE id=:id'\n")
    monkeypatch.setattr(job_census, 'ROOT', tmp_path.parent)
    monkeypatch.setattr(job_census, 'BACKEND', tmp_path)
    sources = job_census.backend_sources()
    assert set(sources) == {(Path(tmp_path.name) / name).as_posix() for name in live}
