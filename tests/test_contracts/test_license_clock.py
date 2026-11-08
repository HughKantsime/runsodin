"""Slice A: real durable evidence and injectable wall/monotonic clocks."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, text, event

from core.license_clock import LicenseClock, TimeStore, KEY, FILENAME, INTERVAL
from tests.test_contracts.test_license_user_limits import signed_license

BASE = datetime(2026, 11, 8, tzinfo=timezone.utc)


@pytest.fixture
def clock_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'clock.db'}")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE system_config (key TEXT PRIMARY KEY, value JSON NOT NULL)"))
    wall, tick = [BASE], [0.0]
    store = TimeStore(tmp_path, lambda: engine)
    def new_clock():
        return LicenseClock(store, wall=lambda: wall[0], monotonic=lambda: tick[0])
    yield wall, tick, store, new_clock, engine
    engine.dispose()


def test_fresh_install_and_exact_skew_boundary(clock_env):
    wall, tick, store, new, _ = clock_env
    clock = new()
    assert clock.sample(persist=True) == (BASE, False)
    wall[0] -= timedelta(hours=24)
    assert clock.sample()[1] is False
    wall[0] -= timedelta(microseconds=1)
    assert clock.sample()[1] is True
    assert '24 hours' in clock.error()


def test_signed_issue_time_with_skew_tolerance(clock_env):
    wall, tick, store, new, _ = clock_env
    clock = new()
    assert clock.error((BASE + timedelta(hours=23)).isoformat()) is None
    assert clock.error((BASE + timedelta(hours=24)).isoformat()) is None
    assert clock.error((BASE + timedelta(hours=24, seconds=1)).isoformat())


def test_monotonic_elapsed_counts_when_wall_stops_or_rolls_back(clock_env):
    wall, tick, store, new, _ = clock_env
    clock = new()
    clock.sample(persist=True)
    tick[0] += 3600
    wall[0] -= timedelta(hours=1)
    assert clock.sample()[0] == BASE + timedelta(hours=1)


@pytest.mark.parametrize('restore', ['database', 'file', 'neither'])
def test_restart_and_one_copy_restore_uses_newest(clock_env, restore):
    wall, tick, store, new, engine = clock_env
    new().sample(persist=True)
    old = BASE - timedelta(days=3)
    if restore == 'database':
        with engine.begin() as db:
            db.execute(text('UPDATE system_config SET value=:v'), {'v': json.dumps(old.isoformat())})
    if restore == 'file':
        store.path.write_text(json.dumps({'high_water': old.isoformat()}))
    wall[0] = old
    clock = new()
    assert clock.sample(persist=True) == (BASE, True)
    assert store.read() == BASE
    assert json.loads(store.path.read_text())['high_water'] == BASE.isoformat()


def test_checkpoints_throttled_and_max_merge(clock_env):
    wall, tick, store, new, engine = clock_env
    clock = new()
    clock.sample(persist=True)
    original = store.path.stat().st_mtime_ns
    tick[0] = 30
    clock.sample(persist=True)
    assert store.path.stat().st_mtime_ns == original
    tick[0] = INTERVAL
    clock.sample(persist=True)
    expected = BASE + timedelta(seconds=INTERVAL)
    store.write(BASE)  # an older worker must not rewind either store
    assert store.read() == expected
    with engine.connect() as db:
        assert json.loads(db.execute(text('SELECT value FROM system_config WHERE key=:k'), {'k': KEY}).scalar()) == expected.isoformat()


def test_corrupt_or_missing_stores_and_read_only_storage_do_not_brick(clock_env, monkeypatch):
    wall, tick, store, new, engine = clock_env
    store.path.write_text('not json')
    with engine.begin() as db:
        db.execute(text('INSERT INTO system_config VALUES (:k,:v)'), {'k': KEY, 'v': 'not json'})
    assert new().sample(persist=True) == (BASE, False)
    # No writable file destination and no database; monotonic checking survives.
    blocked = TimeStore(store.path / 'not-a-directory', lambda: None)
    assert LicenseClock(blocked, wall=lambda: BASE).sample(persist=True) == (BASE, False)


def test_database_checkpoint_does_not_wait_on_callers_write_lock(clock_env):
    wall, tick, store, new, engine = clock_env
    clock = new()
    with engine.connect() as writer:
        writer.execute(text("INSERT INTO system_config VALUES ('owned','null')"))
        # This would deadlock/time out if checkpoint waited for the caller.
        assert clock.sample(persist=True) == (BASE, False)
        assert json.loads(store.path.read_text())['high_water'] == BASE.isoformat()


def test_verification_and_cache_expire_at_exact_instant_and_cannot_revive(clock_env, tier_license, monkeypatch):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new()
    monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('pro', trial=True, issued_at=(BASE - timedelta(days=1)).isoformat(),
                 expires_at=(BASE + timedelta(seconds=60)).isoformat())
    cached = lm.get_license()
    wall[0] += timedelta(seconds=60)
    tick[0] += 60
    assert lm.load_license().expired
    expired = lm.get_license()
    assert expired is not cached and expired.expired and not expired.valid and expired.trial
    wall[0] -= timedelta(days=2)
    assert lm.load_license().expired
    assert not lm.get_license().has_feature('sso')
    assert lm.get_license().has_feature('cost_calculator')


def test_cached_license_refused_on_large_rollback_and_recovers_with_correct_clock(clock_env, tier_license, monkeypatch):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new()
    monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('pro')
    wall[0] -= timedelta(days=2)
    assert not lm.get_license().valid
    assert 'clock' in lm.get_license().error
    wall[0] = BASE
    assert lm.get_license().valid


def test_signed_issue_time_is_checked_by_verifier(clock_env, tier_license, monkeypatch):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new()
    monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('pro', issued_at=BASE.isoformat())
    wall[0] -= timedelta(days=2)
    # Fresh process and no high-water mark: signed issue time alone detects it.
    store.path.unlink()
    with clock_env[-1].begin() as db:
        db.execute(text('DELETE FROM system_config'))
    fresh = new()
    monkeypatch.setattr(lm, 'license_clock', lambda: fresh)
    assert lm.load_license().expired


@pytest.mark.parametrize('issued', [0, False, {}, 'not-a-date'])
def test_malformed_signed_issue_time_is_refused(signed_license, issued):
    _, info = signed_license(issued_at=issued)
    assert not info.valid


def test_extreme_checkpoint_is_a_recoverable_refusal_not_a_crash(clock_env):
    wall, tick, store, new, _ = clock_env
    store.write(datetime.max.replace(tzinfo=timezone.utc))
    clock = new()
    assert clock.sample()[1]
    tick[0] += 1000
    assert clock.sample(persist=True)[1]
    assert 'clock' in clock.error()


@pytest.mark.parametrize('checkpoint_blocked', [False, True])
@pytest.mark.parametrize('caller_commits', [False, True])
def test_checkpoint_only_finishes_its_own_connection(clock_env, checkpoint_blocked, caller_commits):
    _, _, store, _, engine = clock_env
    commits, rollbacks = [], []
    event.listen(engine, 'commit', commits.append)
    event.listen(engine, 'rollback', rollbacks.append)
    with engine.connect() as caller:
        if checkpoint_blocked:
            caller.execute(text("INSERT INTO system_config VALUES ('caller-owned','null')"))
        else:
            caller.execute(text('SELECT 1'))
        store.write(BASE)
        assert caller.in_transaction()
        assert caller not in commits and caller not in rollbacks
        if checkpoint_blocked:
            assert caller.execute(text("SELECT count(*) FROM system_config WHERE key='caller-owned'")).scalar_one() == 1
        if caller_commits:
            caller.commit()
        else:
            caller.rollback()
    with engine.connect() as observer:
        assert observer.execute(text("SELECT count(*) FROM system_config WHERE key='caller-owned'")).scalar_one() == int(checkpoint_blocked and caller_commits)
        assert observer.execute(text('SELECT count(*) FROM system_config WHERE key=:key'), {'key': KEY}).scalar_one() == int(not checkpoint_blocked)
        assert observer.exec_driver_sql('PRAGMA busy_timeout').scalar_one() == 5000
